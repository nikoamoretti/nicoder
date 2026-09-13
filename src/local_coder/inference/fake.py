"""Scripted fake adapter for offline testing. VISIBLY FAKE.

Supplies canned ACTION strings only. It never supplies test results: every
CHECK outcome comes from real skill execution and real pytest evidence.
Fake runs are labeled fake in every record and CLI summary.

Milestone-1 demo profiles (fake:success, fake:wrong, fake:blocked) are
coupled to fixtures/demo_calc; test_fake_matches_gold pins the success
diff byte-identical to fixtures/demo_calc/gold.diff so drift fails loudly.
"""
from __future__ import annotations

import json

from .base import AdapterError, ModelAdapter

DEMO_SUCCESS_DIFF = '''--- a/src/calc.py
+++ b/src/calc.py
@@ -2,7 +2,7 @@
 
 def add(a, b):
     """Return the sum of a and b."""
-    return a - b  # BUG: subtracts instead of adding
+    return a + b
 
 
 def mul(a, b):
'''

DEMO_WRONG_DIFF = '''--- a/src/calc.py
+++ b/src/calc.py
@@ -2,7 +2,7 @@
 
 def add(a, b):
     """Return the sum of a and b."""
-    return a - b  # BUG: subtracts instead of adding
+    return a - b + 1
 
 
 def mul(a, b):
'''

_FIND = '{"action":"find","query":"add subtracts calc","symbols":["add"],\
"hypothesis":"add() in src/calc.py implements subtraction"}'
_CHECK = '{"action":"check","scope":"focused"}'


def demo_script(profile: str) -> list[str]:
    """Canned raw responses for the Milestone-1 demo fixture."""
    if profile == "fake:success":
        return [
            _FIND,
            '{"action":"patch","operation":"apply","diff":'
            + json.dumps(DEMO_SUCCESS_DIFF)
            + ',"expected_effect":"add() returns the sum"}',
            _CHECK,
            '{"action":"done","status":"solved","summary":"Fixed add() to sum instead of subtract."}',
        ]
    if profile == "fake:wrong":
        return [
            _FIND,
            '{"action":"patch","operation":"apply","diff":'
            + json.dumps(DEMO_WRONG_DIFF)
            + ',"expected_effect":"attempted fix for add()"}',
            _CHECK,
            '{"action":"done","status":"solved","summary":"Attempted fix for add()."}',
            '{"action":"done","status":"blocked","reason_code":"validation_failed",\
"summary":"Validation rejected the fix; add() still fails."}',
        ]
    if profile == "fake:blocked":
        return [
            '{"action":"find","query":"zzz-no-such-identifier-zzz","symbols":["NoSuchSymbol"],\
"hypothesis":"failure location unknown"}',
            '{"action":"done","status":"blocked","reason_code":"insufficient_evidence",\
"summary":"Could not localize the failure within budget."}',
        ]
    raise AdapterError(f"unknown fake profile {profile!r}")


class ScriptedFakeAdapter(ModelAdapter):
    """Returns script[step]. Exhaustion and transport faults consume a slot."""

    name = "fake"

    def __init__(self, script: list[str], profile: str = "scripted") -> None:
        self.script = list(script)
        self.profile = profile
        self.calls = 0

    def generate(self, briefing: str, step: int) -> str:
        """Return the canned response indexed by generation slot."""
        self.calls += 1
        self.last_request = {
            "adapter": "fake",
            "profile": self.profile,
            "briefing_chars": len(briefing),
            "note": "scripted adapter: briefing and state are the exact inputs; no wire payload exists",
        }
        self.last_usage = None
        if step < 0 or step >= len(self.script):
            raise AdapterError(f"fake script exhausted at step {step} (profile {self.profile})")
        response = self.script[step]
        if response == "__TRANSPORT_ERROR__":
            raise AdapterError("fake transport failure (scripted)")
        return response

    def provenance(self) -> dict[str, str]:
        """Visibly-fake identity record."""
        return {
            "adapter": "fake",
            "profile": self.profile,
            "model": "scripted-fake",
            "note": "VISIBLY FAKE offline run; supplies actions only, never test results",
        }
