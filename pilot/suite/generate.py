"""Generate the frozen 20-ticket pilot suite (deterministic, offline).

Produces 16 generated engineering tasks in 8 bug-class families plus
manifest entries for the 4 familiar curated fixtures. Variants in a family
share a bug CLASS but are distinct code (different domains, identifiers,
values, and tests); whole families are assigned to one split so related
tasks and their trajectories can never leak across splits.

Generated tasks are labeled origin=generated: they test engineering
correctness, not representative real-world coding performance.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SUITE_DIR = Path(__file__).resolve().parent
TASKS_DIR = SUITE_DIR / "tasks"
sys.path.insert(0, str(REPO_ROOT / "src"))

TOML = """\
version = 1
execution = "trusted-local"
editable_paths = ["src/**/*.py"]
readonly_paths = ["tests/**/*.py", "conftest.py", "local-coder.toml"]
test_paths = ["tests"]

[commands]
baseline = ["python3", "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider"]
focused = ["python3", "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider"]
regression = ["python3", "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider"]
final = ["python3", "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider"]

[acceptance]
preexisting_failures = []
"""

CONFTEST = '''"""Make the fixture root importable without installing anything."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
'''


def _diff(rel: str, old_lines: list[str], new_lines: list[str]) -> str:
    return "\n".join(difflib.unified_diff(
        old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")) + "\n"


def _replace(lines: list[str], old: str, new: str, count: int = 1) -> list[str]:
    text = "\n".join(lines)
    found = text.count(old)
    assert found >= count, f"pattern not found: {old!r}"
    return text.replace(old, new, count).split("\n")


FAMILIES: dict[str, dict] = {}


def family(name: str, split: str):
    """Register a bug-class family bound to one split."""

    def wrap(builder):
        FAMILIES[name] = {"split": split, "builder": builder}
        return builder

    return wrap


@family("arith-op", "dev")
def _arith(variant: str):
    if variant == "a":
        src = '''"""Temperature conversion helpers."""

def celsius_to_fahrenheit(celsius):
    """Convert Celsius to Fahrenheit."""
    return celsius * 9 / 5


def freezing_point():
    """Fahrenheit value of freezing water."""
    return 32.0
'''
        tests = '''"""Tests for temperature conversion."""

from src.convert import celsius_to_fahrenheit, freezing_point


def test_boiling():
    assert celsius_to_fahrenheit(100) == 212.0


def test_freezing():
    assert celsius_to_fahrenheit(0) == 32.0


def test_constant():
    assert freezing_point() == 32.0
'''
        return {"module": "convert", "src": src, "tests": tests,
                "gold": ("    return celsius * 9 / 5", "    return celsius * 9 / 5 + 32"),
                "wrong": ("    return celsius * 9 / 5", "    return celsius * 9 / 5 + 31"),
                "labels": (["src/convert.py"], ["celsius_to_fahrenheit"]),
                "ticket": ("# Ticket: Celsius conversion drops the offset\n\n"
                           "`celsius_to_fahrenheit` scales but never adds 32, so 0C maps to 0F. "
                           "Add the missing offset; `freezing_point()` is correct and must keep working.")}
    src = '''"""Shop discount helpers."""

LOYALTY_RATE = 0.05


def apply_discount(price, fraction):
    """Apply a fractional discount to a price."""
    return price * fraction


def loyalty_price(price):
    """Apply the fixed loyalty discount."""
    return price * (1 - LOYALTY_RATE)
'''
    tests = '''"""Tests for shop discounts."""

from src.shop import apply_discount, loyalty_price


def test_half_off():
    assert apply_discount(80.0, 0.5) == 40.0


def test_no_discount():
    assert apply_discount(80.0, 0.0) == 80.0


def test_loyalty():
    assert loyalty_price(100.0) == 95.0
'''
    return {"module": "shop", "src": src, "tests": tests,
            "gold": ("    return price * fraction", "    return price * (1 - fraction)"),
            "wrong": ("    return price * fraction", "    return price * (1 - fraction) + 1"),
            "labels": (["src/shop.py"], ["apply_discount"]),
            "ticket": ("# Ticket: discount computes the taken-off part, not the price\n\n"
                       "`apply_discount(80, 0.5)` returns 40 only by coincidence of the test "
                       "values... actually it returns the discount amount instead of the final "
                       "price. Charge `price * (1 - fraction)`; `loyalty_price()` is correct.")}


@family("off-by-one", "dev")
def _offbyone(variant: str):
    if variant == "a":
        src = '''"""Small sequence helpers."""

