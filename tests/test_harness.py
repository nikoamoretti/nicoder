"""Local Coder owns a tiny candidate-only read / write / edit loop."""
from __future__ import annotations

import pytest

from local_coder.general.direct_write import complete_files, implement_prompt, parse_file_blocks
from local_coder.general.harness import (
    EditError,
    apply_edit_text,
    format_hashed,
    line_hash,
    parse_actions,
    read_files,
)


def test_line_hash_is_stable_for_the_same_text():
    assert line_hash("  return <p>hi</p>") == line_hash("  return <p>hi</p>")
    assert line_hash("a") != line_hash("b")
    assert len(line_hash("x")) == 8


def test_parse_actions_classifies_read_write_edit_and_refuses_shell():
    raw = """
```read src/App.css
```
```src/App.tsx
export default function App(){return null}
```
```edit src/main.tsx
-aaaaaaaa
+export {}
```
```bash
rm -rf /
```
```shell src/evil.ts
oops
```
"""
    parsed = parse_actions(raw)
    assert parsed.reads == ["src/App.css"]
    assert parsed.writes["src/App.tsx"].startswith("export default")
    assert parsed.edits[0][0] == "src/main.tsx"
    assert "aaaaaaaa" in parsed.edits[0][1]
    assert any("bash" in item or "shell" in item for item in parsed.refused)


def test_two_edits_to_one_file_both_apply(tmp_path):
    from local_coder.general.harness import materialize

    root = tmp_path
    (root / "src").mkdir()
    (root / "src" / "App.tsx").write_text("import a from 'a';\nconst x = 1;\nconst y = 2;\n", encoding="utf-8")
    raw = (
        f"```edit src/App.tsx\n-{line_hash('const x = 1;')}\n+const x = 10;\n```\n"
        f"```edit src/App.tsx\n-{line_hash('const y = 2;')}\n+const y = 20;\n```\n"
    )
    files, problems = materialize(root, parse_actions(raw))
    assert problems == []
    assert files["src/App.tsx"] == "import a from 'a';\nconst x = 10;\nconst y = 20;\n"


def test_ambiguous_or_scattered_hash_spans_are_rejected_with_a_reason():
    current = "}\nconst a = 1;\n}\nconst b = 2;\n"
    with pytest.raises(EditError, match="match 2 places"):
        apply_edit_text(current, f"-{line_hash('}')}\n+}}\n")
    with pytest.raises(EditError, match="not consecutive"):
        apply_edit_text(current, f"-{line_hash('const a = 1;')}\n-{line_hash('const b = 2;')}\n+x\n")


def test_hash_tokens_tolerate_marker_and_pasted_line_text():
    current = "const a = 1;\nconst b = 2;\n"
    marked = apply_edit_text(current, f"->{line_hash('const a = 1;')}\n+const a = 3;\n")
    assert marked.startswith("const a = 3;\n")
    pasted = apply_edit_text(current, f"-{line_hash('const b = 2;')}|const b = 2;\n+const b = 4;\n")
    assert pasted.endswith("const b = 4;\n")


def test_parameter_style_xml_read_is_a_read():
    raw = "<tool_call>\n<function=read>\n<parameter=path>\nsrc/App.tsx\n</parameter>\n</function>\n</tool_call>"
    assert parse_actions(raw).reads == ["src/App.tsx"]


def test_edit_through_symlink_out_of_the_candidate_is_refused(tmp_path):
    from local_coder.general.harness import materialize

    outside = tmp_path / "outside.ts"
    outside.write_text("export const secret = 1;\n", encoding="utf-8")
    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)
    (root / "src" / "link.ts").symlink_to(outside)
    raw = f"```edit src/link.ts\n-{line_hash('export const secret = 1;')}\n+export const secret = 2;\n```\n"
    files, problems = materialize(root, parse_actions(raw))
    assert files == {}
    assert "outside the candidate" in problems[0]
    assert outside.read_text(encoding="utf-8") == "export const secret = 1;\n"


def test_parse_actions_treats_tool_call_xml_as_a_read():
    raw = """I'll inspect the failing file.

<tool_call>
<function=read>
src/TaskItem.tsx
</function>
</tool_call>
"""
    parsed = parse_actions(raw)
    assert parsed.reads == ["src/TaskItem.tsx"]
    assert parsed.writes == {}


def test_parse_file_blocks_does_not_treat_read_or_edit_as_writes():
    raw = """
```read src/App.tsx
stolen
```
```edit src/App.tsx
-aaaaaaaa
+nope
```
```src/App.css
body{}
```
"""
    files = parse_file_blocks(raw)
    assert files == {"src/App.css": "body{}\n"}


def test_apply_edit_text_replaces_hashed_span():
    current = "export default function App(){\n  return <p>hi</p>\n}\n"
    target = line_hash("  return <p>hi</p>")
    updated = apply_edit_text(current, f"-{target}\n+  return <h1>Board</h1>\n")
    assert "Board" in updated
    assert "hi" not in updated
    assert updated.startswith("export default")


