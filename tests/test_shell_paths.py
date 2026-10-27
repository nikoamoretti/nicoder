"""Shell commands in a change request: the original project's path means the working copy where the shell uses it.

2026-10-27 benchmark: `cd <original> && python3 -m unittest` tested the untouched original 40 times over. From the
Challenger's review of the first fix: text a command writes keeps the original's path, what the copy leaves out
stays on the original, git reads the original, and a working copy inside the project is left alone.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from local_coder.agent.shell_paths import into_working_copy


@pytest.fixture
def folders(tmp_path) -> tuple[Path, Path, Path]:
    home = Path(os.path.realpath(tmp_path)) / "home"
    project = home / "Local Coder Projects" / "pov"
    work = home / ".local" / "share" / "lc" / "work"
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (work / "src").mkdir(parents=True)
    return home, project, work


def _map(command: str, home: Path, project: Path, work: Path) -> str:
    return into_working_copy(command, project=project, workdir=work, home=home)


def test_a_path_the_shell_uses_means_the_working_copy(folders):
    _, project, work = folders
    p, w = str(project), str(work)
    assert _map(f'cd "{p}" && python3 -m unittest pov_test.py', *folders) == (
        f'cd "{w}" && python3 -m unittest pov_test.py')
    assert _map("cd ~/Local\\ Coder\\ Projects/pov && ls", *folders) == f"cd {w} && ls"
    assert _map('cd "$HOME/Local Coder Projects/pov" && ls', *folders) == f'cd "{w}" && ls'
    assert _map(f'PYTHONPATH="{p}/src" pytest --rootdir="{p}" "{p}/tests"', *folders) == (
        f'PYTHONPATH="{w}/src" pytest --rootdir="{w}" "{w}/tests"')
    assert _map(f"echo built > '{p}/out.txt'", *folders) == f"echo built > '{w}/out.txt'"


def test_every_entry_of_a_path_list_and_a_shell_script_are_mapped(folders):
    _, project, work = folders
    p, w = str(project), str(work)
    assert _map(f'PYTHONPATH="{p}/src:{p}/lib" pytest', *folders) == f'PYTHONPATH="{w}/src:{w}/lib" pytest'
    assert _map(f'PATH="{p}/bin:$PATH" make', *folders) == f'PATH="{w}/bin:$PATH" make'
    assert _map(f"bash -c 'cd \"{p}\" && cat a.py'", *folders) == f"bash -c 'cd \"{w}\" && cat a.py'"
    assert _map(f'sh -lc "cd \'{p}\' && echo \'{p}\'"', *folders) == f'sh -lc "cd \'{w}\' && echo \'{p}\'"'


def test_a_search_pattern_is_text(folders):
    _, project, work = folders
    p, w = str(project), str(work)
    assert _map(f'grep -rn "{p}" src', *folders) == f'grep -rn "{p}" src'
    assert _map(f'grep -rn -A 3 "foo" "{p}/src"', *folders) == f'grep -rn -A 3 "foo" "{w}/src"'
    assert _map(f'rg -e "{p}" "{p}"', *folders) == f'rg -e "{p}" "{w}"'


def test_text_keeps_the_original_path(folders):
    # What a command writes or prints lands in the user's project on Keep, where the working copy's path is meaningless.
    _, project, work = folders
    p, w = str(project), str(work)
    script = f"cat > run.sh <<'EOF'\ncd \"{p}\" && python3 app.py\nEOF\ncd \"{p}\" && sh run.sh"
    assert _map(script, *folders) == f"cat > run.sh <<'EOF'\ncd \"{p}\" && python3 app.py\nEOF\ncd \"{w}\" && sh run.sh"
    for text in (f'echo "cd {p}" >> notes.txt', f"printf '%s\\n' \"{p}\"", f"ls  # {p}",
                 f"python3 -c \"import os; os.chdir('{p}')\"", f"sed -i '' 's|{p}|.|' notes.txt"):
        assert _map(text, *folders) == text


def test_the_original_stays_for_what_the_copy_leaves_out_for_git_and_for_other_folders(folders):
    _, project, _ = folders
    p = str(project)
    for command in (f'"{p}/.venv/bin/python" -m pytest', f'cat "{p}/.git/HEAD"', f'git -C "{p}" log --oneline',
                    f'ls "{p}-old" "{p}2" "{p}.bak"'):
        assert _map(command, *folders) == command


def test_a_working_copy_inside_the_project_is_left_alone(folders):
    home, _, work = folders
    command = f"cd {work} && ls {home}/notes"
    assert into_working_copy(command, project=home, workdir=work, home=home) == f"cd {work} && ls {work}/notes"
