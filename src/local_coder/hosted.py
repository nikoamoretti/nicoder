"""Hosted chat models (NVIDIA's API and OrcaRouter) behind llama.cpp's reply shape.

Used only when the machine config selects a hosted provider. The API key is
read at request time from the environment or the macOS Keychain item that the
`key` tool writes (`key:<NAME>`, created by /usr/bin/security, so reading it
never shows a password prompt). The key never reaches config, logs, events,
artifacts, or error text.

Replies are streamed so a long generation keeps the connection alive, then
folded into one completion dict (text, reasoning, tool calls and usage; on
OrcaRouter the usage carries the billed `cost_usd`). Hosted endpoints fail
intermittently, so a dropped stream or a 5xx answer gets three attempts, a
rate limit (429) four; refusals such as a bad key or an empty balance are not
retried.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from .machine import MachineConfig

KEYCHAIN_PREFIX = "key:"
_SECURITY = "/usr/bin/security"
_PROVIDER_NAMES = {"nvidia": "NVIDIA", "orcarouter": "OrcaRouter"}
_PROVIDER_PLACES = {"nvidia": "NVIDIA's servers", "orcarouter": "OrcaRouter"}
_RETRY_STATUS = frozenset({500, 502, 503, 504})
_READ_TIMEOUT_S = 120.0
_ATTEMPTS = 3
_BUSY_ATTEMPTS = 4
_BUSY_WAIT_CAP_S = 30.0
# A connection that cannot be opened sent nothing, so waiting costs nothing: a Mac waking from sleep, Wi-Fi
# reconnecting or a short outage (Nico, 2026-10-26: "OrcaRouter did not answer after 3 attempts: ConnectTimeout").
_OFFLINE_PATIENCE_S = 180.0
# OrcaRouter refusals that no retry fixes, with what to tell the user.
_ORCA_REFUSALS = {
    "insufficient_user_quota": "Your OrcaRouter balance is empty. Add credits at orcarouter.ai/console, "
                               "or pick a free model.",
    "free_quota_exhausted": "No free OrcaRouter model could take this request right now. Pick a paid model.",
    "model_not_found": "OrcaRouter does not offer this model to your account. Pick another one.",
    "model_not_yet_available": "OrcaRouter has announced this model but it is not live yet. Pick another one.",
    "budget_exceeded": "Your OrcaRouter workspace reached its monthly budget for this key.",
    "pre_consume_token_quota_failed": "This OrcaRouter key used up its own spending cap.",
}


class HostedError(RuntimeError):
    """The hosted model refused the request, could not be reached, or sent no complete reply."""


class _Retryable(HostedError):
    """A dropped stream or server error worth exactly one more attempt."""


class _Unreachable(_Retryable):
    """The connection could not be opened (offline, DNS, connect timeout): nothing reached the provider."""


class _Stopped(HostedError):
    """The user pressed Stop while the reply was streaming."""


class _Busy(HostedError):
    """A rate limit: wait, then try again."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def provider_name(cfg: MachineConfig) -> str:
    """Display name of the hosted provider."""
    return _PROVIDER_NAMES.get(cfg.provider, cfg.provider)


def provider_place(cfg: MachineConfig) -> str:
    """Where the model runs, for sentences like "Using X on <place>"."""
    return _PROVIDER_PLACES.get(cfg.provider, f"{provider_name(cfg)}'s servers")


def credential(cfg: MachineConfig) -> tuple[str, str]:
    """The bearer credential for this provider and where it came from ('' when missing)."""
    return find_api_key(cfg.api_key_name)


def credential_status(cfg: MachineConfig) -> tuple[bool, str]:
    """Whether the credential is available, without printing it."""
    value, where = credential(cfg)
    return bool(value), where


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


