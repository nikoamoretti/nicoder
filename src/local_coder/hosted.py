"""Hosted chat models (NVIDIA's API) behind llama.cpp's non-streaming reply shape.

Used only when the machine config selects a hosted provider. The API key is
read at request time from the environment or the macOS Keychain item that the
`key` tool writes (`key:<NAME>`, created by /usr/bin/security, so reading it
never shows a password prompt). The key never reaches config, logs, events,
artifacts, or error text.

Replies are streamed so a long generation keeps the connection alive, then
folded into one completion dict. A stream that drops before it finishes, or a
busy/5xx answer, is retried once; refusals such as a bad key are not.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

import httpx

from .machine import MachineConfig

KEYCHAIN_PREFIX = "key:"
_SECURITY = "/usr/bin/security"
_PROVIDER_NAMES = {"nvidia": "NVIDIA"}
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_READ_TIMEOUT_S = 120.0


class HostedError(RuntimeError):
    """The hosted model refused the request, could not be reached, or sent no complete reply."""


class _Retryable(HostedError):
    """A dropped stream or busy answer worth exactly one more attempt."""


def provider_name(cfg: MachineConfig) -> str:
    """Display name of the hosted provider."""
    return _PROVIDER_NAMES.get(cfg.provider, cfg.provider)


def read_keychain(service: str) -> str:
    """Password of a generic Keychain item, or '' when there is none."""
    try:
        proc = subprocess.run(
            [_SECURITY, "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def find_api_key(name: str) -> tuple[str, str]:
    """Return (value, where it came from). The value is '' when the key is not on this Mac."""
    if not name:
        return "", "no API key name is configured"
    value = os.environ.get(name, "").strip()
    if value:
        return value, f"environment variable {name}"
    value = read_keychain(KEYCHAIN_PREFIX + name)
    if value:
        return value, f"Keychain item {KEYCHAIN_PREFIX}{name}"
    return "", f"{name} is not set and the Keychain has no {KEYCHAIN_PREFIX}{name} item"


def hosted_chat(cfg: MachineConfig, body: dict[str, Any], *, thinking: bool) -> dict[str, Any]:
    """One chat completion from the hosted provider, shaped like a non-streaming llama.cpp reply."""
    key, where = find_api_key(cfg.api_key_name)
    if not key:
        raise HostedError(f"The {provider_name(cfg)} key was not found: {where}.")
    payload = {
        **body,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    problem = ""
    for attempt in (1, 2):
        try:
            data = _stream_once(cfg, payload, key)
        except _Retryable as exc:
            problem = str(exc)
            if attempt == 1:
                time.sleep(2)
            continue
        data["timings"]["attempts"] = attempt
        return data
    raise HostedError(f"{provider_name(cfg)} did not answer after two attempts: {problem}")


def _stream_once(cfg: MachineConfig, payload: dict[str, Any], key: str) -> dict[str, Any]:
    url = f"{cfg.endpoint.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Accept": "text/event-stream"}
    deadline = time.monotonic() + max(30, int(cfg.gen_timeout))
    content: list[str] = []
    reasoning: list[str] = []
    finish: str | None = None
    usage: dict[str, Any] = {}
    reply_id = None
    served = None
    first_token_s = None
    started = time.perf_counter()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(_READ_TIMEOUT_S, connect=15.0), trust_env=False, follow_redirects=False,
        ) as client, client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code != 200:
                detail = _error_detail(response)
                message = f"HTTP {response.status_code}{': ' + detail if detail else ''}"
                if response.status_code in _RETRY_STATUS:
                    raise _Retryable(message)
                raise HostedError(f"{provider_name(cfg)} refused the request ({message}).")
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    raise HostedError(f"no complete reply within {cfg.gen_timeout} seconds")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                reply_id = reply_id or event.get("id")
                served = served or event.get("model")
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        reasoning.append(str(delta["reasoning_content"]))
                    if delta.get("content"):
                        content.append(str(delta["content"]))
                    if first_token_s is None and (content or reasoning):
                        first_token_s = round(time.perf_counter() - started, 3)
                    if choice.get("finish_reason"):
                        finish = str(choice["finish_reason"])
    except (httpx.HTTPError, OSError) as exc:
        raise _Retryable(f"{type(exc).__name__}: {exc}") from exc
    if finish is None:
        raise _Retryable("the connection closed before the reply finished")
    return {
        "id": reply_id,
        "model": served,
        "choices": [{
            "index": 0,
            "finish_reason": finish,
            "message": {
                "role": "assistant",
                "content": "".join(content),
                "reasoning_content": "".join(reasoning) or None,
            },
        }],
        "usage": usage,
        "timings": {"first_token_s": first_token_s, "stream_s": round(time.perf_counter() - started, 3)},
    }


def _error_detail(response: httpx.Response) -> str:
    """Short provider explanation from an error body (never request headers)."""
    try:
        response.read()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for name in ("detail", "message", "error", "title"):
        value = data.get(name)
        if isinstance(value, dict):
            value = value.get("message")
        if value:
            return str(value)[:200]
    return ""
