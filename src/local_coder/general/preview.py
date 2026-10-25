"""Owned Vite/dev-server preview on loopback. Preview is untrusted."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from local_coder.engines.opencode_config import data_root
from local_coder.general import packages

PREVIEW_HOST = "127.0.0.1"
INSTALL_SECONDS = 180
# How a preview got its `node_modules` (the "packages" key of the record `start_preview` answers with).
PACKAGES_PRESENT = "present"
PACKAGES_CLONED = "cloned"
PACKAGES_CLONED_AND_UPDATED = "cloned+installed"
PACKAGES_INSTALLED = "installed"
_SCRIPT_SRC = re.compile(
    r"""<script[^>]+type=["']module["'][^>]+src=["']([^"']+)["']""",
    re.I,
)
_REL_IMPORT = re.compile(r"""(?:from|import)\s+['"](\.[^'"]+)['"]""")
_VITE_JSON_MESSAGE = re.compile(r'"message"\s*:\s*"((?:\\.|[^"\\])*)"')
_PREVIEW_ERROR_MARKERS = (
    "[plugin:vite:",
    "plugin:vite:react-babel",
    "plugin:vite:react-swc",
    "failed to parse source",
    "internal server error",
)
_HTML_OVERLAY_MARKERS = (
    "<vite-error-overlay",
    "</vite-error-overlay>",
)


def preview_record_path() -> Path:
    from local_coder.machine import config_dir

    return config_dir() / "preview.json"


def project_port(project_id: str, *, base: int = 5173) -> int:
    """Stable loopback port derived from the project id."""
    digest = sum(ord(ch) for ch in project_id) + len(project_id) * 17
    return 4100 + (digest % 800)


def _alive(pid: int) -> bool:
    """True while the process runs.

    A dev server this process started and has not waited for is a zombie once it exits, and `kill(pid, 0)` still finds
    a zombie: `stop_preview` then waited out its whole deadline (5 s, for every preview that replaced another and for
    every Discard) for a server that was already gone. `waitpid` collects such a child; any other pid is asked of the
    process table.
    """
    if pid <= 0:
        return False
    try:
        done, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass  # not our child
    except OSError:
        return False
    else:
        return done == 0
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def read_preview() -> dict[str, Any] | None:
    path = preview_record_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stop_preview() -> dict[str, Any]:
    """Stop only a preview this app started."""
    record = read_preview()
    if not record:
        return {"stopped": False, "detail": "no preview"}
    pid = int(record.get("pid") or 0)
    if pid and _alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.05)
        if _alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    preview_record_path().unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


