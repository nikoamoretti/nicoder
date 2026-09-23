"""Current-request coherence: visibility, one active target, purposeful reads."""
from __future__ import annotations

import json
from pathlib import Path

from local_coder.general.coordinator import request_dir, run_general_request
from local_coder.general.direct_write import (
    ReplyOutcome,
    build_repair_prompt,
    complete_files,
    implement_prompt,
    prompt_budget_chars,
)
from local_coder.general.harness import line_hash
from local_coder.general.repair_context import (
    FileView,
    build_repair_state,
    note_shown,
    select_active,
    serve_read,
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


def _state(root: Path, detail: str, goal: str = "Keep search, add, edit, and persistence working."):
    return build_repair_state(
        goal,
        {
            "detail": detail,
            "build": "fail",
            "preview_health": {"code": "build_failed", "detail": detail},
        },
        root,
    )


def _includes_section(prompt: str) -> str:
    start = prompt.find("This request includes:")
    end = prompt.find("This request does not include:")
    assert start != -1 and end != -1 and end > start
    return prompt[start:end]


def _omits_section(prompt: str) -> str:
    start = prompt.find("This request does not include:")
    assert start != -1
    return prompt[start:start + 1200]


def test_needed_type_from_a_previous_turn_stays_in_the_current_request(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    (src / "models.ts").write_text(
        "export type Widget = { id: string; label: string };\n"
        "export type Other = { n: number };\n",
        encoding="utf-8",
    )
    (src / "Page.tsx").write_text(
        'import { Widget } from "./models";\n'
        "export function Page(item: Widget) {\n"
        "  const row: Widget = { id: item.id, label: 1 };\n"
        "  return row.label;\n"
        "}\n",
        encoding="utf-8",
    )
    detail = "src/Page.tsx(3,27): error TS2322: Type 'number' is not assignable to type 'string'."
    state = _state(tmp_path, detail)
    views: dict[str, FileView] = {}
    first, shown = build_repair_prompt(
        state.goal, {"name": "catalog"}, tmp_path, repair=state, views=views,
    )
    note_shown(views, tmp_path, shown)
    assert "export type Widget" in first
    assert "This request includes:" in first
    assert "src/models.ts" in _includes_section(first)
    second, _shown2 = build_repair_prompt(
        state.goal, {"name": "catalog"}, tmp_path, repair=state, views=views,
        previous_outcome=ReplyOutcome("read", reads=["src/models.ts"], already_shown=["src/models.ts"]),
    )
    assert "export type Widget" in second
    assert "Already shown in full" not in second
    assert "not repeated" not in second
    assert "src/models.ts" in _includes_section(second)
    assert "Historical display is not current visibility" in second


def test_historically_read_omitted_source_is_not_called_available(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    fail = (
        "export function run() {\n"
        + "\n".join(f"  const pad{index} = {index};" for index in range(1, 20))
        + "\n  return missingName;\n}\n"
    )
    (src / "Fail.ts").write_text(fail, encoding="utf-8")
    (src / "History.ts").write_text(
        "\n".join(f"export const hist{index} = {index};" for index in range(1, 180)) + "\n",
        encoding="utf-8",
    )
    state = _state(tmp_path, "src/Fail.ts(21,10): error TS2304: Cannot find name 'missingName'.")
    views: dict[str, FileView] = {}
    first, shown = build_repair_prompt(
        state.goal, {"name": "catalog"}, tmp_path, repair=state, views=views,
    )
    note_shown(views, tmp_path, shown)
    assert "missingName" in first
    assert "hist50" not in first
    status, count = serve_read(views, tmp_path, "src/History.ts")
    assert status == "read" and count > 0
    second, shown2 = build_repair_prompt(
        state.goal, {"name": "catalog"}, tmp_path, repair=state, views=views,
        previous_outcome=ReplyOutcome("read", reads=["src/History.ts"]),
    )
    note_shown(views, tmp_path, shown2)
    assert "missingName" in second
    third, _shown3 = build_repair_prompt(
        state.goal, {"name": "catalog"}, tmp_path, repair=state, views=views,
        previous_outcome=ReplyOutcome("repeat_read", reads=["src/History.ts"], already_shown=["src/History.ts"]),
    )
    includes = _includes_section(third)
    omits = _omits_section(third)
    assert "src/Fail.ts" in includes
    assert "missingName" in third
    assert "src/History.ts" not in includes
    assert "src/History.ts (read earlier; not in this request)" in omits
    assert "Already shown in full, so not repeated" not in third
    assert "hist50" not in third


def test_late_scope_error_includes_enclosing_function_not_just_the_expression(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    body = ["export function submitForm(form: { title: string }) {"]
    body.extend(f"  const step{index} = {index};" for index in range(1, 40))
    body.append("  const payload = buildPayload(form, missingHelper);")
    body.extend(f"  const after{index} = {index};" for index in range(1, 8))
    body.append("  return payload;")
    body.append("}")
    (src / "Form.ts").write_text("\n".join(body) + "\n", encoding="utf-8")
    state = _state(tmp_path, "src/Form.ts(41,36): error TS2304: Cannot find name 'missingHelper'.")
    prompt = implement_prompt(state.goal, {"name": "forms"}, tmp_path, repair=state)
    source = prompt.split("Source (hash|line", 1)[-1]
    assert "missingHelper" in source
    assert "export function submitForm" in source
    assert "const payload = buildPayload" in source
    assert source.find("export function submitForm") < source.find("missingHelper")


def test_selecting_one_diagnostic_keeps_the_goal_and_remaining_errors(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    (src / "Alpha.ts").write_text("import { a } from './gone-a';\nexport const Alpha = a;\n", encoding="utf-8")
    (src / "Beta.ts").write_text("export function Beta() { return missingBeta; }\n", encoding="utf-8")
    (src / "Gamma.ts").write_text("const unused = 1;\nexport const Gamma = unused;\n", encoding="utf-8")
    detail = (
        "src/Alpha.ts(1,19): error TS2307: Cannot find module './gone-a' or its corresponding type declarations.\n"
        "src/Beta.ts(1,38): error TS2304: Cannot find name 'missingBeta'.\n"
        "src/Gamma.ts(1,7): error TS6133: 'unused' is declared but its value is never read.\n"
    )
    goal = "Ship a catalog the user can search, add to, edit, and keep after refresh."
    state = _state(tmp_path, detail, goal=goal)
    active = select_active(state.hits, detail)
    assert active[0].rel == "src/Alpha.ts"
    assert active[0].code == "TS2307"
    prompt = implement_prompt(goal, {"name": "catalog"}, tmp_path, repair=state)
    exact = prompt.split("Exact diagnostic:")[1].split("Remaining diagnostics")[0]
    assert "TS2307" in exact and "gone-a" in exact
    assert "missingBeta" not in exact
    assert "Remaining diagnostics" in prompt
    assert "missingBeta" in prompt
    assert "TS6133" in prompt
    assert goal in prompt
    assert "fix this target only" in prompt
    assert "gone-a" in prompt


def test_additional_read_does_not_displace_the_active_error_or_its_source(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    (src / "Fail.ts").write_text(
        "export function run() {\n  return missingName;\n}\n",
        encoding="utf-8",
    )
    (src / "Extra.ts").write_text(
        "\n".join(f"export const extra{index} = {index};" for index in range(1, 220)) + "\n",
        encoding="utf-8",
    )
    state = _state(tmp_path, "src/Fail.ts(2,10): error TS2304: Cannot find name 'missingName'.")
    views: dict[str, FileView] = {}
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "```read src/Extra.ts\n```\n"
        return "I still need a named section of Extra.ts.\n"

    written, _raw, _problems = complete_files(
        tmp_path, state.goal, {"name": "catalog"}, complete=complete, repair=state, views=views,
    )
    assert written == []
    assert len(prompts) == 2
    second = prompts[1]
    assert "missingName" in second
    assert f">{line_hash('  return missingName;')}|  return missingName;" in second
    assert "export function run" in second
    assert "src/Fail.ts" in _includes_section(second)
    assert "extra1" in second or "Extra.ts" in second


def test_applied_edit_invalidates_stale_views_then_rechecks_before_the_next_target(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    user = tmp_path / "catalog"
    (user / "src").mkdir(parents=True)
    (user / "src" / "App.tsx").write_text(
        "export default function App(){return <main>catalog</main>}\n",
        encoding="utf-8",
    )
    (user / "src" / "Alpha.ts").write_text("export const Alpha = 1;\n", encoding="utf-8")
    (user / "src" / "Beta.ts").write_text("export const Beta = 1;\n", encoding="utf-8")
    broken_alpha = "import { a } from './gone-a';\nexport const Alpha = a;\n"
    broken_beta = "import { b } from './gone-b';\nexport const Beta = b;\n"

    def seed(_prompt: str) -> str:
        return (
            f"```src/Alpha.ts\n{broken_alpha}```\n"
            f"```src/Beta.ts\n{broken_beta}```\n"
            "```src/App.tsx\nexport default function App(){return <main>catalog</main>}\n```\n"
        )

    run_general_request(
        user, "Ship a searchable catalog.", request_id="req-seed", fake_complete=seed,
        start_preview_after=False, generation_limit=1, model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            exact = prompt.split("Exact diagnostic:")[1].split("Remaining diagnostics")[0]
            assert "gone-a" in exact
            assert "gone-b" in prompt
            assert "Ship a searchable catalog." in prompt
            return "```src/Alpha.ts\nexport const Alpha = 1;\n```\n"
        exact = prompt.split("Exact diagnostic:")[1]
        if "Remaining diagnostics" in exact:
            exact = exact.split("Remaining diagnostics")[0]
        assert "gone-b" in exact
        assert "gone-a" not in exact
        assert "Your previous reply was applied: it changed src/Alpha.ts" in prompt
        return "```src/Beta.ts\nexport const Beta = 1;\n```\n"

    result = run_general_request(
        user, "Ship a searchable catalog.", request_id="req-next", fake_complete=complete,
        start_preview_after=False, generation_limit=6, continue_from="req-seed",
        model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )
    root = request_dir(result["project_id"], "req-next")
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    assert [row["kind"] for row in meta["replies"]] == ["applied", "applied"]
    assert [check["ok"] for check in meta["checks"]] == [False, False, True]
    assert meta["stop_reason"] == "checks_passed"
    assert "gone-a" not in (root / "candidate" / "src" / "Alpha.ts").read_text(encoding="utf-8")
    assert "gone-b" not in (root / "candidate" / "src" / "Beta.ts").read_text(encoding="utf-8")


def test_assembled_request_fits_the_current_token_budget(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    (src / "models.ts").write_text(
        "export type Item = { id: string; title: string };\n" * 12,
        encoding="utf-8",
    )
    page = ['import { Item } from "./models";', "export function Page(items: Item[]) {"]
    page.extend(f"  const row{index} = items[{index}];" for index in range(1, 80))
    page.append("  return items[missing];")
    page.append("}")
    (src / "Page.ts").write_text("\n".join(page) + "\n", encoding="utf-8")
    extra = "\n".join(
        f"src/Other{index}.ts(1,1): error TS6133: 'unused{index}' is declared but its value is never read."
        for index in range(1, 30)
    )
    detail = (
        "src/Page.ts(82,16): error TS2304: Cannot find name 'missing'.\n"
        f"{extra}\n"
    )
    state = _state(tmp_path, detail)
    prompt = implement_prompt(state.goal, {"name": "catalog"}, tmp_path, repair=state)
    budget = prompt_budget_chars()
    assert len(prompt) <= budget
    estimated = len(prompt) / 2.4 + 60 + 400 + 16
    assert estimated <= 8192 - 3072 - 512
    assert "Cannot find name 'missing'" in prompt


def test_unproductive_reads_stop_without_a_fabricated_edit(tmp_path, monkeypatch):
    bonsai_config(tmp_path, monkeypatch)
    user = tmp_path / "catalog"
    (user / "src").mkdir(parents=True)
    (user / "src" / "App.tsx").write_text(
        "export default function App(){return <main>catalog</main>}\n",
        encoding="utf-8",
    )
    broken = "import { helper } from './gone';\nexport const Row = helper;\n"

    def seed(_prompt: str) -> str:
        return (
            f"```src/Row.ts\n{broken}```\n"
            "```src/App.tsx\nexport default function App(){return <main>catalog</main>}\n```\n"
        )

    run_general_request(
        user, "Ship a searchable catalog.", request_id="req-seed", fake_complete=seed,
        start_preview_after=False, generation_limit=1, model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return (
            "Please give me all the full files.\n"
            "```read src/Row.ts\n```\n"
            "```read src/App.tsx\n```\n"
        )

    result = run_general_request(
        user, "Ship a searchable catalog.", request_id="req-loop", fake_complete=complete,
        start_preview_after=False, generation_limit=6, continue_from="req-seed",
        model_id="Ternary-Bonsai-2-27B-PQ2_0",
    )
    root = request_dir(result["project_id"], "req-loop")
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    kinds = [row["kind"] for row in meta["replies"]]
    assert "applied" not in kinds
    assert kinds[0] == "read"
    assert kinds.count("repeat_read") >= 2
    assert meta["stop_reason"] == "no_progress"
    assert broken in (root / "candidate" / "src" / "Row.ts").read_text(encoding="utf-8")
    assert any("entire repository cannot be included" in prompt for prompt in prompts)
    assert all("Make a focused edit now" not in prompt for prompt in prompts)
