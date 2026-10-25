"""OrcaRouter: connect an account, keep its key in the Keychain, and list its models.

OrcaRouter (api.orcarouter.ai) is an OpenAI-compatible router to many providers'
models. Nicoder calls it like any hosted provider (`hosted.py`, provider
"orcarouter"); this module covers the rest:

- The key lives in the Keychain item `key:ORCAROUTER_API_KEY`, the same item
  format as Nico's `key` tool. It is written by /usr/bin/security in its
  interactive mode, fed on stdin, so the value never shows in a process list.
  It never reaches config, logs, events, results or error text.
- "Sign in with OrcaRouter" is OAuth 2.0 with PKCE and a loopback callback:
  the browser opens orcarouter.ai/auth, the user approves, the code comes back
  to 127.0.0.1 and is exchanged for a new key.
- The model list is OrcaRouter's public /v1/models joined with its public
  benchmark board (intelligence index, speed, price), cached for the picker.
  Only chat models that can call tools are offered: Nicoder's agent needs
  tool calls.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import json
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import hosted
from .hosted import KEYCHAIN_PREFIX, find_api_key
from .machine import config_dir

API = "https://api.orcarouter.ai"
SITE = "https://www.orcarouter.ai"
CONSOLE_URL = f"{SITE}/console"
KEY_NAME = "ORCAROUTER_API_KEY"
KEY_SERVICE = f"{KEYCHAIN_PREFIX}{KEY_NAME}"
CATALOG_MAX_AGE_S = 12 * 3600
RECENT_LIMIT = 8
SIGN_IN_SECONDS = 300
_SECURITY = "/usr/bin/security"
_KEY_SHAPE = re.compile(r"^sk-orca-[A-Za-z0-9_.-]{8,200}$")
_NOT_CHAT = ("embed", "tts", "image", "imagen", "video", "audio", "transcribe", "realtime", "search", "moderation")
_PROVIDERS = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "deepseek": "DeepSeek", "grok": "xAI",
    "qwen": "Qwen", "kimi": "Moonshot", "minimax": "MiniMax", "z-ai": "Z.ai", "tencent": "Tencent",
    "orcarouter": "OrcaRouter", "orca": "OrcaRouter",
}


class OrcaError(RuntimeError):
    """A plain-language OrcaRouter problem for the app to show."""


# --- the key -------------------------------------------------------------------------------------------------


def key_status() -> dict[str, Any]:
    """Whether a key is on this Mac and where it lives (never the value)."""
    value, where = find_api_key(KEY_NAME)
    return {
        "connected": bool(value),
        "where": where if value else "",
        "key_name": KEY_NAME,
        "console_url": CONSOLE_URL,
    }


def _security(command: str) -> subprocess.CompletedProcess[str]:
    """Run one /usr/bin/security command fed on stdin, so arguments stay out of the process list."""
    return subprocess.run([_SECURITY, "-i"], input=command + "\n", capture_output=True, text=True, timeout=20)


def _item_exists() -> bool:
    """Whether a Keychain item for the key exists (reads attributes only, not the value)."""
    try:
        proc = subprocess.run([_SECURITY, "find-generic-password", "-s", KEY_SERVICE], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _delete_items() -> int:
    """Remove every Keychain item for the key (an older one could shadow a new one)."""
    removed = 0
    while removed < 5 and _item_exists():
        _security(f"delete-generic-password -s {KEY_SERVICE}")
        removed += 1
    return removed


def store_key(value: str) -> dict[str, Any]:
    """Save the key in the Keychain, replacing any older one."""
    value = value.strip()
    if not _KEY_SHAPE.match(value):
        raise OrcaError("That does not look like an OrcaRouter key. Keys start with sk-orca-.")
    _delete_items()
    account = re.sub(r"[^A-Za-z0-9_.-]", "", getpass.getuser()) or "local-coder"
    _security(f"add-generic-password -U -a {account} -s {KEY_SERVICE} -w {value}")
    if hosted.read_keychain(KEY_SERVICE) != value:
        raise OrcaError("The Keychain did not save the key.")
    _register_with_key_tool()
    return key_status()


def remove_key() -> dict[str, Any]:
    """Forget the key on this Mac (the key itself stays valid in the OrcaRouter console)."""
    _delete_items()
    return key_status()


def _register_with_key_tool() -> None:
    """List the item for Nico's `key` tool, when it is installed, so `key list` shows it."""
    folder = Path.home() / ".config" / "key"
    listing = folder / "keychain.txt"
    if not folder.is_dir():
        return
    known = listing.read_text(encoding="utf-8").split() if listing.is_file() else []
    if KEY_SERVICE not in known:
        with listing.open("a", encoding="utf-8") as handle:
            handle.write(KEY_SERVICE + "\n")


