"""Isolated OpenCode configuration owned by Local Coder.

Never reads or writes the user's personal ~/.config/opencode or
~/.local/share/opencode. The process HOME is redirected to an app-owned
directory so those layers cannot merge in.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any

from .opencode_pin import PINNED_SHA256, PINNED_VERSION, SYSTEM_BINARY, pinned_binary

PROVIDER_ID = "llama.cpp"
DEFAULT_MODEL_ID = "Qwen3.5-9B-Q4_K_M"
DEFAULT_CONTEXT = 32768
DEFAULT_OUTPUT = 2048

# Cloud and hosted providers that must stay disabled even if a project file
# tries to re-enable them. OPENCODE_CONFIG_CONTENT is applied after project
# config and wins on conflicting keys.
DISABLED_PROVIDERS = [
    "openai",
    "anthropic",
    "google",
    "gemini",
    "opencode",
    "openrouter",
    "groq",
    "mistral",
    "xai",
    "together",
    "deepseek",
    "azure",
    "amazon-bedrock",
    "vertex",
    "copilot",
    "github",
    "gitlab",
    "huggingface",
    "nvidia",
]

ISOLATION_ENV_KEYS = (
    "OPENCODE_TEST_HOME",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_PURE",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS",
    "OPENCODE_DISABLE_CLAUDE_CODE",
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT",
    "OPENCODE_DISABLE_PROJECT_CONFIG",
    "OPENCODE_DISABLE_MODELS_FETCH",
    "OPENCODE_DISABLE_SHARE",
    "OPENCODE_DISABLE_AUTOUPDATE",
)

CLOUD_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "XAI_API_KEY",
    "TOGETHER_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENCODE_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
)


def data_root() -> Path:
    """App-owned persistent data (not the user's OpenCode profile)."""
    override = os.environ.get("LOCAL_CODER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "local-coder"


def engine_home() -> Path:
    """Fake HOME for the OpenCode process."""
    return data_root() / "engine-home"


def secret_path() -> Path:
    """HTTP basic-auth password for the loopback OpenCode server."""
    from local_coder.machine import config_dir

    return config_dir() / "engine-secret"


def ensure_secret() -> str:
    """Create or load the app-owned server password. Never log the value."""
    path = secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    value = secrets.token_urlsafe(32)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return value


def ensure_pinned_binary() -> Path:
    """Copy the verified Homebrew binary into the app-owned location."""
    dest = pinned_binary()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and _sha256(dest) == PINNED_SHA256:
        return dest
    if not SYSTEM_BINARY.exists():
        raise FileNotFoundError(
            f"Pinned OpenCode {PINNED_VERSION} is not installed at {SYSTEM_BINARY}."
        )
    digest = _sha256(SYSTEM_BINARY)
    if digest != PINNED_SHA256:
        raise RuntimeError(
            f"OpenCode at {SYSTEM_BINARY} has SHA-256 {digest}, expected {PINNED_SHA256} "
            f"(version {PINNED_VERSION}). Refusing to use an unpinned binary."
        )
    tmp = dest.with_suffix(".tmp")
    shutil.copy2(SYSTEM_BINARY, tmp)
    tmp.chmod(stat.S_IRWXU)
    os.replace(tmp, dest)
    if _sha256(dest) != PINNED_SHA256:
        dest.unlink(missing_ok=True)
        raise RuntimeError("OpenCode copy failed the checksum after install.")
    return dest


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def opencode_config(
    *,
    llama_base_url: str,
    model_id: str = DEFAULT_MODEL_ID,
    context: int = DEFAULT_CONTEXT,
    output: int = DEFAULT_OUTPUT,
    max_steps: int = 120,
) -> dict[str, Any]:
    """Effective OpenCode config: local llama.cpp only, no sharing or plugins."""
    model_ref = f"{PROVIDER_ID}/{model_id}"
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model_ref,
        "small_model": model_ref,
        "autoupdate": False,
        "share": "disabled",
        "enabled_providers": [PROVIDER_ID],
        "disabled_providers": list(DISABLED_PROVIDERS),
        "provider": {
            PROVIDER_ID: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Local llama.cpp",
                "options": {
                    "baseURL": llama_base_url.rstrip("/") + "/v1",
                    "apiKey": "local-coder",
                },
                "models": {
                    model_id: {
                        "name": model_id,
                        "tool_call": True,
                        "limit": {"context": context, "output": output},
                    }
                },
            }
        },
        "permission": {
            "*": "allow",
            "webfetch": "deny",
            "websearch": "deny",
            "external_directory": "deny",
            "task": "deny",
            "doom_loop": "ask",
            "read": {
                "*": "allow",
                "*.env": "deny",
                "*.env.*": "deny",
                "*.pem": "deny",
                "*.key": "deny",
            },
            "bash": {
                "*": "allow",
                "git push *": "deny",
                "git commit *": "deny",
                "sudo *": "deny",
                "ssh *": "deny",
            },
        },
        "agent": {
            "build": {
                "model": model_ref,
                "maxSteps": max_steps,
                "permission": {"task": "deny", "webfetch": "deny", "websearch": "deny"},
            },
            "plan": {
                "model": model_ref,
                "permission": {"task": "deny", "webfetch": "deny", "websearch": "deny"},
            },
        },
        "instructions": [],
        "plugin": [],
        "mcp": {},
        "skills": {"paths": [], "urls": []},
        "compaction": {"auto": True},
    }


