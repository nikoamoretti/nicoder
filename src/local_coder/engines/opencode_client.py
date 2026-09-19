"""HTTP client for a pinned OpenCode server. Schema is verified at runtime."""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import httpx


class OpenCodeError(Exception):
    """A refused or failed OpenCode API call."""

    def __init__(self, detail: str, *, status: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


class OpenCodeClient:
    """Authenticated loopback client. The password is never included in logs."""

    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = (username, password)
        self._timeout = timeout

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            auth=self._auth,
            timeout=timeout if timeout is not None else self._timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def health(self, *, timeout: float | None = None) -> dict[str, Any]:
        """GET /global/health, falling back to /health on older builds."""
        try:
            return self._json("GET", "/global/health", timeout=timeout)
        except OpenCodeError as exc:
            if exc.status == 404:
                return self._json("GET", "/health", timeout=timeout)
            raise

    def openapi(self) -> dict[str, Any]:
        """GET /doc OpenAPI document when the server serves JSON."""
        with self._client() as client:
            response = client.get("/doc", headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise OpenCodeError(f"openapi failed: {response.status_code}", status=response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise OpenCodeError("openapi document was not JSON") from exc

    def config(self, *, timeout: float | None = None) -> dict[str, Any]:
        return self._json("GET", "/config", timeout=timeout)

    def providers(self) -> dict[str, Any]:
        return self._json("GET", "/provider")

    def path(self) -> dict[str, Any]:
        return self._json("GET", "/path")

    def experimental_tools(self, provider: str, model: str) -> Any:
        return self._json("GET", "/experimental/tool", params={"provider": provider, "model": model})

    def create_session(self, *, title: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        return self._json("POST", "/session", json=body)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._json("GET", f"/session/{quote(session_id)}")

    def abort(self, session_id: str) -> Any:
        return self._json("POST", f"/session/{quote(session_id)}/abort")

    def diff(self, session_id: str) -> Any:
        return self._json("GET", f"/session/{quote(session_id)}/diff")

    def messages(self, session_id: str, *, limit: int | None = None) -> Any:
        params = {"limit": str(limit)} if limit else None
        return self._json("GET", f"/session/{quote(session_id)}/message", params=params)

    def prompt_async(
        self,
        session_id: str,
        text: str,
        *,
        provider_id: str,
        model_id: str,
        agent: str = "build",
    ) -> None:
        """Send a user message without waiting for the full reply."""
        body = {
            "agent": agent,
            "model": {"providerID": provider_id, "modelID": model_id},
            "parts": [{"type": "text", "text": text}],
        }
        with self._client(timeout=60.0) as client:
            response = client.post(f"/session/{quote(session_id)}/prompt_async", json=body)
        if response.status_code not in {200, 204}:
            raise OpenCodeError(
                f"prompt_async failed: {response.status_code} {response.text[:300]}",
                status=response.status_code,
            )

    def respond_permission(self, session_id: str, permission_id: str, response: str) -> Any:
        return self._json(
            "POST",
            f"/session/{quote(session_id)}/permissions/{quote(permission_id)}",
            json={"response": response},
        )

    def events(self) -> Iterator[dict[str, Any]]:
        """Yield parsed SSE event payloads from GET /event."""
        with self._client(timeout=None) as client:
            with client.stream("GET", "/event") as response:
                if response.status_code >= 400:
                    raise OpenCodeError(f"event stream failed: {response.status_code}",
                                       status=response.status_code)
                yield from _iter_sse(response)

    def _json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        with self._client(timeout=timeout) as client:
            response = client.request(method, path, json=json, params=params)
        if response.status_code >= 400:
            raise OpenCodeError(
                f"{method} {path} failed: {response.status_code} {response.text[:300]}",
                status=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise OpenCodeError(f"{method} {path} returned non-JSON") from exc


def _iter_sse(response: httpx.Response) -> Iterator[dict[str, Any]]:
    event_name = "message"
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line is None:
            continue
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {"raw": raw}
                if isinstance(payload, dict):
                    payload.setdefault("event", event_name)
                    yield payload
                else:
                    yield {"event": event_name, "data": payload}
            event_name = "message"
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
