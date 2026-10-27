"""A command's step line says what the model is doing: its own words for the command, else words made from it.

Nico, 2026-10-27, on a step line that read "ran cd /Users/…/live/can…": "is there a way to have something more
descriptive or qualitative about what the model is doing".
"""
from __future__ import annotations

import pytest

from local_coder.agent.command_words import describe, from_command


def test_the_model_s_own_words_come_first():
    assert describe("cd /x && python3 -m pytest", "Look for stale pycache\nand other pov.py copies.") == (
        "Look for stale pycache and other pov.py copies")
    assert describe("python3 -m pytest", "   ") == "Run the tests"
    long = describe("ls", "word " * 40)
    assert len(long) == 90 and long.endswith("…")


@pytest.mark.parametrize(("command", "words"), [
    ("cd /Users/nico/.local/share/lc/live/candidate && python3 -m unittest pov_test.py -v 2>&1 | tail -25",
     "Run the tests"),
    ("PYTHONPATH=src python3 -m pytest tests/ -q", "Run the tests"),
    ("bash -c 'cd src && pytest -x'", "Run the tests"),
    ("npm test", "Run the tests"),
    ("pip install requests bs4", "Install packages"),
    ("npm run build", "Build the project"),
    ("npm run lint", "Run the lint script"),
    ("python3 scrape.py --live", "Run scrape.py"),
    ("python3 - <<'EOF'\nimport os\nprint(os.getcwd())\nEOF", "Try some Python"),
    ("ls -la src", "List the files in src"),
    ("sed -n '1,80p' src/app.py", "Read app.py"),
    ("tail -f server.log", "Read server.log"),
    ('grep -rn -A 3 "def main" .', "Search for “def main”"),
    ("git log --oneline -5", "Read the git history"),
    ("curl -s -H 'Accept: x' https://api.example.com/v1/items", "Fetch api.example.com"),
    ("cat > notes.md <<'EOF'\ncd /x && make\nEOF", "Write notes.md"),
    ("sleep 5 && curl localhost:8000/health", "Fetch localhost:8000"),
    ("export X=1 && rm -rf build dist", "Delete build and 1 more"),
    ("node --version", "Check the node version"),
    ("vercel deploy --prod", "Run vercel deploy"),
    ("cd src", "Set up the shell"),
])
def test_words_made_from_a_command_the_model_did_not_describe(command, words):
    assert from_command(command) == words
