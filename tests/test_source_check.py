"""Cheap source checks catch files Vite/Babel cannot compile."""
from __future__ import annotations

from local_coder.general.source_check import parse_retry_message, scan_changed_sources, scan_text


def test_scan_text_flags_mismatched_type_quotes():
    text = 'const columns: Task["status\'][] = ["todo", "in-progress", "done"];\n'
    problems = scan_text("src/App.tsx", text)
    assert problems
    assert any("quote" in item.lower() for item in problems)


def test_scan_text_flags_smart_quotes():
    text = (
        'const c: Record<Task["status"], number> = { todo: 0, “in-progress”: 0, done: 0 };\n'
    )
    problems = scan_text("src/App.tsx", text)
    assert any("quote" in item.lower() for item in problems)


def test_scan_text_accepts_valid_typescript_generics():
    text = 'const c: Record<Task["status"], number> = { todo: 0, "in-progress": 0, done: 0 };\n'
    assert scan_text("src/App.tsx", text) == []


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


def test_parse_retry_message_asks_for_straight_quotes():
    note = parse_retry_message(['src/App.tsx: mismatched quotes in a type'])
    assert "do not parse" in note.lower()
    assert "straight quotes" in note.lower()
    assert "src/App.tsx" in note


def test_curly_quotes_in_text_are_fine():
    # Nico's French app (2026-10-25): every "’" was an error, and the model stripped all accents to pass.
    jsx = '<p>Vider l’entrepôt ? Les données « INSEE » s’affichent ici.</p>\n'
    code = 'window.confirm("Vider l’entrepôt IndexedDB ? Cette action est définitive.");\n'
    assert scan_text("src/App.tsx", jsx) == []
    assert scan_text("src/App.tsx", code) == []


def test_curly_quotes_used_as_code_quotes_are_flagged():
    assert scan_text("src/App.tsx", "const label = “Tasks”;\n")
    assert scan_text("src/App.tsx", "import App from ‘./App’;\n")


def test_a_regex_class_and_apostrophes_in_jsx_are_not_errors():
    text = (
        "const QUOTE = /[\"']/g;\n"
        "export const Note = () => <p>It's the commune's latest figure (don't round it)</p>;\n"
    )
    assert scan_text("src/ingestion/normalise.tsx", text) == []


def test_a_broken_file_is_still_caught_without_a_build():
    assert scan_text("src/App.tsx", "export default function App(){return <h1>Tasks</h1\n")


def test_apostrophes_in_words_do_not_unbalance_the_count():
    text = "export const N = () => <p>Vider l'entrepôt ? C'est fini (vraiment). It's done {count}.</p>;\n"
    assert scan_text("src/App.tsx", text) == []


def test_a_regex_with_quotes_and_brackets_is_not_an_error():
    # From Nico's upload.ts: the quotes inside the pattern were read as strings.
    text = 'for (const m of s.matchAll(/<Rel[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"/g)) { use(m) }\n'
    assert scan_text("src/ingestion/upload.ts", text) == []
    assert scan_text("src/a.ts", "const half = (a + b) / 2; const r = total / count;\n") == []
