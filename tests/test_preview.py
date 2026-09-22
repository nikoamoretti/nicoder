"""Preview health treats Vite overlays as failure, not a running app."""
from __future__ import annotations

from pathlib import Path

import httpx

from local_coder.general.preview import (
    check_preview_health,
    page_mount_from_html,
    preview_error_from_response,
)


def test_preview_error_from_swc_overlay():
    body = (
        "[plugin:vite:react-swc] /candidate/src/App.tsx: Unexpected token\n"
        'const columns: Task["status\'][] = ["todo"];\n'
    )
    detail = preview_error_from_response(200, body)
    assert detail is not None
    assert "react-swc" in detail


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


def test_vite_client_library_is_not_an_overlay(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("export default function App(){return null}\n", encoding="utf-8")
    client_js = (
        'import "/node_modules/vite/dist/client/env.mjs";\n'
        'class ErrorOverlay extends HTMLElement {}\n'
        'customElements.define("vite-error-overlay", ErrorOverlay);\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<html><script type="module" src="/@vite/client"></script>'
                '<script type="module" src="/src/main.tsx"></script></html>',
            )
        if request.url.path == "/@vite/client":
            return httpx.Response(200, headers={"content-type": "text/javascript"}, text=client_js)
        return httpx.Response(
            200,
            headers={"content-type": "text/javascript"},
            text="export default function App(){return null}",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    result = check_preview_health(
        "http://127.0.0.1:4173/",
        Path(tmp_path),
        client=client,
        wait_s=0.1,
    )
    assert result["ok"] is True
    assert preview_error_from_response(
        200, client_js, content_type="text/javascript", url="http://127.0.0.1:4173/@vite/client",
    ) is None


def test_vite_client_html_is_not_a_mounted_page(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("export default function App(){return null}\n", encoding="utf-8")
    html = "<!doctype html><html><body><div id='root'></div></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<html><script type="module" src="/@vite/client"></script>'
                '<script type="module" src="/src/main.tsx"></script><div id="root"></div></html>',
            )
        if request.url.path == "/@vite/client":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=html)
        return httpx.Response(
            200,
            headers={"content-type": "text/javascript"},
            text="export default function App(){return null}",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    result = check_preview_health(
        "http://127.0.0.1:4173/",
        Path(tmp_path),
        client=client,
        wait_s=0.1,
    )
    assert result["ok"] is False
    assert result["code"] in {"vite_client_html", "vite_overlay"}
    assert "cannot mount" in result["detail"]


def test_nested_module_500_is_a_preview_failure(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text(
        'import TaskItem from "./TaskItem";\nexport default function App(){return <TaskItem />}\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "TaskItem.tsx").write_text(
        "export default function TaskItem(){return null}\n", encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/src/TaskItem.tsx":
            return httpx.Response(
                500,
                headers={"content-type": "text/html"},
                text='<!DOCTYPE html><title>Error</title><script>const error = {"message":'
                '"[plugin:vite:react-swc] optional chaining assignment"}</script>',
            )
        if request.url.path == "/":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<html><script type="module" src="/src/main.tsx"></script><div id="root"></div></html>',
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/javascript"},
            text="export default function App(){return null}",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    result = check_preview_health(
        "http://127.0.0.1:4173/",
        Path(tmp_path),
        client=client,
        wait_s=0.1,
    )
    assert result["ok"] is False
    assert "react-swc" in result["detail"].lower() or "optional chaining" in result["detail"].lower()


def test_empty_root_html_is_not_a_mounted_app():
    html = '<!doctype html><html><body><div id="root"></div><vite-error-overlay></vite-error-overlay></body></html>'
    assert page_mount_from_html(html)
    assert page_mount_from_html('<html><body><div id="root"><h1>Task Board</h1></div></body></html>') is None
