"""Repair prompts keep the current diagnostic and its source window."""
from __future__ import annotations

from local_coder.general.direct_write import CompletionResult, complete_files, implement_prompt
from local_coder.general.harness import line_hash
from local_coder.general.repair_context import (
    build_repair_state,
    parse_diagnostic_hits,
    refresh_identity,
    repair_prompt_sections,
)


def _long_task_item(*, fail_at: int = 75, extra: str = "") -> str:
    lines = [f"    const row{index} = {index};" for index in range(1, 121)]
    lines[fail_at - 1] = f'        e.dataTransfer?.effectAllowed = "move";{extra}'
    return "\n".join(lines) + "\n"


def _state(root, detail: str, goal: str = "Finish the board"):
    return build_repair_state(
        goal,
        {
            "detail": detail,
            "build": "fail",
            "preview_health": {"code": "build_failed", "detail": detail},
        },
        root,
    )


def test_parse_ts_and_vite_locations():
    ts = 'src/TaskItem.tsx(75,9): error TS2779: The left-hand side of an assignment'
    vite = ',-[/tmp/candidate/src/TaskItem.tsx:75:1] x The left-hand side'
    hits = parse_diagnostic_hits(ts)
    assert hits[0].rel == "src/TaskItem.tsx"
    assert hits[0].line == 75
    assert parse_diagnostic_hits(vite)[0].line == 75
    named = parse_diagnostic_hits("src/App.tsx: cannot resolve import './gone'")
    assert named[0].rel == "src/App.tsx"
    assert named[0].line is None


