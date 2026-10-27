"""Run one agent shell command inside a macOS sandbox (/usr/bin/sandbox-exec).

The profile allows everything except: writing anywhere but the given folders
(the working copy, a per-run temp folder, system temp folders), reading the
private places and secret files the file tools refuse, and reaching the
Keychain (the `security` CLI, Nico's `key` tool, and the securityd services).
The command gets a scrubbed environment with no API keys. Network access is
allowed so installs, builds and git fetches work.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .access import ALLOWED_INSIDE_DENIED, ALLOWED_NAMES, DENIED_DIR_NAMES, DENIED_HOME_DIRS, DENIED_NAMES

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
SHELL = "/bin/bash"
SYSTEM_WRITABLE = ("/private/tmp", "/private/var/folders", "/dev")
KEYCHAIN_SERVICES = (
    "com.apple.SecurityServer",
    "com.apple.securityd",
    "com.apple.securityd.xpc",
    "com.apple.security.agent",
    "com.apple.security.agent.login",
)
COMMAND_PATH = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
_KEEP_BYTES = 2_000_000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass
class CommandResult:
    """What one sandboxed command did. `exit_code` is None when it was stopped."""

    exit_code: int | None
    output: str
    seconds: float
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False


def available() -> bool:
    """Whether this Mac can sandbox commands."""
    return SANDBOX_EXEC.is_file()


def _quote(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _literal_regex(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "/_-" else "\\" + ch for ch in text)


def _name_regex(pattern: str, *, directory: bool = False, under: Path | None = None) -> str:
    """Case-insensitive SBPL regex for a file-name glob in the last path component.

    With `under`, only paths inside that folder match, so system files such as
    /etc/ssl/cert.pem stay readable.
    """
    prefix = "^" + _literal_regex(str(under)) + "/(.*/)?" if under is not None else "/"
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch.isalpha():
            out.append(f"[{ch.lower()}{ch.upper()}]")
        elif ch.isdigit() or ch in "-_~":
            out.append(ch)
        else:
            out.append("\\" + ch)
    tail = "(/|$)" if directory else "$"
    return '#"' + prefix + "".join(out) + tail + '"'


OUTSIDE_ACTORS = (
    "/usr/bin/open", "/usr/bin/osascript", "/bin/launchctl", "/usr/bin/shortcuts", "/usr/bin/automator",
)


def build_profile(*, home: Path, writable: list[Path], blocked_programs: list[Path] | None = None) -> str:
    """SBPL text for one command. Later rules win over earlier ones."""
    home = Path(os.path.realpath(home))
    rules = ["(version 1)", "(allow default)", '(deny file-write* (subpath "/"))']
    for system in SYSTEM_WRITABLE:
        rules.append(f"(allow file-write* (subpath {_quote(os.path.realpath(system))}))")
    # Nothing in the home folder is writable except the given folders, even when the
    # home folder itself sits inside a system temp folder (as in tests).
    rules.append(f"(deny file-write* (subpath {_quote(home)}))")
    for folder in writable:
        rules.append(f"(allow file-write* (subpath {_quote(os.path.realpath(folder))}))")
    for denied in DENIED_HOME_DIRS:
        rules.append(f"(deny file-read-data (subpath {_quote(home / denied)}))")
        rules.append(f"(deny file-write* (subpath {_quote(home / denied)}))")
    for allowed in ALLOWED_INSIDE_DENIED:
        rules.append(f"(allow file-read-data (subpath {_quote(home / allowed)}))")
    for name in sorted(DENIED_DIR_NAMES):
        regex = _name_regex(name, directory=True, under=home)
        rules.append(f"(deny file-read-data (regex {regex}))")
        rules.append(f"(deny file-write* (regex {regex}))")
    for pattern in DENIED_NAMES:
        regex = _name_regex(pattern, under=home)
        rules.append(f"(deny file-read-data (regex {regex}))")
        rules.append(f"(deny file-write* (regex {regex}))")
    for name in sorted(ALLOWED_NAMES):
        rules.append(f"(allow file-read-data (regex {_name_regex(name, under=home)}))")
    programs = blocked_programs if blocked_programs is not None else [
        Path("/usr/bin/security"), home / ".local" / "bin" / "key",
    ]
    for program in programs:
        rules.append(f"(deny process-exec (literal {_quote(program)}))")
    # Nothing may act outside the sandbox through another program: open a URL or an app (`open` can also run a
    # .command file in Terminal), AppleScript (Finder would write anywhere), Shortcuts or a launchd job.
    # 2026-10-26: a build run opened Nico's browser on a server that died with the command, then tried launchd.
    for actor in OUTSIDE_ACTORS:
        rules.append(f"(deny process-exec (literal {_quote(actor)}))")
    # The same, whatever program asks: launching apps or URLs (NSWorkspace too), Apple Events, writing his app
    # preferences through cfprefsd (`defaults write`), and reading his clipboard. Headless Chrome still runs.
    rules.append("(deny lsopen)")
    rules.append("(deny appleevent-send)")
    rules.append("(deny user-preference-write)")
    rules.append('(deny mach-lookup (global-name "com.apple.pasteboard.1"))')
    for service in KEYCHAIN_SERVICES:
        rules.append(f'(deny mach-lookup (global-name "{service}"))')
    # Signals reach only this command's own processes: a build can stop what it started, never the user's apps or
    # Nicoder's preview (2026-10-26: a change run's `kill` reached a process outside its sandbox).
    rules.append("(deny signal)")
    rules.append("(allow signal (target same-sandbox))")
    return "\n".join(rules) + "\n"


def command_env(home: Path, tmp: Path) -> dict[str, str]:
    """A clean environment: no inherited API keys, no pagers, no prompts."""
    npmrc = tmp / "npmrc"
    if not npmrc.exists():
        npmrc.write_text("", encoding="utf-8")
    user = os.environ.get("USER") or home.name
    return {
        "PATH": ":".join(COMMAND_PATH),
        "HOME": str(home),
        "USER": user,
        "LOGNAME": user,
        "SHELL": SHELL,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "dumb",
        "TMPDIR": str(tmp),
        "NO_COLOR": "1",
        "CI": "1",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "npm_config_userconfig": str(npmrc),
        "npm_config_cache": str(home / ".npm"),
        "npm_config_update_notifier": "false",
        "npm_config_fund": "false",
        "npm_config_audit": "false",
    }


def run_command(
    command: str,
    *,
    cwd: Path,
    home: Path,
    writable: list[Path],
    tmp: Path,
    timeout_s: float = 120.0,
    cancel: threading.Event | None = None,
    max_output: int = 30_000,
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    """Run `command` with bash inside the sandbox. Never raises for a failing command."""
    tmp.mkdir(parents=True, exist_ok=True)
    profile = build_profile(home=home, writable=[*writable, tmp])
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [str(SANDBOX_EXEC), "-p", profile, SHELL, "-c", command],
            cwd=str(cwd),
            env={**command_env(home, tmp), **(extra_env or {})},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return CommandResult(exit_code=None, output=f"The command could not start: {exc}", seconds=0.0)
    chunks: list[bytes] = []
    kept = [0]

    def drain() -> None:
        if proc.stdout is None:
            return
        descriptor = proc.stdout.fileno()
        while True:
            try:
                block = os.read(descriptor, 65536)
            except OSError:
                return
            if not block:
                return
            if kept[0] < _KEEP_BYTES:
                chunks.append(block)
                kept[0] += len(block)

    reader = threading.Thread(target=drain, name="agent-command-output", daemon=True)
    reader.start()
    timed_out = cancelled = False
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if time.monotonic() - started > timeout_s:
            timed_out = True
            break
        time.sleep(0.05)
    _stop_group(proc)
    reader.join(timeout=2)
    text = _ANSI.sub("", b"".join(chunks).decode("utf-8", errors="replace"))
    truncated = len(text) > max_output
    if truncated:
        head = max_output // 4
        tail = max_output - head
        text = f"{text[:head]}\n… ({len(text) - max_output} characters cut) …\n{text[-tail:]}"
    return CommandResult(
        exit_code=None if (timed_out or cancelled) else proc.returncode,
        output=text,
        seconds=round(time.monotonic() - started, 2),
        timed_out=timed_out,
        cancelled=cancelled,
        truncated=truncated,
    )


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    """End the command and anything it left running in the background."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                try:
                    os.killpg(proc.pid, 0)
                except (ProcessLookupError, PermissionError, OSError):
                    return
            time.sleep(0.05)