def first_n(items, n):
    """Return the first n items."""
    return items[:n - 1]


def last(items):
    """Return the final item."""
    return items[-1]
'''
        tests = '''"""Tests for sequence helpers."""

from src.seqs import first_n, last


def test_first_two():
    assert first_n([1, 2, 3], 2) == [1, 2]


def test_first_zero():
    assert first_n([1, 2, 3], 0) == []


def test_last():
    assert last([1, 2, 3]) == 3
'''
        return {"module": "seqs", "src": src, "tests": tests,
                "gold": ("    return items[:n - 1]", "    return items[:n]"),
                "wrong": ("    return items[:n - 1]", "    return items[:n + 1]"),
                "labels": (["src/seqs.py"], ["first_n"]),
                "ticket": ("# Ticket: first_n drops one element too many\n\n"
                           "`first_n([1, 2, 3], 2)` returns `[1]`. Return exactly the first n "
                           "items; `last()` is correct.")}
    src = '''"""Thermostat schedule helpers."""

def readings_between(low, high):
    """Hourly readings from low to high, inclusive of both ends."""
    return list(range(low, high))


def midnight():
    """First hour of the day."""
    return 0
'''
    tests = '''"""Tests for schedule helpers."""

from src.schedule import midnight, readings_between


def test_span():
    assert readings_between(9, 12) == [9, 10, 11, 12]


def test_single():
    assert readings_between(9, 9) == [9]


def test_midnight():
    assert midnight() == 0
'''
    return {"module": "schedule", "src": src, "tests": tests,
            "gold": ("    return list(range(low, high))", "    return list(range(low, high + 1))"),
            "wrong": ("    return list(range(low, high))", "    return list(range(low - 1, high))"),
            "labels": (["src/schedule.py"], ["readings_between"]),
            "ticket": ("# Ticket: schedule range excludes the closing hour\n\n"
                       "`readings_between(9, 12)` stops at 11. Both ends are inclusive; "
                       "`midnight()` is correct.")}


@family("swapped-args", "dev")
def _swapped(variant: str):
    if variant == "a":
        src = '''"""Mailing label helpers."""

def make_label(name, code):
    """Render a label as CODE-name."""
    return f"{name}-{code}"


def make_tag(code):
    """Render a bare tag."""
    return f"[{code}]"
'''
        tests = '''"""Tests for mailing labels."""

from src.labels import make_label, make_tag


def test_label():
    assert make_label("ada", "X1") == "X1-ada"


def test_tag():
    assert make_tag("X1") == "[X1]"
'''
        return {"module": "labels", "src": src, "tests": tests,
                "gold": ('    return f"{name}-{code}"', '    return f"{code}-{name}"'),
                "wrong": ('    return f"{name}-{code}"', '    return f"{code}_{name}"'),
                "labels": (["src/labels.py"], ["make_label"]),
                "ticket": ("# Ticket: label fields are swapped\n\n"
                           "`make_label(\"ada\", \"X1\")` renders `ada-X1` but the code must come "
                           "first. `make_tag()` is correct.")}
    src = '''"""Canvas movement helpers."""

STEP = 1


def move_point(x, y, dx, dy):
    """Shift a point by (dx, dy)."""
    return (x + dy, y + dx)


def origin():
    """The canvas origin."""
    return (0, 0)
'''
    tests = '''"""Tests for canvas movement."""

from src.canvas import move_point, origin


def test_move():
    assert move_point(3, 4, 1, 2) == (4, 6)


def test_still():
    assert move_point(3, 4, 0, 0) == (3, 4)


def test_origin():
    assert origin() == (0, 0)
'''
    return {"module": "canvas", "src": src, "tests": tests,
            "gold": ("    return (x + dy, y + dx)", "    return (x + dx, y + dy)"),
            "wrong": ("    return (x + dy, y + dx)", "    return (x + dx, y - dy)"),
            "labels": (["src/canvas.py"], ["move_point"]),
            "ticket": ("# Ticket: movement applies deltas to the wrong axes\n\n"
                       "`move_point(3, 4, 1, 2)` yields `(5, 5)`. Apply dx to x and dy to y; "
                       "`origin()` is correct.")}


@family("missing-key", "dev")
def _missingkey(variant: str):
    if variant == "a":
        src = '''"""Client configuration helpers."""