def test_apply_edit_text_rejects_stale_hash():
    current = "export default function App(){\n  return <p>hi</p>\n}\n"
    with pytest.raises(EditError, match="does not match"):
        apply_edit_text(current, "-ffffffff\n+  return <h1>Board</h1>\n")


def test_apply_edit_text_rejects_escape_paths_via_parse_actions():
    parsed = parse_actions("```edit ../secret.ts\n-aaaaaaaa\n+nope\n```\n")
    assert parsed.edits == []


def test_read_files_returns_hashed_candidate_contents(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "theme.ts").write_text("export const blue = '#00f'\n", encoding="utf-8")
    text = read_files(tmp_path, ["src/theme.ts", "../secret.ts", "src/missing.ts"])
    assert "src/theme.ts" in text
    assert line_hash("export const blue = '#00f'") in text
    assert "#00f" in text
    assert "secret" not in text
    assert "missing" in text


def test_format_hashed_marks_and_windows_a_late_line():
    body = "\n".join(f"const n{index} = {index};" for index in range(1, 201))
    text = format_hashed(body, start_line=170, limit=21, mark_line=180)
    assert f">{line_hash('const n180 = 180;')}|const n180 = 180;" in text
    assert "const n1 = 1;" not in text
    assert "169 lines above" in text


def test_read_files_windows_around_a_failing_line(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    body = "\n".join(f"const n{index} = {index};" for index in range(1, 201)) + "\n"
    (src / "Late.tsx").write_text(body, encoding="utf-8")
    text = read_files(tmp_path, ["src/Late.tsx"], around={"src/Late.tsx": 180})
    assert "const n180 = 180;" in text
    assert f">{line_hash('const n180 = 180;')}|" in text
    assert "const n1 = 1;" not in text


def test_format_hashed_prefixes_each_line():
    text = format_hashed("alpha\nbeta\n")
    assert text.splitlines()[0].startswith(f"{line_hash('alpha')}|")
    assert text.splitlines()[0].endswith("alpha")


def test_implement_prompt_teaches_read_and_hash_edit(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text(
        "export default function App(){return null}\n", encoding="utf-8",
    )
    prompt = implement_prompt(
        "Make the title larger",
        {"name": "app", "languages": ["TypeScript"]},
        tmp_path,
    )
    assert "```edit" in prompt
    assert "```read" in prompt
    assert "no shell" in prompt.lower() or "no bash" in prompt.lower()
    assert f"{line_hash('export default function App(){return null}')}|" in prompt


def test_complete_files_applies_a_hash_edit(tmp_path):
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "App.tsx").write_text(
        "export default function App(){\n  return <p>hi</p>\n}\n",
        encoding="utf-8",
    )
    target = line_hash("  return <p>hi</p>")

    written, _raw, problems = complete_files(
        root,
        "Make a heading",
        {"name": "app", "languages": ["TypeScript"]},
        complete=lambda _prompt: (
            f"```edit src/App.tsx\n-{target}\n+  return <h1>Board</h1>\n```\n"
        ),
    )
    assert problems == []
    assert written == ["src/App.tsx"]
    text = (root / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "Board" in text
    assert "hi" not in text


def test_complete_files_read_then_write(tmp_path):
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "App.tsx").write_text(
        "export default function App(){return null}\n", encoding="utf-8",
    )
    (root / "src" / "theme.ts").write_text("export const blue = '#00f'\n", encoding="utf-8")
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "```read src/theme.ts\n```\n"
        assert "export const blue" in prompt
        return "```src/App.tsx\nexport default function App(){return <h1>blue</h1>}\n```\n"

    written, _raw, problems = complete_files(
        root, "Use the theme color", {"name": "app", "languages": ["TypeScript"]},
        complete=complete,
    )
    assert problems == []
    assert written == ["src/App.tsx"]
    assert len(prompts) == 2
    assert "blue" in (root / "src" / "App.tsx").read_text(encoding="utf-8")


def test_complete_files_does_not_run_shell(tmp_path):
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    original = "export default function App(){return <p>hi</p>}\n"
    (root / "src" / "App.tsx").write_text(original, encoding="utf-8")

    written, raw, problems = complete_files(
        root,
        "Delete everything",
        {"name": "app", "languages": ["TypeScript"]},
        complete=lambda _prompt: "```bash\nrm -rf /\n```\n",
    )
    assert written == []
    assert (root / "src" / "App.tsx").read_text(encoding="utf-8") == original
    assert "bash" in raw.lower() or any("bash" in item or "shell" in item for item in problems)


def test_complete_files_does_not_apply_stale_edit(tmp_path):
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    original = "export default function App(){\n  return <p>hi</p>\n}\n"
    (root / "src" / "App.tsx").write_text(original, encoding="utf-8")

    written, _raw, problems = complete_files(
        root,
        "Make a heading",
        {"name": "app", "languages": ["TypeScript"]},
        complete=lambda _prompt: "```edit src/App.tsx\n-ffffffff\n+  return <h1>Board</h1>\n```\n",
    )
    assert written == []
    assert problems
    assert (root / "src" / "App.tsx").read_text(encoding="utf-8") == original
