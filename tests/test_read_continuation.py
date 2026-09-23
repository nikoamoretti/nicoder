"""The recorded 9efd XML read must continue to an applied, checked edit on the production path."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from local_coder.general.budgets import CREATE
from local_coder.general.coordinator import _GenerationLedger, request_dir, run_general_request
from local_coder.general.direct_write import (
    CompletionResult,
    ReplyOutcome,
    complete_files,
    prompt_budget_chars,
)
from local_coder.general.harness import line_hash, parse_actions, read_files
from local_coder.general.repair_context import build_repair_state

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence/bonsai-taskboard-2026-09-23/last_reply.txt"
RECORDED_SHA256 = "ecac82a26007bed108e244fbdd9a6515e7347b8299c9379c5a1775aa7b6ed74d"
BROKEN_IMPORT = 'import { helper } from "./gone";'
TYPES = (
    'export type ColumnId = "todo" | "in-progress" | "done";\n\n'
    "export interface Task {\n  id: string;\n  title: string;\n  column: ColumnId;\n}\n"
)
TASK_ITEM = (
    'import type { Task } from "./types";\n\n'
    "export default function TaskItem({ task }: { task: Task }) {\n"
    "  return <li>{task.title}</li>;\n}\n"
)
APP = (
    'import TaskItem from "./TaskItem";\n'
    'import type { Task } from "./types";\n\n'
    "const tasks: Task[] = [];\n\n"
    "export default function App() {\n"
    "  return <ul>{tasks.map((task) => <TaskItem key={task.id} task={task} />)}</ul>;\n}\n"
)


def recorded_reply() -> str:
    return EVIDENCE.read_bytes().decode("utf-8")


def recorded_completion() -> CompletionResult:
    return CompletionResult(
        text=recorded_reply(),
        finish_reason="stop",
        usage={"prompt_tokens": 3369, "completion_tokens": 72},
        request_identity={"id": "chatcmpl-8lqNkC3SQGr0dumwtR69N2WhvG3M3Fae"},
    )


def bonsai_config(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(
        'model_alias = "Ternary-Bonsai-2-27B-PQ2_0"\nn_ctx = 8192\nmax_tokens = 3072\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCAL_CODER_CONFIG_DIR", str(config))
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))


def make_project(tmp_path: Path) -> Path:
    user = tmp_path / "task-board"
    (user / "src").mkdir(parents=True)
    (user / "src" / "types.ts").write_text(TYPES, encoding="utf-8")
    (user / "src" / "TaskItem.tsx").write_text(TASK_ITEM, encoding="utf-8")
    (user / "src" / "App.tsx").write_text(APP, encoding="utf-8")
    return user


def broken_history(user: Path, request_id: str = "req-hist") -> dict:
    """A recorded candidate with a real source-check failure, like faf7/9efd."""

    def write_broken(_prompt: str) -> str:
        return f"```src/TaskItem.tsx\n{BROKEN_IMPORT}\n{TASK_ITEM}```\n"

    return run_general_request(
        user, "Finish this task board", request_id=request_id, fake_complete=write_broken,
        start_preview_after=False, generation_limit=1, model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )


SECOND_RUN = EVIDENCE.parents[1] / "bonsai-taskboard-2026-09-23-read-cont"
SECOND_RUN_SHA256 = {
    "turn01-reply.txt": "b668ca4e22efa7f0dad97d754a86b0a86593291b233ae251ddd86a266e734d2b",
    "turn02-reply.txt": "88d4e906c79e62aaf337876a3ba1fdf155ba0243eaf5412cffe781bb4dd2dfc6",
}


def test_7d0e_inline_path_reads_are_recognized():
    for name, digest in SECOND_RUN_SHA256.items():
        raw = (SECOND_RUN / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        parsed = parse_actions(raw.decode("utf-8"))
        assert parsed.reads == ["src/App.tsx", "src/TaskBoard.tsx", "src/TaskItem.tsx", "src/types.ts"]
    assert parse_actions('<function=read path="src/App.tsx">\n</function>').reads == ["src/App.tsx"]
    assert parse_actions("<function=reader src/App.tsx\n</function>").reads == []
    assert parse_actions("<function=read ../secret.ts\n</function>").reads == []


def test_7d0e_reply_continues_to_an_edit_in_complete_files(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)
    (root / "src" / "types.ts").write_text(TYPES, encoding="utf-8")
    (root / "src" / "TaskItem.tsx").write_text(f"{BROKEN_IMPORT}\n{TASK_ITEM}", encoding="utf-8")
    (root / "src" / "TaskBoard.tsx").write_text("export const TaskBoard = () => null;\n", encoding="utf-8")
    (root / "src" / "App.tsx").write_text(APP, encoding="utf-8")
    state = build_repair_state("Finish", {"detail": "src/TaskItem.tsx: cannot resolve import './gone'"}, root)
    replies = [(SECOND_RUN / "turn01-reply.txt").read_text(encoding="utf-8"),
               f"```edit src/TaskItem.tsx\n-{line_hash(BROKEN_IMPORT)}\n```\n"]
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return replies[len(prompts) - 1]

    outcomes: list[ReplyOutcome] = []
    written, _raw, _problems = complete_files(
        root, "Finish", {"name": "t"}, complete=complete, repair=state, outcomes=outcomes, views={},
    )
    assert [item.kind for item in outcomes] == ["read", "applied"]
    assert "You asked to read src/App.tsx, src/TaskBoard.tsx, src/TaskItem.tsx, src/types.ts" in prompts[1]
    assert "Already shown in full, so not repeated: src/TaskItem.tsx" in prompts[1]
    assert f"{line_hash('export const TaskBoard = () => null;')}|" in prompts[1]
    assert written == ["src/TaskItem.tsx"]


def test_no_action_feedback_names_the_read_fence():
    status = ReplyOutcome("no_action").status()
    assert "read fence per file" in status
    assert "tool_call XML is only understood" not in status


def test_recorded_reply_is_byte_exact_and_parses_as_three_reads():
    raw = EVIDENCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == RECORDED_SHA256
    parsed = parse_actions(raw.decode("utf-8"))
    assert parsed.reads == ["src/types.ts", "src/TaskItem.tsx", "src/App.tsx"]
    assert parsed.writes == {} and parsed.edits == []


def test_recorded_read_continues_to_an_applied_edit_in_complete_files(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)
    (root / "src" / "types.ts").write_text(TYPES, encoding="utf-8")
    item_lines = [f"  const row{n} = {n};" for n in range(1, 121)]
    item_lines[74] = '        e.dataTransfer?.effectAllowed = "move";'
    (root / "src" / "TaskItem.tsx").write_text("\n".join(item_lines) + "\n", encoding="utf-8")
    app_lines = [f"  const cell{n} = {n};" for n in range(1, 301)]
    app_lines[290] = "                          aria-label={`Move to ${COLUMN_LABELS[next]}`}"
    (root / "src" / "App.tsx").write_text("\n".join(app_lines) + "\n", encoding="utf-8")
    detail = (
        "src/App.tsx(291,64): error TS2304: Cannot find name 'next'.\n"
        "src/TaskItem.tsx(75,9): error TS2779: The left-hand side of an assignment expression "
        "may not be an optional property access."
    )
    state = build_repair_state(
        "Finish this task board",
        {"detail": detail, "build": "fail", "preview_health": {"code": "build_failed", "detail": detail}},
        root,
    )
    prompts: list[str] = []
    fixed = '        if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";'

    def complete(prompt: str) -> CompletionResult | str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return recorded_completion()
        return (
            "Guard the optional access before assigning.\n\n"
            f"```edit src/TaskItem.tsx\n-{line_hash(item_lines[74])}\n+{fixed}\n```\n"
        )

    outcomes: list[ReplyOutcome] = []
    written, _raw, problems = complete_files(
        root, "Finish this task board", {"name": "task-board"}, complete=complete,
        repair=state, outcomes=outcomes, views={},
    )
    assert [item.kind for item in outcomes] == ["read", "applied"]
    assert outcomes[0].completion["usage"]["completion_tokens"] == 72
    assert written == ["src/TaskItem.tsx"] and problems == []
    second = prompts[1]
    assert "TS2779" in second and "Cannot find name 'next'" in second
    assert "You asked to read src/types.ts, src/TaskItem.tsx, src/App.tsx" in second
    assert "not applied" not in second
    assert f"{line_hash('export interface Task {')}|export interface Task {{" in second
    assert f">{line_hash(item_lines[74])}|{item_lines[74]}" in second
    assert f">{line_hash(app_lines[290])}|{app_lines[290]}" in second
    assert all(len(prompt) <= prompt_budget_chars() for prompt in prompts)
    assert fixed in (root / "src" / "TaskItem.tsx").read_text(encoding="utf-8")


def test_recorded_read_through_coordinator_continuation_runs_real_checks(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    user = make_project(tmp_path)
    before = {p.name: p.read_bytes() for p in (user / "src").iterdir()}
    hist = broken_history(user)
    assert hist["verification"]["stopped_incomplete"] is True
    prompts: list[str] = []

    def complete(prompt: str) -> CompletionResult | str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return recorded_completion()
        return f"Remove the missing import.\n\n```edit src/TaskItem.tsx\n-{line_hash(BROKEN_IMPORT)}\n```\n"

    result = run_general_request(
        user, "Finish this task board", request_id="req-cont", fake_complete=complete,
        start_preview_after=False, generation_limit=5, continue_from="req-hist",
        model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )
    root = request_dir(result["project_id"], "req-cont")
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    assert [row["kind"] for row in meta["replies"]] == ["read", "applied"]
    assert meta["generation_ledger"]["generations"] == 2
    assert meta["stop_reason"] == "checks_passed"
    assert [check["ok"] for check in meta["checks"]] == [False, True]
    assert "cannot resolve import './gone'" in prompts[0]
    assert "cannot resolve import './gone'" in prompts[1]
    assert f">{line_hash(BROKEN_IMPORT)}|{BROKEN_IMPORT}" in prompts[1] or (
        f"{line_hash(BROKEN_IMPORT)}|{BROKEN_IMPORT}" in prompts[1]
    )
    assert f"{line_hash('export interface Task {')}|export interface Task {{" in prompts[1]
    assert (root / "turns" / "01-reply.txt").read_bytes() == EVIDENCE.read_bytes()
    assert (root / "turns" / "02-prompt.txt").read_text(encoding="utf-8") == prompts[1]
    assert BROKEN_IMPORT not in (root / "candidate" / "src" / "TaskItem.tsx").read_text(encoding="utf-8")
    assert result["verification"]["typecheck"] == "pass"
    assert {p.name: p.read_bytes() for p in (user / "src").iterdir()} == before
    hist_item = request_dir(result["project_id"], "req-hist") / "candidate" / "src" / "TaskItem.tsx"
    assert BROKEN_IMPORT in hist_item.read_text(encoding="utf-8")


def test_applied_edit_then_failed_check_says_it_was_applied(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    user = make_project(tmp_path)
    broken_history(user)
    prompts: list[str] = []
    comment = "// the task row"

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return (
                f"```edit src/TaskItem.tsx\n-{line_hash(BROKEN_IMPORT)}\n+{BROKEN_IMPORT}\n+{comment}\n```\n"
            )
        return f"```edit src/TaskItem.tsx\n-{line_hash(BROKEN_IMPORT)}\n```\n"

    result = run_general_request(
        user, "Finish this task board", request_id="req-applied", fake_complete=complete,
        start_preview_after=False, generation_limit=5, continue_from="req-hist",
        model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )
    second = prompts[1]
    assert "Your previous reply was applied: it changed src/TaskItem.tsx" in second
    assert "not applied" not in second
    assert "cannot resolve import './gone'" in second
    assert f"{line_hash(comment)}|{comment}" in second
    meta = json.loads((request_dir(result["project_id"], "req-applied") / "meta.json").read_text())
    assert [row["kind"] for row in meta["replies"]] == ["applied", "applied"]
    assert meta["stop_reason"] == "checks_passed"


def test_rejected_edit_is_reported_as_not_applied(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    user = make_project(tmp_path)
    broken_history(user)
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "```edit src/TaskItem.tsx\n-ffffffff\n+nothing\n```\n"
        return f"```edit src/TaskItem.tsx\n-{line_hash(BROKEN_IMPORT)}\n```\n"

    result = run_general_request(
        user, "Finish this task board", request_id="req-rejected", fake_complete=complete,
        start_preview_after=False, generation_limit=5, continue_from="req-hist",
        model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )
    assert "was not applied, so the candidate did not change" in prompts[1]
    assert "edit hash does not match the current file" in prompts[1]
    assert "Previous reply (not applied):" in prompts[1]
    meta = json.loads((request_dir(result["project_id"], "req-rejected") / "meta.json").read_text())
    assert [row["kind"] for row in meta["replies"]] == ["rejected", "applied"]
    assert [check["ok"] for check in meta["checks"]] == [False, True]


def test_repeated_identical_read_stops_only_after_the_first_read_was_served(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    user = make_project(tmp_path)
    broken_history(user)
    prompts: list[str] = []

    def complete(prompt: str) -> CompletionResult:
        prompts.append(prompt)
        return recorded_completion()

    result = run_general_request(
        user, "Finish this task board", request_id="req-repeat", fake_complete=complete,
        start_preview_after=False, generation_limit=5, continue_from="req-hist",
        model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )
    meta = json.loads((request_dir(result["project_id"], "req-repeat") / "meta.json").read_text())
    assert [row["kind"] for row in meta["replies"]] == ["read", "repeat_read", "repeat_read"]
    assert meta["stop_reason"] == "no_progress"
    assert meta["generation_ledger"]["generations"] == 3
    assert "already shown" in prompts[2]
    assert len(meta["checks"]) == 1


def test_truncated_repair_reply_gets_feedback_then_can_recover(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    user = make_project(tmp_path)
    broken_history(user)
    prompts: list[str] = []

    def complete(prompt: str) -> CompletionResult | str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return CompletionResult(
                text=f"```edit src/TaskItem.tsx\n-{line_hash(BROKEN_IMPORT)}\n```\n",
                finish_reason="length", truncated=True,
            )
        return f"```edit src/TaskItem.tsx\n-{line_hash(BROKEN_IMPORT)}\n```\n"

    result = run_general_request(
        user, "Finish this task board", request_id="req-trunc-repair", fake_complete=complete,
        start_preview_after=False, generation_limit=5, continue_from="req-hist",
        model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )
    root = request_dir(result["project_id"], "req-trunc-repair")
    meta = json.loads((root / "meta.json").read_text())
    assert [row["kind"] for row in meta["replies"]] == ["truncated", "applied"]
    assert "cut off at the output limit and was not applied" in prompts[1]
    assert meta["stop_reason"] == "checks_passed"


def test_cancel_during_repair_drops_the_late_edit(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    user = make_project(tmp_path)
    broken_history(user)
    cancel = threading.Event()

    def complete(_prompt: str) -> str:
        cancel.set()
        return f"```edit src/TaskItem.tsx\n-{line_hash(BROKEN_IMPORT)}\n```\n"

    result = run_general_request(
        user, "Finish this task board", request_id="req-cancel-repair", fake_complete=complete,
        start_preview_after=False, generation_limit=5, continue_from="req-hist",
        cancel_event=cancel, model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )
    root = request_dir(result["project_id"], "req-cancel-repair")
    assert BROKEN_IMPORT in (root / "candidate" / "src" / "TaskItem.tsx").read_text(encoding="utf-8")
    meta = json.loads((root / "meta.json").read_text())
    assert meta["stop_reason"] == "cancelled"


def test_xml_reads_cannot_escape_the_candidate(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    outside = tmp_path / "outside-secret.ts"
    outside.write_text("export const secret = 'do-not-leak';\n", encoding="utf-8")
    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)
    (root / "src" / "TaskItem.tsx").write_text(f"{BROKEN_IMPORT}\n{TASK_ITEM}", encoding="utf-8")
    (root / "src" / "link.ts").symlink_to(outside)
    escape = (
        "<tool_call>\n<function=read>\n../outside-secret.ts\n</function>\n</tool_call>\n"
        "<tool_call>\n<function=read>\n/etc/passwd\n</function>\n</tool_call>\n"
        "<tool_call>\n<function=bash>\nrm -rf /\n</function>\n</tool_call>\n"
    )
    assert parse_actions(escape).reads == []
    assert "do-not-leak" not in read_files(root, ["src/link.ts"])
    detail = "src/TaskItem.tsx: cannot resolve import './gone'"
    state = build_repair_state("Finish", {"detail": detail, "preview_health": {"code": "source_check"}}, root)
    prompts: list[str] = []
    replies = [
        "<tool_call>\n<function=read>\nsrc/link.ts\n</function>\n</tool_call>\n",
        escape,
    ]

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return replies[len(prompts) - 1]

    kinds: list[str] = []
    for _ in range(2):
        outcomes: list[ReplyOutcome] = []
        written, _raw, _problems = complete_files(
            root, "Finish", {"name": "task-board"}, complete=complete, repair=state,
            outcomes=outcomes, views={},
        )
        assert written == []
        kinds.append(outcomes[-1].kind)
    assert kinds == ["rejected", "no_action"]
    assert all("do-not-leak" not in prompt for prompt in prompts)
    assert outside.read_text(encoding="utf-8") == "export const secret = 'do-not-leak';\n"


def test_symlink_read_is_rejected_with_a_reason(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    outside = tmp_path / "outside.ts"
    outside.write_text("export const secret = 'do-not-leak';\n", encoding="utf-8")
    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)
    (root / "src" / "TaskItem.tsx").write_text(f"{BROKEN_IMPORT}\n{TASK_ITEM}", encoding="utf-8")
    (root / "src" / "link.ts").symlink_to(outside)
    state = build_repair_state(
        "Finish", {"detail": "src/TaskItem.tsx: cannot resolve import './gone'"}, root,
    )
    outcomes: list[ReplyOutcome] = []
    complete_files(
        root, "Finish", {"name": "t"},
        complete=lambda _p: "<tool_call>\n<function=read>\nsrc/link.ts\n</function>\n</tool_call>\n",
        repair=state, outcomes=outcomes, views={},
    )
    assert outcomes[-1].kind == "rejected"
    assert "resolves outside the candidate" in outcomes[-1].problems[0]


def test_build_script_cannot_change_during_repair(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": "app", "scripts": {"build": "tsc -b && vite build"}}) + "\n", encoding="utf-8",
    )
    (root / "src" / "App.tsx").write_text(f"{BROKEN_IMPORT}\n", encoding="utf-8")
    state = build_repair_state("Finish", {"detail": "src/App.tsx: cannot resolve import './gone'"}, root)
    weakened = json.dumps({"name": "app", "scripts": {"build": "vite build"}})
    outcomes: list[ReplyOutcome] = []
    written, _raw, _problems = complete_files(
        root, "Finish", {"name": "t"}, complete=lambda _p: f"```package.json\n{weakened}\n```\n",
        repair=state, outcomes=outcomes, views={},
    )
    assert written == []
    assert outcomes[-1].kind == "rejected"
    assert "build script" in outcomes[-1].problems[0]
    assert "tsc -b" in (root / "package.json").read_text(encoding="utf-8")


def test_large_read_stays_in_budget_and_keeps_the_failing_line(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)
    late = [f"  const late{n} = {n}; // {'x' * 40}" for n in range(1, 401)]
    late[379] = "  const broken = missingName;"
    (root / "src" / "Late.tsx").write_text("\n".join(late) + "\n", encoding="utf-8")
    other = [f"export const other{n} = '{'y' * 60}';" for n in range(1, 401)]
    (root / "src" / "Other.tsx").write_text("\n".join(other) + "\n", encoding="utf-8")
    detail = "src/Late.tsx(380,18): error TS2304: Cannot find name 'missingName'."
    state = build_repair_state("Finish", {"detail": detail, "build": "fail"}, root)
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "```read src/Other.tsx\n```\n```read src/Late.tsx\n```\n"
        return "Thinking about it.\n"

    complete_files(root, "Finish", {"name": "t"}, complete=complete, repair=state, outcomes=[], views={})
    budget = prompt_budget_chars()
    assert all(len(prompt) <= budget for prompt in prompts)
    assert f">{line_hash(late[379])}|{late[379]}" in prompts[1]
    assert "requested lines of src/Other.tsx did not fit" in prompts[1]


def test_ledger_leaves_time_for_one_more_call_and_check():
    ledger = _GenerationLedger(CREATE, generation_limit=5, wall_seconds=110)
    assert ledger.can_generate()
    ledger.note_generation(100.0)
    ledger.note_check(20.0)
    assert not ledger.can_generate()
    assert ledger.stop_reason() == "wall_budget"
