"""Direct model file writes when the coding engine only plans."""
from __future__ import annotations

from local_coder.general.coordinator import run_general_request
from local_coder.general.direct_write import (
    apply_files,
    checked_blocks,
    complete_files,
    fallback_explanation,
    implement_prompt,
    is_blank_starter,
    parse_file_blocks,
    reply_prose,
    safe_rel,
)


def test_reply_prose_reads_text_before_fences():
    text = """I rebuilt the board with three columns and larger type.

```src/App.tsx
export default function App() { return <h1>Board</h1> }
```
"""
    assert "three columns" in reply_prose(text)
    assert "App.tsx" not in reply_prose(text)
    assert reply_prose("```src/App.tsx\nexport const App = () => null\n```\n") == ""
    assert "src/App.tsx" in fallback_explanation(["src/App.tsx"])


def test_checked_blocks_rejects_the_kanban_tsx_error():
    raw = """I rebuilt the board.

```src/App.tsx
const columns: Task["status'][] = ["todo", "in-progress", "done"];
const c: Record<Task["status"], number> = { todo: 0, "in-progress": 0, done: 0 };
export default function App() { return <main /> }
```
"""
    files, problems = checked_blocks(raw)
    assert "src/App.tsx" in files
    assert problems


def test_parse_file_blocks_reads_path_and_body():
    text = """I'll write the app now.

```src/App.tsx
export default function App() { return <h1>Board</h1> }
```
"""
    files = parse_file_blocks(text)
    assert files == {"src/App.tsx": "export default function App() { return <h1>Board</h1> }\n"}


def test_parse_file_blocks_accepts_language_tag_and_strips_think():
    text = """<think>planning</think>
```tsx src/App.tsx
export const App = () => null
```
"""
    files = parse_file_blocks(text)
    assert "src/App.tsx" in files
    assert "export const App" in files["src/App.tsx"]


def test_safe_rel_rejects_escape():
    assert safe_rel("../secret.ts") is None
    assert safe_rel("src/App.tsx") == "src/App.tsx"
    assert safe_rel("node_modules/pkg/index.js") is None


def test_safe_rel_allows_electron_and_root_package():
    assert safe_rel("electron/main.cjs") == "electron/main.cjs"
    assert safe_rel("package.json") == "package.json"
    assert safe_rel("electron/package.json") == "electron/package.json"
    assert safe_rel("public/index.html") == "public/index.html"
    assert safe_rel("src/secret.json") is None