DEFAULT_TIMEOUT = 30


def get_timeout(config):
    """Read the timeout, defaulting to DEFAULT_TIMEOUT."""
    return config["timeout"]


def default_timeout():
    """The built-in default timeout."""
    return DEFAULT_TIMEOUT
'''
        tests = '''"""Tests for client configuration."""

from src.client import default_timeout, get_timeout


def test_explicit():
    assert get_timeout({"timeout": 5}) == 5


def test_missing_uses_default():
    assert get_timeout({}) == 30


def test_default():
    assert default_timeout() == 30
'''
        return {"module": "client", "src": src, "tests": tests,
                "gold": ('    return config["timeout"]', '    return config.get("timeout", DEFAULT_TIMEOUT)'),
                "wrong": ('    return config["timeout"]', '    return config.get("timeout", 0)'),
                "labels": (["src/client.py"], ["get_timeout"]),
                "ticket": ("# Ticket: missing timeout raises instead of defaulting\n\n"
                           "`get_timeout({})` raises KeyError. Fall back to DEFAULT_TIMEOUT; "
                           "`default_timeout()` is correct.")}
    src = '''"""Greeting helpers."""

def welcome(user):
    """Greet by nickname, falling back to the full name."""
    return "hi " + user["nickname"]


def formal(user):
    """Formal greeting by full name."""
    return "hello " + user["name"]
'''
    tests = '''"""Tests for greetings."""

from src.greet import formal, welcome


def test_nickname():
    assert welcome({"name": "Ada L", "nickname": "ada"}) == "hi ada"


def test_fallback():
    assert welcome({"name": "Ada L"}) == "hi Ada L"


def test_formal():
    assert formal({"name": "Ada L"}) == "hello Ada L"
'''
    return {"module": "greet", "src": src, "tests": tests,
            "gold": ('    return "hi " + user["nickname"]',
                     '    return "hi " + user.get("nickname", user["name"])'),
            "wrong": ('    return "hi " + user["nickname"]', '    return "hi stranger"'),
            "labels": (["src/greet.py"], ["welcome"]),
            "ticket": ("# Ticket: greeting crashes without a nickname\n\n"
                       "`welcome({\"name\": \"Ada L\"})` raises KeyError. Fall back to the full "
                       "name; `formal()` is correct.")}


@family("inverted-condition", "held-out")
def _inverted(variant: str):
    if variant == "a":
        src = '''"""Age gate helpers."""

LIMIT = 18


def is_adult(age):
    """True once LIMIT is reached."""
    return age < LIMIT


def limit():
    """The age limit."""
    return LIMIT
'''
        tests = '''"""Tests for the age gate."""

from src.agegate import is_adult, limit


def test_adult():
    assert is_adult(20) is True


def test_minor():
    assert is_adult(10) is False


def test_boundary():
    assert is_adult(18) is True


def test_limit():
    assert limit() == 18
'''
        return {"module": "agegate", "src": src, "tests": tests,
                "gold": ("    return age < LIMIT", "    return age >= LIMIT"),
                "wrong": ("    return age < LIMIT", "    return age > LIMIT"),
                "labels": (["src/agegate.py"], ["is_adult"]),
                "ticket": ("# Ticket: age gate is inverted\n\n"
                           "`is_adult(20)` is False. Adulthood starts at LIMIT inclusive; "
                           "`limit()` is correct.")}
    src = '''"""List helpers."""

def is_empty(items):
    """True when there is nothing to show."""
    return len(items) > 0


def first_or(items, fallback):
    """First item, or the fallback."""
    return items[0] if items else fallback
'''
    tests = '''"""Tests for list helpers."""

from src.lists import first_or, is_empty


def test_empty():
    assert is_empty([]) is True


def test_full():
    assert is_empty([1]) is False


def test_fallback():
    assert first_or([], "x") == "x"
'''
    return {"module": "lists", "src": src, "tests": tests,
            "gold": ("    return len(items) > 0", "    return len(items) == 0"),
            "wrong": ("    return len(items) > 0", "    return len(items) >= 0"),
            "labels": (["src/lists.py"], ["is_empty"]),
            "ticket": ("# Ticket: emptiness check is inverted\n\n"
                       "`is_empty([])` is False. Empty means zero items; `first_or()` "
                       "is correct.")}


@family("wrong-constant", "held-out")
def _constant(variant: str):
    if variant == "a":
        src = '''"""API paging helpers."""

