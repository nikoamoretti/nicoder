"""Coordinator Keep/Discard uses snapshots, not the original tree, until Keep."""
from __future__ import annotations

import json
from pathlib import Path

from local_coder.general.coordinator import (
    _has_file_changes,
    discard_request,
    keep_request,
    prepare_recovery_workspace,
    prepare_workspace,
    request_dir,
    review_payload,
    run_general_request,
)


def test_has_file_changes_detects_new_source(tmp_path):
    before = tmp_path / "b"
    after = tmp_path / "a"
    before.mkdir()
    after.mkdir()
    (after / "src").mkdir()
    (after / "src" / "App.tsx").write_text("export const App = () => null\n", encoding="utf-8")
    assert _has_file_changes(before, after) is True
    assert _has_file_changes(before, before) is False


def test_empty_file_reply_is_retried_once(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_CODER_CONFIG_DIR", str(tmp_path / "config"))
    user = tmp_path / "app"
    user.mkdir()
    (user / "src").mkdir()
    (user / "src" / "App.tsx").write_text(
        "export default function App(){return <p>hi</p>}\n", encoding="utf-8",
    )
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "I will plan a task board with columns and cards."
        assert "no usable file blocks" in prompt
        return "```src/App.tsx\nexport default function App(){return <h1>Task Board</h1>}\n```\n"

    result = run_general_request(
        user,
        "Build a task board",
        request_id="req-retry",
        fake_complete=complete,
        start_preview_after=False,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    assert len(prompts) == 2
    assert result["can_keep"] is True
    assert result["engine"] == "local"
    assert "src/App.tsx" in result["facts"]["files"]
    assert "hi" in (user / "src" / "App.tsx").read_text(encoding="utf-8")


def test_two_empty_file_replies_is_honest_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_CODER_CONFIG_DIR", str(tmp_path / "config"))
    user = tmp_path / "app"
    user.mkdir()
    (user / "src").mkdir()
    (user / "src" / "App.tsx").write_text(
        "export default function App(){return <p>hi</p>}\n", encoding="utf-8",
    )
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return "Here is a plan. I will add columns later."

    result = run_general_request(
        user,
        "Build a task board",
        request_id="req-empty",
        fake_complete=complete,
        start_preview_after=False,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    assert len(prompts) == 2
    assert result["can_keep"] is False
    assert result["facts"]["files"] == []
    assert "asked twice" in (result.get("model_commentary") or "").lower()
    assert "not modified" in result["summary"].lower()


def test_review_omits_leftover_preview_when_no_files(tmp_path):
    from local_coder.general.preview import preview_record_path

    user = tmp_path / "app"
    user.mkdir()
    (user / "package.json").write_text('{"name":"app"}\n', encoding="utf-8")
    meta = prepare_workspace(user, "req-preview", allow_starter=False)
    leftover = {
        "ok": True,
        "url": "http://127.0.0.1:4173/",
        "project_id": meta["project_id"],
        "dir": str(user),
    }
    path = preview_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(leftover), encoding="utf-8")
    review = review_payload(meta["project_id"], "req-preview")
    assert review["facts"]["files"] == []
    assert review.get("preview") == {}


def test_review_does_not_treat_files_as_verified(tmp_path, monkeypatch):
    user = tmp_path / "app"
    user.mkdir()
    (user / "src").mkdir()
    (user / "src" / "App.tsx").write_text(
        "export default function App(){return <p>hi</p>}\n", encoding="utf-8",
    )

    def complete(_prompt: str) -> str:
        return (
            "I updated the heading.\n\n"
            "```src/App.tsx\nexport default function App(){return <h1>Board</h1>}\n```\n"
        )

    result = run_general_request(
        user,
        "Build a task board",
        request_id="req-unverified",
        fake_complete=complete,
        start_preview_after=False,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    assert result["can_keep"] is True
    assert result["facts"]["complete"] is False
    assert result["verification"]["tests"] == "not_run"
    assert "package-lock.json" not in result["facts"]["files"]


def test_review_leads_with_plain_language(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_CODER_CONFIG_DIR", str(tmp_path / "config"))
    user = tmp_path / "app"
    user.mkdir()
    (user / "src").mkdir()
    (user / "src" / "App.tsx").write_text(
        "export default function App(){return <p>hi</p>}\n", encoding="utf-8",
    )

    def complete(prompt: str) -> str:
        assert "2-4 sentences" in prompt
        return (
            "I turned the starter into a three-column board with larger type.\n\n"
            "```src/App.tsx\nexport default function App(){return <h1>Board</h1>}\n```\n"
        )

    result = run_general_request(
        user,
        "Make the UI better",
        request_id="req-plain",
        fake_complete=complete,
        start_preview_after=False,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    assert "three-column" in result["summary"]
    assert "Proposed changes" not in result["summary"]
    assert result["model_commentary"] == result["summary"]


def test_fake_engine_keep_and_discard(tmp_path):
    user = tmp_path / "app"
    user.mkdir()
    (user / "src").mkdir()
    (user / "src" / "App.tsx").write_text("export default function App(){return <p>hi</p>}\n", encoding="utf-8")
    original = (user / "src" / "App.tsx").read_text(encoding="utf-8")

    def fake(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "src" / "App.tsx").write_text(
            "export default function App(){return <p>board</p>}\n", encoding="utf-8",
        )
        (candidate / "src" / "board.tsx").write_text("export const Board = () => null\n", encoding="utf-8")

    result = run_general_request(
        user, "Build a board",
        request_id="req-keep",
        fake_engine=fake,
        start_preview_after=False,
    )
    assert (user / "src" / "App.tsx").read_text(encoding="utf-8") == original
    assert result["can_keep"] is True
    assert "src/App.tsx" in result["facts"]["files"]

    kept = keep_request(result["project_id"], "req-keep", user)
    assert kept["ok"] is True
    assert "board" in (user / "src" / "App.tsx").read_text(encoding="utf-8")
    assert (user / "src" / "board.tsx").exists()


def test_discard_leaves_original(tmp_path):
    user = tmp_path / "app"
    user.mkdir()
    (user / "a.ts").write_text("1\n", encoding="utf-8")

    def fake(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "a.ts").write_text("2\n", encoding="utf-8")

    result = run_general_request(
        user, "change a",
        request_id="req-discard",
        fake_engine=fake,
        start_preview_after=False,
    )
    discard_request(result["project_id"], "req-discard")
    assert (user / "a.ts").read_text(encoding="utf-8") == "1\n"


def test_starter_used_for_empty_folder(tmp_path):
    user = tmp_path / "blank"
    user.mkdir()
    meta = prepare_workspace(user, "req-empty", allow_starter=True)
    assert meta["starter_used"] is True
    assert (user / "package.json").exists()
    assert "task" not in (user / "src" / "App.tsx").read_text(encoding="utf-8").lower()
    review = review_payload(meta["project_id"], "req-empty")
    assert review["engine"] == "local"


def test_broken_preview_is_honest_not_finished_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_CODER_CONFIG_DIR", str(tmp_path / "config"))
    user = tmp_path / "app"
    user.mkdir()
    (user / "package.json").write_text('{"name":"app","scripts":{"dev":"vite"}}\n', encoding="utf-8")
    (user / "src").mkdir()
    (user / "src" / "App.tsx").write_text(
        "export default function App(){return <p>hi</p>}\n", encoding="utf-8",
    )

    def complete(_prompt: str) -> str:
        return (
            "I rebuilt the task board as a polished, finished UI: a three-column "
            "Kanban layout with color-coded status cards.\n\n"
            "```src/App.tsx\nexport default function App(){return <h1>Board</h1>}\n```\n"
        )

    def fake_start(project_dir, project_id, **_kwargs):
        return {
            "ok": True,
            "url": "http://127.0.0.1:4199/",
            "dir": str(project_dir),
            "project_id": project_id,
        }

    def fake_health(_url, _project_dir=None, **_kwargs):
        return {
            "ok": False,
            "code": "vite_overlay",
            "detail": '[plugin:vite:react-babel] Unexpected token, expected ";"',
        }

    monkeypatch.setattr("local_coder.general.coordinator.start_preview", fake_start)
    monkeypatch.setattr("local_coder.general.coordinator.check_preview_health", fake_health)

    result = run_general_request(
        user,
        "Can you make this app better in terms of UI and design?",
        request_id="req-preview-fail",
        fake_complete=complete,
        start_preview_after=True,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    assert result["can_keep"] is True
    assert result["facts"]["complete"] is False
    assert result["verification"]["preview"] == "failed"
    assert result["preview"].get("ok") is False
    assert "url" not in result["preview"]
    text = f"{result['summary']} {result.get('model_commentary') or ''}".lower()
    assert "does not run" in text or "preview" in text and "fail" in text
    assert "polished" not in text
    assert "three-column" not in text
    assert "kanban" not in text
    again = review_payload(result["project_id"], "req-preview-fail")
    assert again["verification"]["preview"] == "failed"
    assert "polished" not in (again.get("model_commentary") or "").lower()
    assert "hi" in (user / "src" / "App.tsx").read_text(encoding="utf-8")


def test_unparseable_tsx_is_retried_once(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_CODER_CONFIG_DIR", str(tmp_path / "config"))
    user = tmp_path / "app"
    user.mkdir()
    (user / "src").mkdir()
    (user / "src" / "App.tsx").write_text(
        "export default function App(){return <p>hi</p>}\n", encoding="utf-8",
    )
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if "do not parse" in prompt.lower():
            return (
                "I simplified the heading.\n\n"
                "```src/App.tsx\nexport default function App(){return <h1>Board</h1>}\n```\n"
            )
        return (
            "I rebuilt the board.\n\n"
            "```src/App.tsx\n"
            'const columns: Task["status\'][] = ["todo"];\n'
            "export default function App(){return <h1>Broken</h1>}\n"
            "```\n"
        )

    result = run_general_request(
        user,
        "Make the UI better",
        request_id="req-parse-retry",
        fake_complete=complete,
        start_preview_after=False,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    assert len(prompts) == 2
    assert any("do not parse" in item.lower() for item in prompts)
    assert result["can_keep"] is True
    candidate = request_dir(result["project_id"], "req-parse-retry") / "candidate" / "src" / "App.tsx"
    assert "Board" in candidate.read_text(encoding="utf-8")
    assert "Broken" not in candidate.read_text(encoding="utf-8")
    assert "hi" in (user / "src" / "App.tsx").read_text(encoding="utf-8")


def test_prepare_recovery_does_not_mutate_historical(tmp_path):
    hist = tmp_path / "historical"
    (hist / "baseline" / "src").mkdir(parents=True)
    (hist / "candidate" / "src").mkdir(parents=True)
    app = b'import TaskBoard from "./components/TaskBoard";\n'
    (hist / "candidate" / "src" / "App.tsx").write_bytes(app)
    (hist / "baseline" / "src" / "App.tsx").write_text("starter\n", encoding="utf-8")
    (hist / "meta.json").write_text(
        json.dumps({
            "user_path": str(tmp_path / "user"),
            "project_id": "app-user-testrecov",
        }),
        encoding="utf-8",
    )
    hist_meta = (hist / "meta.json").read_bytes()
    meta = prepare_recovery_workspace(hist, "req-recover")
    assert (hist / "candidate" / "src" / "App.tsx").read_bytes() == app
    assert (hist / "meta.json").read_bytes() == hist_meta
    assert meta["starter_used"] is False
    assert meta["recovered_from"] == str(hist.resolve())
    copied = Path(meta["candidate"]) / "src" / "App.tsx"
    assert copied.read_bytes() == app
    start = json.loads((Path(meta["baseline"]).parent / "recovery_start.json").read_text(encoding="utf-8"))
    assert "src/App.tsx" in start["files"]