def verify_key(value: str, *, timeout_s: float = 15.0) -> bool | None:
    """True when OrcaRouter accepts the key, False when it refuses it, None when it could not tell."""
    try:
        response = httpx.get(
            f"{API}/v1/dashboard/billing/subscription",
            headers={"Authorization": f"Bearer {value.strip()}"},
            timeout=timeout_s,
            trust_env=False,
        )
    except httpx.HTTPError:
        return None
    if response.status_code == 200:
        return True
    if response.status_code in {401, 403}:
        return False
    return None


def save_pasted_key(value: str) -> dict[str, Any]:
    """Check a pasted key with OrcaRouter, then keep it."""
    value = value.strip()
    if not _KEY_SHAPE.match(value):
        raise OrcaError("That does not look like an OrcaRouter key. Keys start with sk-orca-.")
    accepted = verify_key(value)
    if accepted is False:
        raise OrcaError("OrcaRouter did not accept that key. Copy it again from orcarouter.ai/console.")
    status = store_key(value)
    status["detail"] = ("OrcaRouter is connected." if accepted
                        else "The key is saved. OrcaRouter could not be reached to check it.")
    return status


# --- sign in with OrcaRouter (OAuth 2.0 + PKCE, loopback callback) --------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)."""
    verifier = _b64url(secrets.token_bytes(32))
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


_DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>Nicoder</title>
<body style="font: 15px -apple-system, system-ui; display: grid; place-items: center; height: 90vh; margin: 0;
color-scheme: light dark"><div style="text-align: center"><h2 style="font-weight: 600">{title}</h2>
<p style="opacity: .7">{body}</p></div></body>"""


class _Callback(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.reply: dict[str, str] | None = None
        self.arrived = threading.Event()


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _Callback

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 — http.server naming
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = {name: values[0] for name, values in parse_qs(parsed.query).items() if values}
        failed = bool(query.get("error")) or not query.get("code")
        title = "Nicoder could not connect" if failed else "OrcaRouter is connected"
        body = ("You can close this tab and try again from Nicoder." if failed
                else "You can close this tab and go back to Nicoder.")
        page = _DONE_PAGE.format(title=title, body=body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)
        if self.server.reply is None:
            self.server.reply = query
            self.server.arrived.set()


def _open_in_browser(url: str) -> None:
    subprocess.run(["/usr/bin/open", url], check=False, timeout=20)


def exchange_code(code: str, verifier: str, *, timeout_s: float = 30.0) -> str:
    """Trade the one-time code for a new API key."""
    try:
        response = httpx.post(
            f"{SITE}/api/v1/auth/keys", json={"code": code, "code_verifier": verifier}, timeout=timeout_s,
            trust_env=False,
        )
    except httpx.HTTPError as exc:
        raise OrcaError(f"OrcaRouter could not be reached to finish signing in ({type(exc).__name__}).") from exc
    if response.status_code != 200:
        raise OrcaError(f"OrcaRouter did not finish the sign-in (HTTP {response.status_code}). Try again.")
    try:
        data = response.json()
    except ValueError as exc:
        raise OrcaError("OrcaRouter sent an unreadable sign-in reply. Try again.") from exc
    key = str(data.get("key") or "") if isinstance(data, dict) else ""
    if not _KEY_SHAPE.match(key):
        raise OrcaError("OrcaRouter's sign-in reply had no usable key. Try again.")
    if str(data.get("scope") or "api") != "api":
        raise OrcaError("OrcaRouter granted a key that cannot call models. Try again.")
    return key


def sign_in(
    *,
    on_status: Any = None,
    cancel: threading.Event | None = None,
    open_url: Any = None,
    timeout_s: float = SIGN_IN_SECONDS,
) -> dict[str, Any]:
    """Open OrcaRouter's approval page, wait for the callback, then keep the new key."""
    say = on_status or (lambda _text: None)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    server = _Callback()
    port = server.server_address[1]
    url = f"{SITE}/auth?" + urlencode({
        "callback_url": f"http://127.0.0.1:{port}/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "app_name": "Nicoder",
        "scope": "api",
    })
    worker = threading.Thread(target=server.serve_forever, name="orcarouter-callback", daemon=True)
    worker.start()
    try:
        (open_url or _open_in_browser)(url)
        say("Approve Nicoder in the browser window that just opened.")
        deadline = time.monotonic() + timeout_s
        while not server.arrived.wait(0.25):
            if cancel is not None and cancel.is_set():
                raise OrcaError("Sign-in was cancelled.")
            if time.monotonic() > deadline:
                raise OrcaError("Sign-in timed out. Try again when you are ready to approve it.")
    finally:
        server.shutdown()
        server.server_close()
    reply = server.reply or {}
    if reply.get("state") != state:
        raise OrcaError("The sign-in reply did not match this request. Try again.")
    if reply.get("error"):
        raise OrcaError("Sign-in was declined on OrcaRouter." if reply["error"] == "access_denied"
                        else f"OrcaRouter stopped the sign-in ({reply['error'][:60]}).")
    say("Finishing the connection.")
    status = store_key(exchange_code(reply.get("code", ""), verifier))
    status["detail"] = "OrcaRouter is connected."
    return status