def start_preview(project_dir: Path, project_id: str, *, wait_s: int = 45) -> dict[str, Any]:
    """Start `npm run dev` in the candidate, bound to a stable loopback port."""
    existing = read_preview()
    if existing and _alive(int(existing.get("pid") or 0)):
        url = str(existing.get("url") or "").strip()
        same = existing.get("project_id") == project_id and existing.get("dir") == str(project_dir)
        if same and existing.get("ok") and url:
            health = check_preview_health(url, project_dir)
            if health.get("ok"):
                return {"started": False, **existing, "preview_health": health}
        stop_preview()
        freed = time.monotonic() + 4
        while time.monotonic() < freed and _port_open(PREVIEW_HOST, project_port(project_id)):
            time.sleep(0.05)
    port = project_port(project_id)
    if _port_open(PREVIEW_HOST, port):
        return {
            "ok": False,
            "code": "port_busy",
            "detail": f"Port {port} is already in use, so the preview was not started.",
            "url": f"http://{PREVIEW_HOST}:{port}/",
        }
    npm = _which("npm")
    if npm is None:
        return {"ok": False, "code": "npm_missing", "detail": "npm was not found on this Mac."}
    if not (project_dir / "package.json").exists():
        return {"ok": False, "code": "no_package", "detail": "This project has no package.json yet."}
    log = data_root() / "logs" / f"preview-{project_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    how = PACKAGES_PRESENT
    if not (project_dir / "node_modules").is_dir():
        how, failure = _provide_packages(npm, project_dir, project_id, log)
        if failure is not None:
            return failure
    env = os.environ.copy()
    env["BROWSER"] = "none"
    env["HOST"] = PREVIEW_HOST
    env["PORT"] = str(port)
    argv = [npm, "run", "dev", "--", "--host", PREVIEW_HOST, "--port", str(port), "--strictPort"]
    with log.open("ab") as handle:
        proc = subprocess.Popen(
            argv,
            cwd=str(project_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    url = f"http://{PREVIEW_HOST}:{port}/"
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return {"ok": False, "code": "preview_exited", "detail": f"Dev server exited. See {log}."}
        if _http_ok(url):
            record = {
                "ok": True,
                "started": True,
                "pid": proc.pid,
                "url": url,
                "port": port,
                "dir": str(project_dir),
                "project_id": project_id,
                "log": str(log),
                "packages": how,
            }
            write_preview_record(record)
            return record
        time.sleep(0.1)
    stop_preview()
    return {"ok": False, "code": "preview_timeout", "detail": f"Preview did not start within {wait_s}s."}


def _provide_packages(npm: str, project_dir: Path, project_id: str, log: Path) -> tuple[str, dict[str, Any] | None]:
    """Give `project_dir` its `node_modules`: a donor's clone when there is one, then `npm install` unless it was exact.

    Answers how they came (`PACKAGES_*`) and, when they could not, the refusal `start_preview` returns. Reusing
    packages is only a shortcut: whatever goes wrong with a donor, the plain `npm install` runs as it always did, and a
    clone that npm cannot finish is thrown away and installed from scratch. A failed install leaves no `node_modules`
    behind, which the next start would trust.
    """
    try:
        adoption = packages.adopt_packages(project_dir, project_id)
    except Exception:  # noqa: BLE001 - a bad donor must never stop a preview
        adoption = packages.Adoption(packages.NONE)
    _note(log, adoption.describe())
    if adoption.how == packages.EXACT:
        return PACKAGES_CLONED, None
    failure = _npm_install(npm, project_dir)
    if adoption.how == packages.CLOSE:
        if failure is None:
            return PACKAGES_CLONED_AND_UPDATED, None
        _remove_packages(project_dir)
        _note(log, "npm could not finish the clone, so the packages are installed from scratch")
        failure = _npm_install(npm, project_dir)
    if failure is not None:
        _remove_packages(project_dir)
    return PACKAGES_INSTALLED, failure


def _npm_install(npm: str, project_dir: Path) -> dict[str, Any] | None:
    """Run `npm install` in `project_dir`: None when it worked, else the refusal `start_preview` returns."""
    try:
        install = subprocess.run(
            [npm, "install", "--no-fund", "--no-audit"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=INSTALL_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "install_failed", "detail": f"npm install took longer than {INSTALL_SECONDS} s."}
    except OSError as exc:
        return {"ok": False, "code": "install_failed", "detail": f"npm could not run: {exc.strerror or exc}."}
    if install.returncode != 0:
        return {"ok": False, "code": "install_failed", "detail": (install.stderr or install.stdout)[-400:]}
    return None


def _remove_packages(project_dir: Path) -> None:
    """Delete `project_dir`'s `node_modules` (a folder, or a link to one), whatever state it is in."""
    modules = project_dir / packages.MODULES
    if modules.is_symlink():
        modules.unlink(missing_ok=True)
    else:
        shutil.rmtree(modules, ignore_errors=True)


def _note(log: Path, line: str) -> None:
    """Add one line to the preview's log, so a slow or odd start can be explained afterwards."""
    try:
        with log.open("ab") as handle:
            handle.write(f"[local-coder] {line}\n".encode())
    except OSError:
        pass


def _http_ok(url: str) -> bool:
    """True when the preview process answers. A 200 page is not a compile check."""
    try:
        parsed = urlparse(url)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            return False
        with httpx.Client(trust_env=False, timeout=1.0) as client:
            response = client.get(url)
        return response.status_code < 500
    except (httpx.HTTPError, OSError):
        return False


def preview_error_from_response(
    status: int,
    body: str,
    *,
    content_type: str = "",
    url: str = "",
) -> str | None:
    """Return an error excerpt when Vite/Babel failed, else None."""
    text = body or ""
    if status >= 400:
        return _error_excerpt(text) or f"HTTP {status}"
    if _is_vite_internal(url):
        if _looks_like_html(text, content_type):
            return "The Vite client script returned HTML, so the page cannot mount."
        return None
    if _looks_like_html(text, content_type) and _looks_like_module_url(url):
        return "A preview module returned HTML instead of JavaScript, so the page cannot mount."
    lowered = text.lower()
    if any(marker in lowered for marker in _HTML_OVERLAY_MARKERS):
        return _error_excerpt(text)
    if any(marker in lowered for marker in _PREVIEW_ERROR_MARKERS):
        return _error_excerpt(text)
    return None


def check_preview_health(
    url: str,
    project_dir: Path | None = None,
    *,
    client: httpx.Client | None = None,
    wait_s: float = 4.0,
) -> dict[str, Any]:
    """Fetch the preview page and entry modules. Overlay/HTTP errors fail."""
    parsed = urlparse(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return {"ok": False, "code": "bad_url", "detail": "Preview URL is not loopback."}
    own_client = client is None
    http = client or httpx.Client(trust_env=False, timeout=2.0, follow_redirects=True)
    try:
        page = _get(http, url)
        if page is None:
            return {"ok": False, "code": "preview_unreachable", "detail": "The preview did not answer."}
        page_error = preview_error_from_response(
            page.status_code, page.text, content_type=page.headers.get("content-type", ""), url=url,
        )
        if page_error:
            return {"ok": False, "code": "vite_overlay", "detail": page_error}
        targets = _entry_urls(url, page.text, project_dir)
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            failed: dict[str, Any] | None = None
            for target in targets:
                response = _get(http, target)
                if response is None:
                    failed = {"ok": False, "code": "preview_unreachable", "detail": f"No response from {target}."}
                    break
                error = preview_error_from_response(
                    response.status_code,
                    response.text,
                    content_type=response.headers.get("content-type", ""),
                    url=target,
                )
                if error:
                    code = "vite_client_html" if "cannot mount" in error else "vite_overlay"
                    failed = {"ok": False, "code": code, "detail": error}
                    break
            if failed is None:
                if client is None:
                    html = _headless_dom(url)
                    mount_error = page_mount_from_html(html) if html else None
                    if mount_error:
                        return {"ok": False, "code": "empty_root", "detail": mount_error}
                return {"ok": True, "code": "ok", "detail": "Preview modules loaded."}
            if time.monotonic() >= deadline:
                return failed
            time.sleep(0.2)
        return {"ok": False, "code": "preview_unreachable", "detail": "The preview did not answer."}
    finally:
        if own_client:
            http.close()


def write_preview_record(record: dict[str, Any]) -> None:
    """Persist the current owned preview record."""
    path = preview_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def _entry_urls(base: str, html: str, project_dir: Path | None) -> list[str]:
    urls: list[str] = []
    for match in _SCRIPT_SRC.finditer(html or ""):
        urls.append(urljoin(base, match.group(1)))
    if project_dir is not None:
        urls.extend(_reachable_src_urls(base, project_dir))
    seen: set[str] = set()
    unique: list[str] = []
    for item in urls:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique or [urljoin(base, "/src/App.tsx")]


def _reachable_src_urls(base: str, project_dir: Path) -> list[str]:
    """Entry modules plus relative imports. Unused broken files stay out of this fetch."""
    stack = [
        rel for rel in ("src/main.tsx", "src/main.jsx", "src/main.ts", "src/App.tsx", "src/App.jsx")
        if (project_dir / rel).is_file()
    ]
    seen: set[str] = set()
    urls: list[str] = []
    while stack and len(seen) < 24:
        rel = stack.pop()
        if rel in seen:
            continue
        seen.add(rel)
        urls.append(urljoin(base, "/" + rel))
        for child in _imported_rels(project_dir, rel):
            if child not in seen:
                stack.append(child)
    return urls


def _imported_rels(project_dir: Path, rel: str) -> list[str]:
    path = project_dir / rel
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    found: list[str] = []
    base = path.parent
    root = project_dir.resolve()
    for spec in _REL_IMPORT.findall(text):
        dest = (base / spec)
        resolved: Path | None = None
        if dest.is_file():
            resolved = dest
        else:
            for suffix in (".tsx", ".ts", ".jsx", ".js"):
                candidate = Path(str(dest) + suffix)
                if candidate.is_file():
                    resolved = candidate
                    break
            if resolved is None and dest.is_dir():
                for index in ("index.tsx", "index.ts", "index.jsx", "index.js"):
                    if (dest / index).is_file():
                        resolved = dest / index
                        break
        if resolved is None:
            continue
        try:
            found.append(resolved.resolve().relative_to(root).as_posix())
        except ValueError:
            continue
    return found


def _get(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        parsed = urlparse(url)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            return None
        return client.get(url)
    except (httpx.HTTPError, OSError):
        return None


def _error_excerpt(body: str) -> str:
    match = _VITE_JSON_MESSAGE.search(body or "")
    if match:
        try:
            raw = bytes(match.group(1), "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            raw = match.group(1).replace("\\n", "\n")
        compact = " ".join(raw.split())
        if compact:
            return compact[:400]
    compact = " ".join((body or "").split())
    return compact[:400]


def _looks_like_html(body: str, content_type: str = "") -> bool:
    ctype = (content_type or "").lower()
    if "text/html" in ctype:
        return True
    stripped = (body or "").lstrip().lower()
    return stripped.startswith("<!doctype html") or stripped.startswith("<html")


def _is_vite_internal(url: str) -> bool:
    path = urlparse(url or "").path
    return (
        path == "/@vite/client"
        or path.startswith("/@id/")
        or path.startswith("/@react-refresh")
        or "/node_modules/" in path
        or path.startswith("/@fs/")
    )


def page_mount_from_html(html: str | None) -> str | None:
    """Return why a rendered page is not mounted, or None when #root has content."""
    text = html or ""
    if not text.strip():
        return "The preview page did not render."
    lowered = text.lower()
    if "<vite-error-overlay" in lowered:
        return _error_excerpt(text) or "The preview showed a Vite error overlay."
    if re.search(r'<div id=["\']root["\']>\s*</div>', text, re.I):
        return "The preview page root is empty, so this is not a working application."
    return None


def _headless_dom(url: str) -> str | None:
    """Render the loopback preview in Chrome. None when Chrome cannot run."""
    parsed = urlparse(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    chrome = _chrome_bin()
    if chrome is None:
        return None
    try:
        proc = subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--virtual-time-budget=4000",
                "--dump-dom",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout or None


def _chrome_bin() -> str | None:
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac.is_file():
        return str(mac)
    return _which("google-chrome") or _which("chromium") or _which("chrome")


def _looks_like_module_url(url: str) -> bool:
    path = urlparse(url or "").path.lower()
    return path.endswith((".tsx", ".ts", ".jsx", ".js", ".mjs"))


def _which(name: str) -> str | None:
    return shutil.which(name)