DEFAULT_LIMIT = 50


def page_params(page):
    """Paging parameters honoring the documented default limit."""
    return {"page": page, "limit": DEFAULT_LIMIT}


def max_pages(total):
    """Page count for a result total."""
    return (total + DEFAULT_LIMIT - 1) // DEFAULT_LIMIT
'''
        tests = '''"""Tests for API paging."""

from src.paging import DEFAULT_LIMIT, max_pages, page_params


def test_default_limit():
    assert page_params(2) == {"page": 2, "limit": 25}


def test_pages():
    assert max_pages(50) == 2


def test_constant_documented():
    assert DEFAULT_LIMIT == 25
'''
        return {"module": "paging", "src": src, "tests": tests,
                "gold": ("DEFAULT_LIMIT = 50", "DEFAULT_LIMIT = 25"),
                "wrong": ("DEFAULT_LIMIT = 50", "DEFAULT_LIMIT = 24"),
                "labels": (["src/paging.py"], ["page_params"]),
                "ticket": ("# Ticket: default page limit disagrees with the docs\n\n"
                           "The rate card allows 25 items per page, but DEFAULT_LIMIT is 50. "
                           "Correct the constant; both helpers derive from it.")}
    src = '''"""Upload retry helpers."""

MAX_RETRIES = 5


def retries_left(attempt):
    """Retries remaining after an attempt number."""
    return MAX_RETRIES - attempt


def may_retry(attempt):
    """Whether another attempt is allowed."""
    return attempt < MAX_RETRIES
'''
    tests = '''"""Tests for upload retries."""

from src.upload import MAX_RETRIES, may_retry, retries_left


def test_remaining():
    assert retries_left(1) == 2


def test_allowed():
    assert may_retry(2) is True


def test_constant_documented():
    assert MAX_RETRIES == 3
'''
    return {"module": "upload", "src": src, "tests": tests,
            "gold": ("MAX_RETRIES = 5", "MAX_RETRIES = 3"),
            "wrong": ("MAX_RETRIES = 5", "MAX_RETRIES = 4"),
            "labels": (["src/upload.py"], ["retries_left"]),
            "ticket": ("# Ticket: retry budget exceeds the documented policy\n\n"
                       "Policy allows 3 retries, but MAX_RETRIES is 5. Correct the "
                       "constant; both helpers derive from it.")}


@family("str-join", "held-out")
def _strjoin(variant: str):
    if variant == "a":
        src = '''"""CSV helpers."""

def csv_line(cells):
    """Render one comma-separated line."""
    return ";".join(cells)


def csv_header(cells):
    """Render a header line (already comma-separated upstream)."""
    return ",".join(cells)
'''
        tests = '''"""Tests for CSV helpers."""

from src.csvout import csv_header, csv_line


def test_line():
    assert csv_line(["a", "b"]) == "a,b"


def test_single():
    assert csv_line(["a"]) == "a"


def test_header():
    assert csv_header(["a", "b"]) == "a,b"
'''
        return {"module": "csvout", "src": src, "tests": tests,
                "gold": ('    return ";".join(cells)', '    return ",".join(cells)'),
                "wrong": ('    return ";".join(cells)', '    return " ".join(cells)'),
                "labels": (["src/csvout.py"], ["csv_line"]),
                "ticket": ("# Ticket: CSV lines use the wrong separator\n\n"
                           "`csv_line([\"a\", \"b\"])` yields `a;b`. Use commas; `csv_header()` "
                           "is correct.")}
    src = '''"""Badge helpers."""

def initials(first, last):
    """First-then-last initials."""
    return f"{last[0]}{first[0]}"


def monogram(first, middle, last):
    """Three-letter monogram in order."""
    return f"{first[0]}{middle[0]}{last[0]}"
'''
    tests = '''"""Tests for badges."""

from src.badges import initials, monogram


def test_initials():
    assert initials("Ada", "Lovelace") == "AL"


def test_monogram():
    assert monogram("Ada", "B", "Lovelace") == "ABL"