def hosted_chat(
    cfg: MachineConfig,
    body: dict[str, Any],
    *,
    thinking: bool,
    session: str = "",
    cancel: threading.Event | None = None,
    on_wait: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """One chat completion from the hosted provider, shaped like a non-streaming llama.cpp reply.

    `session` groups an agent run's calls on OrcaRouter (its Sessions view); other providers ignore it.
    `cancel` (the run's Stop) ends a streaming reply at the next chunk instead of after it finishes, and ends a wait.
    `on_wait` hears one line when the provider cannot be reached and the call waits for the connection.
    """
    key, where = credential(cfg)
    if not key:
        raise HostedError(f"The {provider_name(cfg)} key was not found: {where}.")
    payload: dict[str, Any] = {**body, "stream": True, "stream_options": {"include_usage": True}}
    extra_headers: dict[str, str] = {}
    if cfg.provider == "orcarouter":
        extra_headers["X-OrcaRouter-Include-Cost"] = "true"
        if session:
            extra_headers["X-OrcaRouter-Session-Id"] = session
        if not thinking and cfg.reasoning_switch:
            # Alibaba's API reads enable_thinking; Qwen served by OrcaRouter itself reads the chat template.
            payload["enable_thinking"] = False
            if cfg.model_alias.startswith("qwen/"):
                payload["chat_template_kwargs"] = {"enable_thinking": False}
    else:
        payload["chat_template_kwargs"] = {"enable_thinking": thinking}
    problem = ""
    attempt = 0
    failed = 0  # attempts that reached the provider and failed
    offline_since: float | None = None
    while True:
        attempt += 1
        if cancel is not None and cancel.is_set():
            raise _Stopped("Stopped.")
        try:
            data = _stream_once(cfg, payload, key, extra_headers, cancel)
        except _Busy as exc:
            problem = str(exc)
            if attempt >= _BUSY_ATTEMPTS:
                break
            _pause(min(_BUSY_WAIT_CAP_S, exc.retry_after or 5.0 * attempt), cancel)
            continue
        except _Unreachable as exc:
            now = time.monotonic()
            if offline_since is None:
                offline_since = now
                if on_wait is not None:
                    on_wait(f"Waiting for the connection to {provider_name(cfg)}.")
            elif now - offline_since >= _OFFLINE_PATIENCE_S:
                minutes = max(1, round((now - offline_since) / 60))
                raise HostedError(
                    f"{provider_name(cfg)} could not be reached for {minutes} minutes; the Mac may be offline."
                ) from exc
            _pause(min(15.0, 2.0 ** min(attempt, 4)), cancel)
            continue
        except _Retryable as exc:
            problem = str(exc)
            failed += 1
            if failed >= _ATTEMPTS:
                break
            _pause(3.0 * failed, cancel)
            continue
        data["timings"]["attempts"] = attempt
        return data
    raise HostedError(f"{provider_name(cfg)} did not answer after {attempt} attempts: {problem}")


def _pause(seconds: float, cancel: threading.Event | None) -> None:
    """Wait between attempts; Stop ends the wait at once."""
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise _Stopped("Stopped.")


def _stream_once(
    cfg: MachineConfig, payload: dict[str, Any], key: str, extra_headers: dict[str, str],
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    url = f"{cfg.endpoint.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Accept": "text/event-stream", **extra_headers}
    deadline = time.monotonic() + max(30, int(cfg.gen_timeout))
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
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
                _raise_for_status(cfg, response)
            for line in response.iter_lines():
                if cancel is not None and cancel.is_set():
                    raise _Stopped("Stopped.")
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
                if isinstance(event.get("error"), dict):
                    message, code, _meta = _error_fields(event)
                    if code in _ORCA_REFUSALS:
                        raise HostedError(_ORCA_REFUSALS[code])
                    raise _Retryable(f"the reply stopped with an error: {message or code or 'no detail'}")
                reply_id = reply_id or event.get("id")
                served = served or event.get("model")
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content") or delta.get("reasoning"):
                        reasoning.append(str(delta.get("reasoning_content") or delta.get("reasoning")))
                    if delta.get("content"):
                        content.append(str(delta["content"]))
                    _fold_tool_calls(tool_calls, delta.get("tool_calls"))
                    if first_token_s is None and (content or reasoning or tool_calls):
                        first_token_s = round(time.perf_counter() - started, 3)
                    if choice.get("finish_reason"):
                        finish = str(choice["finish_reason"])
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise _Unreachable(f"{type(exc).__name__}: {exc}") from exc
    except (httpx.HTTPError, OSError) as exc:
        raise _Retryable(f"{type(exc).__name__}: {exc}") from exc
    if finish is None:
        raise _Retryable("the connection closed before the reply finished")
    reply: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content),
        "reasoning_content": "".join(reasoning) or None,
    }
    if tool_calls:
        reply["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    return {
        "id": reply_id,
        "model": served,
        "choices": [{"index": 0, "finish_reason": finish, "message": reply}],
        "usage": usage,
        "timings": {"first_token_s": first_token_s, "stream_s": round(time.perf_counter() - started, 3)},
    }


def _raise_for_status(cfg: MachineConfig, response: httpx.Response) -> None:
    """Turn a non-200 answer into the right error: wait, retry once more, or stop with a plain reason."""
    message, code, meta = _error_info(response)
    status = response.status_code
    text = f"HTTP {status}{': ' + message if message else ''}"
    if cfg.provider == "orcarouter":
        if code in _ORCA_REFUSALS:
            raise HostedError(_ORCA_REFUSALS[code])
        if status == 401:
            raise HostedError("OrcaRouter did not accept the API key. Connect OrcaRouter again in Settings.")
        if meta.get("retryable") is False or meta.get("reason") == "err_free_access_denied":
            raise HostedError(f"OrcaRouter refused the request ({text}).")
    if status == 429:
        raise _Busy(text, _retry_after(response))
    if status in _RETRY_STATUS:
        raise _Retryable(text)
    raise HostedError(f"{provider_name(cfg)} refused the request ({text}).")


def _fold_tool_calls(slots: dict[int, dict[str, Any]], deltas: Any) -> None:
    """Merge streamed tool-call pieces: the id and name arrive once, arguments in fragments."""
    if not isinstance(deltas, list):
        return
    for position, item in enumerate(deltas):
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            index = len(slots) if item.get("id") else max(slots, default=position)
        slot = slots.setdefault(index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
        if item.get("id") and not slot["id"]:
            slot["id"] = str(item["id"])
        function = item.get("function") or {}
        if function.get("name") and not slot["function"]["name"]:
            slot["function"]["name"] = str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            slot["function"]["arguments"] += json.dumps(arguments)
        elif arguments:
            slot["function"]["arguments"] += str(arguments)


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a Retry-After header, when it is a number."""
    try:
        return max(0.0, float(response.headers.get("retry-after") or ""))
    except ValueError:
        return None


def _error_info(response: httpx.Response) -> tuple[str, str, dict[str, Any]]:
    """(message, code, metadata) from an error body; never request headers."""
    try:
        response.read()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return "", "", {}
    return _error_fields(data) if isinstance(data, dict) else ("", "", {})


def _error_fields(data: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    error = data.get("error")
    if isinstance(error, dict):
        meta = error.get("metadata")
        return (str(error.get("message") or "")[:200], str(error.get("code") or ""),
                meta if isinstance(meta, dict) else {})
    for name in ("detail", "message", "error", "title"):
        value = data.get(name)
        if value:
            return str(value)[:200], "", {}
    return "", "", {}


def _error_detail(response: httpx.Response) -> str:
    """Short provider explanation from an error body (never request headers)."""
    return _error_info(response)[0]