def test_unused_noise_does_not_displace_blocking_errors(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text(
        "function saveTasks(){return null}\n" + ("const pad = 1;\n" * 40)
        + "const label = COLUMN_LABELS[next];\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "TaskItem.tsx").write_text(_long_task_item(fail_at=75), encoding="utf-8")
    noise = "\n".join(
        f"src/Other{index}.tsx({index},1): error TS6133: 'unused{index}' is declared but its value is never read."
        for index in range(1, 40)
    )
    detail = (
        "src/App.tsx(1,10): error TS6133: 'saveTasks' is declared but its value is never read.\n"
        f"{noise}\n"
        "src/App.tsx(42,16): error TS2304: Cannot find name 'next'.\n"
        "src/TaskItem.tsx(75,9): error TS2779: The left-hand side of an assignment expression\n"
    )
    state = _state(tmp_path, detail)
    prompt = implement_prompt("Finish the board", {"name": "app"}, tmp_path, repair=state)
    assert "TS2779" in prompt
    assert "effectAllowed" in prompt
    assert "Active repair target" in prompt
    assert "Cannot find name 'next'" in prompt
    assert "fix this target only" in prompt


def test_late_failing_line_is_in_the_repair_prompt(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text(
        "export default function App(){return <h1>App chrome and CSS hook</h1>}\n" * 40,
        encoding="utf-8",
    )
    (tmp_path / "src" / "App.css").write_text("body { color: red; }\n" * 80, encoding="utf-8")
    (tmp_path / "src" / "TaskItem.tsx").write_text(_long_task_item(fail_at=110), encoding="utf-8")
    state = _state(
        tmp_path,
        "src/TaskItem.tsx(110,9): error TS2779: The left-hand side of an assignment expression",
    )
    prompt = implement_prompt(
        "Finish the board",
        {"name": "app", "languages": ["TypeScript"]},
        tmp_path,
        repair=state,
    )
    assert "effectAllowed" in prompt
    assert "TS2779" in prompt
    assert "src/TaskItem.tsx" in prompt
    assert "look finished" not in prompt
    assert "2-4 sentences" not in prompt
    assert "Also write src/App.css" not in prompt
    app_pos = prompt.find("App chrome and CSS hook")
    fail_pos = prompt.find("effectAllowed")
    assert fail_pos != -1
    if app_pos != -1:
        assert fail_pos < app_pos


def test_unicode_failing_line_is_kept(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Board.tsx").write_text(
        "const title = \"看板\";\n" + ("const pad = 1;\n" * 20) + 'const label = "café";\n',
        encoding="utf-8",
    )
    state = _state(tmp_path, "src/Board.tsx(22,1): error TS2304: Cannot find name 'café'")
    text = repair_prompt_sections(state, tmp_path)
    label = 'const label = "café";'
    assert f">{line_hash(label)}|{label}" in text
    assert "src/Board.tsx (22 lines" in text


def test_multiple_cited_files_keep_each_failing_line(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text(
        "\n".join(f"export const chrome{index} = {index};" for index in range(1, 60)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "TaskItem.tsx").write_text(_long_task_item(fail_at=90), encoding="utf-8")
    state = _state(
        tmp_path,
        "src/App.tsx(40,1): error TS2322: Type 'number' is not assignable\n"
        "src/TaskItem.tsx(90,9): error TS2779: The left-hand side of an assignment expression",
    )
    prompt = implement_prompt(
        "Finish the board",
        {"name": "app", "languages": ["TypeScript"]},
        tmp_path,
        repair=state,
    )
    assert "effectAllowed" in prompt
    assert "TS2779" in prompt
    assert "TS2322" in prompt
    assert "Remaining diagnostics" in prompt


def test_vite_client_is_not_treated_as_project_source():
    hits = parse_diagnostic_hits(
        "GET /@vite/client net::ERR\n"
        "from /node_modules/vite/dist/client/client.mjs: vite-error-overlay"
    )
    assert hits == []


def test_missing_cited_file_is_named(tmp_path):
    (tmp_path / "src").mkdir()
    state = _state(tmp_path, "src/Gone.tsx(3,1): error TS2307: Cannot find module")
    assert "missing" in "\n".join(state.excerpts)


def test_source_change_marks_diagnostic_stale(tmp_path):
    (tmp_path / "src").mkdir()
    path = tmp_path / "src" / "TaskItem.tsx"
    path.write_text(_long_task_item(), encoding="utf-8")
    state = _state(tmp_path, "src/TaskItem.tsx(75,9): error TS2779: left-hand side")
    path.write_text(_long_task_item(extra=" // changed"), encoding="utf-8")
    refreshed = refresh_identity(state, tmp_path)
    assert refreshed.stale is True
    prompt = implement_prompt("Finish the board", {"name": "app"}, tmp_path, repair=refreshed)
    assert "not current" in prompt
    assert "effectAllowed" not in prompt or "changed after the check" in prompt


def test_error_survives_a_tool_call_xml_read(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("export default function App(){return null}\n", encoding="utf-8")
    (tmp_path / "src" / "TaskItem.tsx").write_text(_long_task_item(), encoding="utf-8")
    state = _state(
        tmp_path,
        "src/TaskItem.tsx(75,9): error TS2779: The left-hand side of an assignment expression",
    )
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return (
                "I'll inspect the file.\n"
                "<tool_call>\n<function=read>\nsrc/TaskItem.tsx\n</function>\n</tool_call>\n"
            )
        return "I will edit next.\n"

    written, _raw, _problems = complete_files(
        tmp_path,
        "Finish the board",
        {"name": "app", "languages": ["TypeScript"]},
        complete=complete,
        repair=state,
    )
    assert written == []
    assert len(prompts) == 2
    assert "TS2779" in prompts[1] or "left-hand side" in prompts[1]
    assert "effectAllowed" in prompts[1]


def test_error_survives_a_model_read(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("export default function App(){return null}\n", encoding="utf-8")
    (tmp_path / "src" / "TaskItem.tsx").write_text(_long_task_item(), encoding="utf-8")
    state = _state(
        tmp_path,
        "src/TaskItem.tsx(75,9): error TS2779: The left-hand side of an assignment expression",
    )
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "```read src/TaskItem.tsx\n```\n"
        return "I will edit next.\n"

    written, _raw, _problems = complete_files(
        tmp_path,
        "Finish the board",
        {"name": "app", "languages": ["TypeScript"]},
        complete=complete,
        retry_reason="The files you wrote failed checking",
        repair=state,
    )
    assert written == []
    assert len(prompts) == 2
    assert "TS2779" in prompts[1] or "left-hand side" in prompts[1]
    assert "effectAllowed" in prompts[1]
    assert line_hash('        e.dataTransfer?.effectAllowed = "move";') in prompts[1]


def test_truncated_reply_is_not_applied(tmp_path):
    (tmp_path / "src").mkdir()
    original = "export default function App(){return <p>hi</p>}\n"
    (tmp_path / "src" / "App.tsx").write_text(original, encoding="utf-8")

    def complete(_prompt: str) -> CompletionResult:
        return CompletionResult(
            text="```src/App.tsx\nexport default function App(){return <h1>Board</h1>}\n```\n",
            finish_reason="length",
            truncated=True,
            request_identity={"id": "chatcmpl-test", "model": "Ternary-Bonsai-2-27B-PQ2_0"},
        )

    written, raw, problems = complete_files(
        tmp_path, "Finish the board", {"name": "app"}, complete=complete,
    )
    assert written == []
    assert any("truncated" in item for item in problems)
    assert "Board" in raw
    assert (tmp_path / "src" / "App.tsx").read_text(encoding="utf-8") == original
