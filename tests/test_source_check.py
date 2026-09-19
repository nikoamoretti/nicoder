"""Cheap source checks catch files Vite/Babel cannot compile."""
from __future__ import annotations

from local_coder.general.source_check import parse_retry_message, scan_changed_sources, scan_text


def test_scan_text_flags_mismatched_type_quotes():
    text = 'const columns: Task["status\'][] = ["todo", "in-progress", "done"];\n'
    problems = scan_text("src/App.tsx", text)
    assert problems
    assert any("quote" in item.lower() for item in problems)


def test_scan_text_flags_smart_quotes_and_tsx_generics():
    text = (
        'const c: Record<Task["status"], number> = { todo: 0, “in-progress”: 0, done: 0 };\n'
    )
    problems = scan_text("src/App.tsx", text)
    assert any("generic" in item.lower() or "quote" in item.lower() for item in problems)


def test_scan_text_accepts_plain_react():
    text = "export default function App(){return <main><h1>Board</h1></main>}\n"
    assert scan_text("src/App.tsx", text) == []


def test_scan_changed_sources_reads_only_listed_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.tsx").write_text(
        'const columns: Task["status\'][] = ["todo"];\n',
        encoding="utf-8",
    )
    (src / "ok.tsx").write_text("export const Ok = () => null\n", encoding="utf-8")
    problems = scan_changed_sources(tmp_path, ["src/App.tsx", "src/ok.tsx", "src/missing.tsx"])
    assert any("App.tsx" in item for item in problems)
    assert not any("ok.tsx" in item for item in problems)


def test_parse_retry_message_asks_for_js_friendly_files():
    note = parse_retry_message(['src/App.tsx: mismatched quotes in a type'])
    assert "do not parse" in note.lower()
    assert "generic" in note.lower()
    assert "src/App.tsx" in note