# --- the model list ------------------------------------------------------------------------------------------


def catalog_path() -> Path:
    """Cached model list (public data; no key inside)."""
    return config_dir() / "orcarouter-models.json"


def prefs_path() -> Path:
    """Recently used OrcaRouter models."""
    return config_dir() / "orcarouter.json"


def _price(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _first_sentence(text: str, limit: int = 180) -> str:
    text = " ".join(str(text or "").split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def has_reasoning_switch(model_id: str, parameters: list[Any] | None) -> bool:
    """Whether thinking can be turned off: Qwen models, and any model that lists enable_thinking.

    Checked on 2026-10-22: OrcaRouter ignores `reasoning` for Qwen3.8 27B; `chat_template_kwargs`
    (self-hosted Qwen) or `enable_thinking` (Alibaba's API) turn it off.
    """
    return model_id.startswith("qwen/") or "enable_thinking" in (parameters or [])


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize(raw: list[dict[str, Any]], bench: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Chat models that can call tools, with the facts the picker shows."""
    scores = {str(row.get("model")): row for row in bench or [] if isinstance(row, dict) and row.get("model")}
    models: list[dict[str, Any]] = []
    for item in raw:
        model_id = str(item.get("id") or "")
        if not model_id or "/" not in model_id:
            continue
        prefix, slug = model_id.split("/", 1)
        lowered = model_id.lower()
        capabilities = item.get("endpoint_capabilities")
        if isinstance(capabilities, list):
            if "chat_completions" not in capabilities:
                continue
        elif "openai" not in (item.get("supported_endpoint_types") or []):
            continue
        architecture = _dict(item.get("architecture"))
        outputs = _list(architecture.get("output_modalities"))
        if outputs and outputs != ["text"]:
            continue
        if prefix == "orcarouter" and slug != "free":
            continue  # fusion panels answer with a judge, not tool calls
        if any(word in lowered for word in _NOT_CHAT):
            continue
        parameters = item.get("supported_parameters")
        if isinstance(parameters, list) and "tools" not in parameters:
            continue
        pricing = _dict(item.get("pricing"))
        price_in = _price(pricing.get("prompt_per_million"))
        price_out = _price(pricing.get("completion_per_million"))
        free = lowered.endswith("-free") or slug == "free" or (
            price_in == 0 and price_out == 0) or (not price_in and _price(pricing.get("request")) == 0)
        name = str(item.get("name") or "")
        provider = _PROVIDERS.get(prefix, prefix.title())
        if ": " in name:
            provider_label, name = name.split(": ", 1)
            provider = _PROVIDERS.get(prefix) or provider_label
        bench_row = _dict(scores.get(slug) or scores.get(slug.removesuffix("-free")))
        inputs = _list(architecture.get("input_modalities"))
        models.append({
            "id": model_id,
            "name": name or slug,
            "provider": provider,
            "description": _first_sentence(str(item.get("description") or "")),
            "context": int(item["context_length"]) if isinstance(item.get("context_length"), int) else None,
            "max_output": (int(item["max_completion_tokens"]) if isinstance(item.get("max_completion_tokens"), int)
                           else None),
            "price_in": 0.0 if free else price_in,
            "price_out": 0.0 if free else price_out,
            "free": bool(free),
            "reasoning": bool(isinstance(parameters, list) and ("reasoning" in parameters
                                                                 or "include_reasoning" in parameters)),
            "vision": "image" in inputs,
            "tools_listed": isinstance(parameters, list),
            "reasoning_switch": has_reasoning_switch(model_id, parameters if isinstance(parameters, list) else None),
            "intelligence": _price(bench_row.get("intelligence")),
            "speed": round(float(bench_row["tokens_per_sec"])) if bench_row.get("tokens_per_sec") else None,
        })
    return models


def fetch_catalog(*, timeout_s: float = 20.0) -> dict[str, Any]:
    """Download the model list and the benchmark board, cache them, and return the joined list."""
    key = find_api_key(KEY_NAME)[0]
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with httpx.Client(timeout=timeout_s, trust_env=False) as client:
            listing = client.get(f"{API}/v1/models", headers=headers)
            listing.raise_for_status()
            raw = listing.json().get("data") or []
            bench: list[dict[str, Any]] = []
            try:
                board = client.get(f"{API}/api/public/benchmark-board")
                if board.status_code == 200:
                    bench = (board.json().get("data") or {}).get("items") or []
            except (httpx.HTTPError, ValueError, AttributeError):
                bench = []
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise OrcaError(f"OrcaRouter's model list could not be loaded ({type(exc).__name__}).") from exc
    payload = {"fetched_at": int(time.time()), "models": normalize(raw, bench)}
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return payload


def cached_catalog() -> dict[str, Any]:
    """The last downloaded list (no network); empty when there is none yet."""
    try:
        data = json.loads(catalog_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"fetched_at": 0, "models": []}
    return data if isinstance(data, dict) and isinstance(data.get("models"), list) else {"fetched_at": 0, "models": []}


def load_catalog(*, refresh: bool = False) -> dict[str, Any]:
    """The model list, downloaded again when asked or when the cache is over 12 hours old."""
    cached = cached_catalog()
    if not refresh and cached["models"] and time.time() - float(cached.get("fetched_at") or 0) < CATALOG_MAX_AGE_S:
        return cached
    try:
        return fetch_catalog()
    except OrcaError as exc:
        if cached["models"]:
            return {**cached, "stale": True, "error": str(exc)}
        raise


def find_model(model_id: str) -> dict[str, Any] | None:
    """One model from the cached list."""
    return next((row for row in cached_catalog()["models"] if row.get("id") == model_id), None)


def suggested(models: list[dict[str, Any]], *, count: int = 3) -> list[str]:
    """A short default list: the smartest affordable models, then the smartest free one."""
    affordable = [row for row in models if row.get("intelligence") is not None and not row.get("free")
                  and (row.get("price_in") or 0) <= 5]
    picks = [row["id"] for row in sorted(affordable, key=lambda row: -float(row["intelligence"]))[:count]]
    free = [row for row in models if row.get("free") and row["id"] != "orcarouter/free"]
    if free:
        best = max(free, key=lambda row: float(row.get("intelligence") or 0))
        picks.append(best["id"])
    return picks


def recent_models() -> list[str]:
    """Most recently chosen OrcaRouter models, newest first."""
    try:
        data = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    recent = data.get("recent") if isinstance(data, dict) else None
    return [str(item) for item in recent if isinstance(item, str)][:RECENT_LIMIT] if isinstance(recent, list) else []


def remember_model(model_id: str) -> list[str]:
    """Put a model at the top of the recent list."""
    recent = [model_id, *[item for item in recent_models() if item != model_id]][:RECENT_LIMIT]
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"recent": recent}, indent=2) + "\n", encoding="utf-8")
    return recent


def menu_models(selected_id: str = "") -> list[str]:
    """Ids for the model menu: the selected one, recent ones, else a suggested few."""
    ids = [*([selected_id] if selected_id else []), *recent_models()]
    if len(ids) < 2:
        ids += suggested(cached_catalog()["models"])
    seen: list[str] = []
    for item in ids:
        if item and item not in seen:
            seen.append(item)
    return seen[:RECENT_LIMIT]


def price_label(row: dict[str, Any]) -> str:
    """'Free' or '$0.66 in · $1.98 out' per million tokens."""
    if row.get("free"):
        return "Free"
    price_in, price_out = row.get("price_in"), row.get("price_out")
    if price_in is None or price_out is None:
        return "Price not listed"
    return f"${price_in:g} in · ${price_out:g} out"