'''
    return {"module": "badges", "src": src, "tests": tests,
            "gold": ('    return f"{last[0]}{first[0]}"', '    return f"{first[0]}{last[0]}"'),
            "wrong": ('    return f"{last[0]}{first[0]}"', '    return f"{first[0]}.{last[0]}"'),
            "labels": (["src/badges.py"], ["initials"]),
            "ticket": ("# Ticket: initials are swapped\n\n"
                       "`initials(\"Ada\", \"Lovelace\")` yields `LA`. First name comes first; "
                       "`monogram()` is correct.")}


@family("early-return", "held-out")
def _earlyreturn(variant: str):
    if variant == "a":
        src = '''"""Cart helpers."""

def cart_total(prices):
    """Sum all prices in the cart."""
    out = 0
    for price in prices:
        out += price
        return out
    return out


def cart_count(prices):
    """Number of items in the cart."""
    return len(prices)
'''
        tests = '''"""Tests for cart helpers."""

from src.cart import cart_count, cart_total


def test_total():
    assert cart_total([4, 5, 6]) == 15


def test_empty():
    assert cart_total([]) == 0


def test_count():
    assert cart_count([4, 5, 6]) == 3
'''
        return {"module": "cart", "src": src, "tests": tests,
                "gold": ("        out += price\n        return out\n    return out",
                         "        out += price\n    return out"),
                "wrong": ("        out += price\n        return out\n    return out",
                          "        out += price\n        return out\n    return out + 1"),
                "labels": (["src/cart.py"], ["cart_total"]),
                "ticket": ("# Ticket: cart total returns after the first item\n\n"
                           "`cart_total([4, 5, 6])` returns 4. Accumulate the whole cart; "
                           "`cart_count()` is correct.")}
    src = '''"""Score helpers."""

PASS_MARK = 60


def passed_all(scores):
    """True when every score reaches PASS_MARK."""
    for score in scores:
        if score < PASS_MARK:
            return False
        return True
    return True


def average(scores):
    """Mean score."""
    return sum(scores) / len(scores)
'''
    tests = '''"""Tests for score helpers."""

from src.scores import average, passed_all


def test_all_pass():
    assert passed_all([70, 80]) is True


def test_one_fails():
    assert passed_all([70, 40]) is False


def test_average():
    assert average([70, 80]) == 75.0
