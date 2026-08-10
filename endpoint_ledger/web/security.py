"""CSRF protection for the private dashboard (double-submit cookie).

Every unsafe request (POST/PUT/DELETE) must carry the token from the
``csrftoken`` cookie in a form field or header. All state-changing dashboard
actions are protected; GET responses embed the token as a hidden form input.

Implemented as pure ASGI (not BaseHTTPMiddleware) so request bodies can be
replayed safely for form parsing.
"""

from __future__ import annotations

import secrets
import uuid
from urllib.parse import parse_qs

from starlette.requests import Request

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
COOKIE_NAME = "el_csrftoken"
FORM_FIELD = "csrf_token"


class CSRFMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request = Request(scope, receive=receive)
        existing = request.cookies.get(COOKIE_NAME)
        token = existing or uuid.uuid4().hex
        request.state.csrf_token = token

        if request.method in SAFE_METHODS:
            response_started = False

            async def _send(message):
                nonlocal response_started
                if message["type"] == "http.response.start" and not existing:
                    headers = list(message.get("headers", []))
                    headers.append(
                        (
                            b"set-cookie",
                            f"{COOKIE_NAME}={token}; Path=/; SameSite=Strict".encode(),
                        )
                    )
                    message = dict(message)
                    message["headers"] = headers
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

            return await self.app(scope, receive, _send)

        # Unsafe method: validate cookie vs presented token.
        presented = request.headers.get("x-csrf-token")
        body = b""
        if presented is None:
            body = await request.body()
            parsed = parse_qs(body.decode("utf-8", errors="ignore"))
            values = parsed.get(FORM_FIELD) or parsed.get(FORM_FIELD.replace("-", "_"))
            presented = values[0] if values else None

        async def replay():
            return {"type": "http.request", "body": body, "more_body": False}

        if not existing or not presented or not secrets.compare_digest(str(existing), str(presented)):
            response = _text_response("forbidden: invalid CSRF token", 403)
            return await response(scope, receive, send)

        return await self.app(scope, replay, send)


def _text_response(body: str, status: int):
    from starlette.responses import PlainTextResponse

    return PlainTextResponse(body, status_code=status)


def csrf_token_for(request: Request) -> str:
    """The CSRF token for the current request (set by the middleware)."""
    return getattr(request.state, "csrf_token", "")
