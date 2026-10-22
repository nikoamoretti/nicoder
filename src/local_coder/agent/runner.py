"""Run the agent for the app: questions (read-only) and change requests (working copy).

Only models that return native tool calls use the agent: the hosted models
(Nemotron 3.5 Super 120B on NVIDIA, and OrcaRouter's models). Local llama.cpp
models keep their own paths.
"""
from __future__ import annotations

import os
import secrets
import shutil
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_coder.hosted import hosted_chat
from local_coder.machine import MachineConfig, is_hosted

from .access import AccessPolicy
from .loop import AgentRun, AgentSession, ChatFn
from .prompts import ask_prompt, build_prompt
from .tools import ToolBox, project_root

ASK_STEPS = 40
ASK_SECONDS = 480
# Questions keep a smaller working context: NVIDIA answers ~80K-token prompts slowly and fails them more often.
ASK_CONTEXT_CHARS = 100_000
_OUTPUT_TOKENS = 16_384
_BYTES_PER_TOKEN = 2


def agent_capable(cfg: MachineConfig) -> bool:
    """Whether the selected model drives Local Coder's agent (it must return native tool calls)."""
    return is_hosted(cfg)


def hosted_tool_chat(cfg: MachineConfig) -> ChatFn:
    """Chat function for the loop: the hosted model, tools offered, reasoning as selected (on by default).

    OrcaRouter models keep their provider's own sampling defaults (several reasoning
    models refuse a temperature), and each run is one OrcaRouter session.
    """
    max_tokens = min(_OUTPUT_TOKENS, int(cfg.max_tokens or _OUTPUT_TOKENS))
    session = f"local-coder-{secrets.token_hex(6)}"

    def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": cfg.model_alias, "messages": messages, "max_tokens": max_tokens}
        if cfg.provider != "orcarouter" or cfg.temperature:
            body["temperature"] = cfg.temperature or 0.3
        if cfg.top_p:
            body["top_p"] = cfg.top_p
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return hosted_chat(cfg, body, thinking=cfg.reasoning != "off", session=session)

    return chat


def context_chars_for(cfg: MachineConfig) -> int:
    """Conversation size the loop keeps under, counted conservatively at two bytes per token."""
    output = min(_OUTPUT_TOKENS, int(cfg.max_tokens or _OUTPUT_TOKENS))
    return max(40_000, (int(cfg.n_ctx) - output - 1024) * _BYTES_PER_TOKEN)


def dominant_project(files: list[Path], home: Path, *, minimum: int = 3, share: float = 0.6) -> Path | None:
    """The one repo most of the read files belong to, if there clearly is one."""
    home_real = Path(os.path.realpath(home))
    roots = Counter(root for root in (project_root(path, home_real) for path in files) if root is not None)
    if not roots:
        return None
    root, count = roots.most_common(1)[0]
    return root if count >= minimum and count >= share * len(files) else None


def runs_dir() -> Path:
    """Where question transcripts are kept (change requests keep theirs in the request folder)."""
    from local_coder.engines.opencode_config import data_root

    return data_root() / "agent-runs"


@dataclass
class AskResult:
    """A question's answer plus what the agent looked at and opened."""

    answer: str
    status: str
    opened: Path | None
    read_paths: list[str] = field(default_factory=list)
    steps: int = 0
    transcript: Path | None = None
    cost_usd: float | None = None


def run_ask(
    question: str,
    *,
    project: Path | None,
    chat: ChatFn,
    context_chars: int,
    history: list[dict[str, str]] | None = None,
    on_activity: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    home: Path | None = None,
    max_steps: int = ASK_STEPS,
    seconds: int = ASK_SECONDS,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> AskResult:
    """Answer a question with read-only tools across the home folder."""
    home = home or Path.home()
    run_root = runs_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    toolbox = ToolBox(
        AccessPolicy(home=home, project=project),
        cwd=project or home,
        run_tmp=run_root / "tmp",
        cancel=cancel,
        allow_open_project=True,
    )
    session = AgentSession(
        chat=chat,
        toolbox=toolbox,
        system=ask_prompt(home=home, project=project),
        history=history,
        context_chars=min(context_chars, ASK_CONTEXT_CHARS),
        on_activity=on_activity,
        cancel=cancel,
        on_step=on_step,
    )
    run = session.ask(question, max_steps=max_steps, deadline=time.monotonic() + seconds, hurry_after=0.5)
    opened = toolbox.opened
    if opened is None and project is None and run.status == "answered":
        # The model read one repo but forgot open_project: open it so follow-ups apply there.
        opened = dominant_project(toolbox.read_files, home)
    transcript = session.save(
        run_root / "transcript.json", mode="ask", status=run.status, steps=run.steps,
        project=str(project) if project else None, opened=str(opened) if opened else None,
    )
    shutil.rmtree(run_root / "tmp", ignore_errors=True)
    return AskResult(
        answer=run.answer,
        status=run.status,
        opened=opened,
        read_paths=list(toolbox.read_paths),
        steps=run.steps,
        transcript=transcript,
        cost_usd=session.cost_usd,
    )


def build_session(
    *,
    project: Path,
    workdir: Path,
    run_tmp: Path,
    chat: ChatFn,
    context_chars: int,
    history: list[dict[str, str]] | None = None,
    on_activity: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    home: Path | None = None,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> AgentSession:
    """A session that may change the request's working copy."""
    home = home or Path.home()
    toolbox = ToolBox(
        AccessPolicy(home=home, workdir=workdir, project=project),
        cwd=workdir,
        run_tmp=run_tmp,
        cancel=cancel,
    )
    return AgentSession(
        chat=chat,
        toolbox=toolbox,
        system=build_prompt(home=home, project=project, workdir=workdir),
        history=history,
        context_chars=context_chars,
        on_activity=on_activity,
        cancel=cancel,
        on_step=on_step,
    )


__all__ = [
    "AgentRun",
    "AskResult",
    "agent_capable",
    "build_session",
    "context_chars_for",
    "hosted_tool_chat",
    "run_ask",
]