'''
    return {"module": "scores", "src": src, "tests": tests,
            "gold": ("        if score < PASS_MARK:\n            return False\n        return True\n    return True",
                     "        if score < PASS_MARK:\n            return False\n    return True"),
            "wrong": ("        if score < PASS_MARK:\n            return False\n        return True\n    return True",
                      "        if score < PASS_MARK:\n            return False\n        return False\n    return True"),
            "labels": (["src/scores.py"], ["passed_all"]),
            "ticket": ("# Ticket: pass check stops after the first score\n\n"
                       "`passed_all([70, 40])` returns True. Every score must reach "
                       "PASS_MARK; `average()` is correct.")}


FAMILIAR = [
    {"id": "fam-demo-calc", "family": "demo", "fixture": "demo_calc"},
    {"id": "fam-localization", "family": "localization", "fixture": "localization"},
    {"id": "fam-multi-hunk", "family": "multi-hunk", "fixture": "multi_hunk"},
    {"id": "fam-recovery", "family": "recovery", "fixture": "recovery"},
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_task(task_id: str, family: str, split: str, spec: dict) -> dict:
    from local_coder.config import load_config
    from local_coder.paths import original_manifest, tree_hash

    root = TASKS_DIR / task_id
    repo = root / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "__init__.py").write_text('"""Generated fixture package."""\n')
    (repo / "tests" / "__init__.py").write_text('"""Generated fixture tests."""\n')
    (repo / "conftest.py").write_text(CONFTEST)
    (repo / "local-coder.toml").write_text(TOML)
    (repo / "src" / f"{spec['module']}.py").write_text(spec["src"])
    (repo / "tests" / f"test_{spec['module']}.py").write_text(spec["tests"])
    (root / "ticket.md").write_text(spec["ticket"])
    old_lines = spec["src"].splitlines()
    gold_new = _replace(old_lines, *spec["gold"])
    wrong_new = _replace(old_lines, *spec["wrong"])
    rel = f"src/{spec['module']}.py"
    (root / "gold.diff").write_text(_diff(rel, old_lines, gold_new))
    (root / "wrong.diff").write_text(_diff(rel, old_lines, wrong_new))
    (root / "gold.json").write_text(json.dumps(
        {"files": spec["labels"][0], "symbols": spec["labels"][1]}, indent=2) + "\n")
    (root / "provenance.json").write_text(json.dumps({
        "origin": "generated", "family": family, "split": split,
        "generator": "pilot/suite/generate.py", "suite_version": 1,
        "note": "engineering fixture, not representative real-world performance",
    }, indent=2) + "\n")
    manifest = original_manifest(repo)
    test_files = sorted(str(p.relative_to(repo).as_posix())
                        for p in (repo / "tests").glob("test_*.py"))
    config = load_config(repo)
    return {
        "id": task_id, "family": family, "split": split, "origin": "generated",
        "repo": f"pilot/suite/tasks/{task_id}/repo",
        "ticket": f"pilot/suite/tasks/{task_id}/ticket.md",
        "gold": f"pilot/suite/tasks/{task_id}/gold.diff",
        "wrong": f"pilot/suite/tasks/{task_id}/wrong.diff",
        "labels": f"pilot/suite/tasks/{task_id}/gold.json",
        "hashes": {
            "repo_tree": tree_hash(manifest),
            "ticket_sha": _sha(spec["ticket"])[:16],
            "tests": {t: manifest[t] for t in test_files},
            "gold_sha": _sha((root / "gold.diff").read_text())[:16],
        },
        "env": {
            "worker_image": "local-coder-worker:1",
            "registry_hash": config.registry_hash(),
            "package": __import__("local_coder").__version__,
        },
    }


def _familiar_entry(item: dict) -> dict:
    from local_coder.config import load_config
    from local_coder.paths import original_manifest, tree_hash

    fix = REPO_ROOT / "fixtures" / item["fixture"]
    repo = fix / "repo"
    manifest = original_manifest(repo)
    test_files = sorted(str(p.relative_to(repo).as_posix()) for p in (repo / "tests").glob("test_*.py"))
    config = load_config(repo)
    labels = json.loads((fix / "gold.json").read_text())
    return {
        "id": item["id"], "family": item["family"], "split": "dev", "origin": "curated",
        "repo": f"fixtures/{item['fixture']}/repo",
        "ticket": f"fixtures/{item['fixture']}/ticket.md",
        "gold": f"fixtures/{item['fixture']}/gold.diff",
        "wrong": f"fixtures/{item['fixture']}/wrong.diff"
        if (fix / "wrong.diff").exists() else None,
        "labels": f"fixtures/{item['fixture']}/gold.json",
        "label_files": labels["files"], "label_symbols": labels["symbols"],
        "hashes": {
            "repo_tree": tree_hash(manifest),
            "ticket_sha": _sha((fix / "ticket.md").read_text())[:16],
            "tests": {t: manifest[t] for t in test_files},
            "gold_sha": _sha((fix / "gold.diff").read_text())[:16],
        },
        "env": {
            "worker_image": "local-coder-worker:1",
            "registry_hash": config.registry_hash(),
            "package": __import__("local_coder").__version__,
        },
    }


def main() -> None:
    """Generate tasks and freeze the manifest."""
    import shutil

    if TASKS_DIR.exists():
        shutil.rmtree(TASKS_DIR)
    entries: list[dict] = [_familiar_entry(item) for item in FAMILIAR]
    for family_name in sorted(FAMILIES):
        info = FAMILIES[family_name]
        for variant in ("a", "b"):
            spec = info["builder"](variant)
            task_id = f"gen-{family_name}-{variant}"
            entry = _write_task(task_id, family_name, info["split"], spec)
            entry["label_files"] = spec["labels"][0]
            entry["label_symbols"] = spec["labels"][1]
            entries.append(entry)
    manifest = {
        "suite_version": 1,
        "name": "pilot-20",
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "frozen_note": ("Do not modify held-out tasks or graders because the model fails. "
                        "Corrections require a new suite version with the reason preserved."),
        "splits": {"dev": [e["id"] for e in entries if e["split"] == "dev"],
                   "held-out": [e["id"] for e in entries if e["split"] == "held-out"]},
        "entries": entries,
    }
    (SUITE_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(entries)} tasks "
          f"({len(manifest['splits']['dev'])} dev, {len(manifest['splits']['held-out'])} held-out)")


if __name__ == "__main__":
    main()
