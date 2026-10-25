"""Candidate source checks without a live model."""
from __future__ import annotations

from local_coder.general.candidate_check import check_candidate, empty_app_reason, unresolved_imports


def test_unresolved_relative_import(tmp_path):
    (tmp_path / "src").mkdir()
    text = 'import { missing } from "./doesNotExist";\nexport default function App(){return null}\n'
    (tmp_path / "src" / "App.tsx").write_text(text, encoding="utf-8")
    problems = unresolved_imports(tmp_path, "src/App.tsx", text)
    assert problems
    result = check_candidate(tmp_path)
    assert result["ok"] is False
    assert result["source_ok"] is False
    assert "doesNotExist" in result["detail"]


def test_existing_relative_import(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "types.ts").write_text("export type Task = { id: string }\n", encoding="utf-8")
    text = 'import { type Task } from "./types";\nexport default function App(){return null}\n'
    (tmp_path / "src" / "App.tsx").write_text(text, encoding="utf-8")
    assert unresolved_imports(tmp_path, "src/App.tsx", text) == []
    assert check_candidate(tmp_path)["source_ok"] is True


def test_empty_starter_is_not_a_mounted_app(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text(
        "export default function App(){return <main><h1>New app</h1>"
        "<p>This starter is empty on purpose.</p></main>}\n",
        encoding="utf-8",
    )
    assert empty_app_reason(tmp_path)
    assert check_candidate(tmp_path)["ok"] is False


def test_a_passing_build_overrules_the_quick_scan(tmp_path, monkeypatch):
    from local_coder.general import candidate_check

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("export default function App(){return <h1>Tasks</h1\n", encoding="utf-8")
    monkeypatch.setattr(candidate_check, "run_project_build",
                        lambda root: {"ran": True, "ok": True, "code": "ok", "detail": "npm run build passed"})
    assert candidate_check.check_candidate(tmp_path)["source_ok"] is True
    monkeypatch.setattr(candidate_check, "run_project_build",
                        lambda root: {"ran": False, "ok": True, "code": "not_run", "detail": "no package.json"})
    assert candidate_check.check_candidate(tmp_path)["source_ok"] is False  # no build: the scan still judges
