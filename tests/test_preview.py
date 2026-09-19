"""Preview health treats Vite overlays as failure, not a running app."""
from __future__ import annotations

from pathlib import Path

import httpx

from local_coder.general.preview import check_preview_health, preview_error_from_response


def test_preview_error_from_babel_overlay():
    body = (
        "[plugin:vite:react-babel] /candidate/src/App.tsx: Unexpected token, expected \";\"\n"
        '73 | const c: Record<Task["status"], number> = { todo: 0, "in-progress": 0, done: 0 };\n'
    )
    detail = preview_error_from_response(200, body)
    assert detail is not None
    assert "vite:react-babel" in detail
    assert preview_error_from_response(500, body)
    assert preview_error_from_response(200, "export default function App(){return null}") is None


def test_check_preview_health_fails_on_module_500(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("export default function App(){return null}\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(
                200,
                text='<html><script type="module" src="/src/main.tsx"></script></html>',
            )
        if request.url.path == "/src/main.tsx":
            return httpx.Response(200, text="import App from './App.tsx'")
        if request.url.path == "/src/App.tsx":
            return httpx.Response(
                500,
                text='[plugin:vite:react-babel] Unexpected token, expected ";"',
            )
        return httpx.Response(404, text="missing")

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    result = check_preview_health(
        "http://127.0.0.1:4173/",
        Path(tmp_path),
        client=client,
        wait_s=0.1,
    )
    assert result["ok"] is False
    assert result["code"] == "vite_overlay"
    assert "babel" in result["detail"].lower() or "unexpected token" in result["detail"].lower()


def test_check_preview_health_ok_when_modules_load(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("export default function App(){return null}\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="export default function App(){return null}")

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    result = check_preview_health(
        "http://127.0.0.1:4173/",
        Path(tmp_path),
        client=client,
        wait_s=0.1,
    )
    assert result["ok"] is True