def test_is_blank_starter_detects_bundled_copy(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    (src / "App.tsx").write_text(
        'export default function App(){return <h1>New app</h1>}\n',
        encoding="utf-8",
    )
    assert is_blank_starter(tmp_path) is True
    (src / "App.tsx").write_text("export default function App(){return <h1>Tasks</h1>}\n", encoding="utf-8")
    assert is_blank_starter(tmp_path) is False


def test_apply_files_writes_only_safe_paths(tmp_path):
    written = apply_files(tmp_path, {
        "src/App.tsx": "export const App = () => null\n",
        "electron/main.cjs": "console.log('main')\n",
        "package.json": '{"name":"app"}\n',
        "../escape.ts": "nope\n",
    })
    assert written == ["src/App.tsx", "electron/main.cjs", "package.json"]
    assert (tmp_path / "src" / "App.tsx").is_file()
    assert (tmp_path / "electron" / "main.cjs").is_file()
    assert (tmp_path / "package.json").is_file()
    assert not (tmp_path.parent / "escape.ts").exists() or "nope" not in (
        (tmp_path.parent / "escape.ts").read_text(encoding="utf-8") if (tmp_path.parent / "escape.ts").exists() else ""
    )


def test_implement_prompt_covers_first_build_and_wrap(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("export default function App(){return null}\n", encoding="utf-8")
    prompt = implement_prompt(
        "Can you make this a standalone mac app?",
        {"name": "app", "languages": ["TypeScript"]},
        tmp_path,
    )
    assert "Can you make this a standalone mac app?" in prompt
    assert "outside Local Coder" in prompt
    assert "electron/" in prompt
    assert "2-4 sentences" in prompt
    assert "App.css" in prompt
    assert "typescript" in prompt.lower()
    assert "only replace App.tsx" not in prompt.lower()
    retry = implement_prompt(
        "Build a task board",
        {"name": "app", "languages": ["TypeScript"]},
        tmp_path,
        previous="I will plan the columns first.",
    )
    assert "no usable file blocks" in retry
    assert "Build a task board" in retry
    with_convo = implement_prompt(
        "make the cards blue",
        {"name": "app", "languages": ["TypeScript"]},
        tmp_path,
        conversation="Recent conversation:\nuser: Build a task board\nassistant: Wrote src/App.tsx.",
    )
    assert "Build a task board" in with_convo
    assert "make the cards blue" in with_convo
    parse_retry = implement_prompt(
        "Make the UI better",
        {"name": "app", "languages": ["TypeScript"]},
        tmp_path,
        previous="```src/App.tsx\nbad\n```\n",
        retry_reason="The files you wrote do not parse. Problems:\n- src/App.tsx: mismatched quotes",
    )
    assert "do not parse" in parse_retry
    assert "no usable file blocks" not in parse_retry


def test_blank_starter_uses_direct_write_not_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_CODER_CONFIG_DIR", str(tmp_path / "config"))
    user = tmp_path / "app"
    user.mkdir()
    (user / "package.json").write_text('{"name":"app"}\n', encoding="utf-8")
    (user / "src").mkdir()
    (user / "src" / "App.tsx").write_text(
        "export default function App(){\n  return (\n    <main>\n      <h1>New app</h1>\n"
        "      <p>This starter is empty on purpose.</p>\n    </main>\n  )\n}\n",
        encoding="utf-8",
    )

    def complete(prompt: str) -> str:
        assert "Build a task board" in prompt
        return "```src/App.tsx\nexport default function App(){return <h1>Task Board</h1>}\n```\n"

    result = run_general_request(
        user,
        "Build a task board",
        request_id="req-direct",
        fake_complete=complete,
        start_preview_after=False,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    assert result["can_keep"] is True
    assert "src/App.tsx" in result["facts"]["files"]
    assert "empty on purpose" in (user / "src" / "App.tsx").read_text(encoding="utf-8")


def test_wrap_as_app_uses_same_apply_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_CODER_CONFIG_DIR", str(tmp_path / "config"))
    user = tmp_path / "app"
    user.mkdir()
    (user / "package.json").write_text('{"name":"app","scripts":{"dev":"vite"}}\n', encoding="utf-8")
    (user / "src").mkdir()
    (user / "src" / "App.tsx").write_text(
        "export default function App(){return <h1>Board</h1>}\n", encoding="utf-8",
    )

    def complete(prompt: str) -> str:
        assert "standalone mac app" in prompt.lower()
        assert "electron/" in prompt
        return (
            "```electron/main.cjs\nconsole.log('electron')\n```\n"
            "```package.json\n{\"name\":\"app\",\"main\":\"electron/main.cjs\"}\n```\n"
        )

    result = run_general_request(
        user,
        "Can you make this a standalone mac app?",
        request_id="req-wrap",
        fake_complete=complete,
        start_preview_after=False,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    assert result["kind"] == "code"
    assert result["can_keep"] is True
    assert "electron/main.cjs" in result["facts"]["files"]
    assert "package.json" in result["facts"]["files"]
    assert not (user / "electron").exists()


def test_complete_files_applies_valid_typescript_generics(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "src").mkdir()
    body = (
        'const counts: Record<Task["status"], number> = { todo: 0, "in-progress": 0, done: 0 };\n'
        "export default function App(){return <main />}\n"
    )

    written, _raw, problems = complete_files(
        root,
        "Build a board",
        {"name": "app", "languages": ["TypeScript"]},
        complete=lambda _prompt: f"```src/App.tsx\n{body}```\n",
    )
    assert problems == []
    assert written == ["src/App.tsx"]
    assert "Record<Task" in (root / "src" / "App.tsx").read_text(encoding="utf-8")


def test_complete_files_does_not_write_unparseable_tsx(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "src").mkdir()
    original = "export default function App(){return <p>hi</p>}\n"
    (root / "src" / "App.tsx").write_text(original, encoding="utf-8")

    def complete(_prompt: str) -> str:
        return (
            "```src/App.tsx\n"
            'const columns: Task["status\'][] = ["todo"];\n'
            "export default function App(){return <main />}\n"
            "```\n"
        )

    written, raw, problems = complete_files(
        root, "Make the UI better", {"name": "app", "languages": ["TypeScript"]},
        complete=complete,
    )
    assert written == []
    assert problems
    assert "status" in raw
    assert (root / "src" / "App.tsx").read_text(encoding="utf-8") == original