def write_engine_home(config: dict[str, Any]) -> Path:
    """Materialize the isolated HOME tree and write the global OpenCode config."""
    home = engine_home()
    cfg_dir = home / ".config" / "opencode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (home / ".local" / "share" / "opencode").mkdir(parents=True, exist_ok=True)
    (home / ".cache" / "opencode").mkdir(parents=True, exist_ok=True)
    _clear_unwanted_discovery(home)
    path = cfg_dir / "opencode.json"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    agents = cfg_dir / "AGENTS.md"
    agents.write_text(
        "Local Coder engine profile. Work only in this project folder.\n"
        "Do not use host skills, cloud providers, or parent-directory instructions.\n"
        "Clipped tool results can be continued with "
        "`local-coder-observe <observation_id> <offset>` via the bash tool.\n",
        encoding="utf-8",
    )
    _install_observe_cli(home)
    return path


def isolated_env(
    *,
    extra: dict[str, str] | None = None,
    path_entries: list[str] | None = None,
) -> dict[str, str]:
    """Minimal environment that cannot see the user's OpenCode profile or cloud keys."""
    home = engine_home()
    home.mkdir(parents=True, exist_ok=True)
    path = [
        str(home / "bin"),
        str(pinned_binary().parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        str(Path.home() / ".local" / "bin"),
    ]
    if path_entries:
        path = path_entries + path
    env = {
        "HOME": str(home),
        "OPENCODE_TEST_HOME": str(home),
        "USER": os.environ.get("USER", "local-coder"),
        "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "local-coder")),
        "TMPDIR": str(home / "tmp"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "OPENCODE_CONFIG": str(home / ".config" / "opencode" / "opencode.json"),
        "OPENCODE_CONFIG_DIR": str(home / ".config" / "opencode"),
        "OPENCODE_SERVER_USERNAME": "local-coder",
        "OPENCODE_SERVER_PASSWORD": ensure_secret(),
        "OPENCODE_PURE": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_SHARE": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_ENABLE_EXA": "0",
        "OPENCODE_ENABLE_PARALLEL": "0",
        "PATH": ":".join(path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]) + (
            os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
        ),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        "TERM": "dumb",
    }
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    if extra:
        env.update(extra)
    return env


def overlay_content(config: dict[str, Any]) -> str:
    """Late-binding JSON that overrides a candidate-written project config."""
    overlay = {
        "model": config["model"],
        "small_model": config["small_model"],
        "autoupdate": False,
        "share": "disabled",
        "enabled_providers": [PROVIDER_ID],
        "disabled_providers": list(DISABLED_PROVIDERS),
        "plugin": [],
        "mcp": {},
        "skills": {"paths": [], "urls": []},
        "provider": config["provider"],
        "instructions": [],
        "permission": {
            "webfetch": "deny",
            "websearch": "deny",
            "external_directory": "deny",
            "task": "deny",
        },
    }
    return json.dumps(overlay)


def _clear_unwanted_discovery(home: Path) -> None:
    """Remove leftover skill/plugin trees from the app-owned fake HOME only."""
    for relative in (
        ".claude",
        ".agents",
        ".config/opencode/skills",
        ".config/opencode/skill",
        ".config/opencode/plugin",
        ".config/opencode/plugins",
        ".config/opencode/command",
        ".config/opencode/commands",
    ):
        path = home / relative
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _install_observe_cli(home: Path) -> Path:
    """Install the bounded-observe helper on the isolated PATH."""
    dest = home / "bin" / "local-coder-observe"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_root = Path(__file__).resolve().parents[2]
    dest.write_text(
        "#!/bin/sh\n"
        f'export PYTHONPATH="{src_root}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        'exec python3 -m local_coder.engines.observe_cli "$@"\n',
        encoding="utf-8",
    )
    dest.chmod(stat.S_IRWXU)
    return dest
