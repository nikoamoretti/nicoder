"""Protocol v3 end-to-end: span selection, bindings, mandatory verification."""
import json
import re

from local_coder.export import export_episode
from local_coder.inference.base import AdapterError, ModelAdapter

FIND = '{"action":"find","query":"add subtracts calc","symbols":["add"]}'
CHECK = '{"action":"check","scope":"focused"}'
DONE = '{"action":"done","status":"solved","summary":"Fixed add()."}'
BLOCKED = '{"action":"done","status":"blocked","reason_code":"t","summary":"stop"}'


class SpanSelectingFake(ModelAdapter):
    """Picks the span containing a marker by reading the actual briefing.

    This proves the displayed span catalogue is sufficient to target code:
    the test never receives bindings directly, only what the model sees.
    """

    name = "fake-span"

    def __init__(self, marker="a - b", replacement="a + b", script_tail=None,
                 bad_span=False, empty_new=False):
        self.marker = marker
        self.replacement = replacement
        self.calls = 0
        self.bad_span = bad_span
        self.empty_new = empty_new
        self.script_tail = list(script_tail or [CHECK, DONE])
        self.tail_index = 0

    def _pick_span(self, briefing):
        blocks = re.findall(r"\[(S\d+)\] (\S+) bytes \d+-\d+ sha=\S+\n(.*?)(?=\n\[S|\Z)",
                            briefing, flags=re.S)
        for span_id, path, text in blocks:
            if self.marker in text and path.endswith(".py"):
                return span_id, text
        raise AdapterError("no span containing marker found in briefing")

    def generate(self, briefing, step):
        self.calls += 1
        self.last_request = {"adapter": "fake", "profile": "span"}
        self.last_usage = None
        if step == 0:
            return FIND
        if step == 1:
            span_id, text = self._pick_span(briefing)
            if self.empty_new:
                new = ""
            else:
                new = text.replace(self.marker, self.replacement)
            if self.bad_span:
                span_id = "S99"
            return json.dumps({"action": "patch", "operation": "replace_span",
                               "edits": [{"span_id": span_id, "new": new}],
                               "expected_effect": "fix add"})
        if self.tail_index < len(self.script_tail):
            response = self.script_tail[self.tail_index]
            self.tail_index += 1
            return response
        raise AdapterError("script exhausted")

    def provenance(self):
        return {"adapter": "fake", "profile": "span", "note": "VISIBLY FAKE"}


def test_v3_span_episode_solves_with_mandatory_verification(repo_copy, state_dir):
    import sys

    from local_coder.config import load_config
    from local_coder.controller import Controller
    from local_coder.execution.trusted_local import TrustedLocalBackend
    from local_coder.persistence import EpisodeStore

    from .conftest import FIXTURE_DIR

    config = load_config(repo_copy)
    config.commands = {s: [sys.executable, *a[1:]] for s, a in config.commands.items()}
    store = EpisodeStore(state_dir)
    adapter = SpanSelectingFake()
    controller = Controller(store, config, adapter, TrustedLocalBackend(), repo_copy,
                            (FIXTURE_DIR / "ticket.md").read_text(),
                            episode_id="ep-v3-ok", protocol_version="3",
                            verification_policy="mandatory")
    state = controller.run()
    assert state.outcome == "solved", state.stop_reason
    assert state.auto_checks == 1
    assert state.generations_used == 4
    assert state.provenance["verification_policy"] == "mandatory"
    assert "return a + b" in (controller.workspace.root / "src/calc.py").read_text()
    records = export_episode(store, "ep-v3-ok", split="dev")
    assert all(r["protocol_version"] == "3" for r in records if r["complete"])
    assert any(r.get("verified_transition") for r in records)


def test_v3_unknown_span_id_rejected_before_apply(repo_copy, state_dir):
    import sys

    from local_coder.config import load_config
    from local_coder.controller import Controller
    from local_coder.execution.trusted_local import TrustedLocalBackend
    from local_coder.persistence import EpisodeStore

    from .conftest import FIXTURE_DIR

    config = load_config(repo_copy)
    config.commands = {s: [sys.executable, *a[1:]] for s, a in config.commands.items()}
    store = EpisodeStore(state_dir)
    adapter = SpanSelectingFake(bad_span=True, script_tail=[BLOCKED, BLOCKED])
    controller = Controller(store, config, adapter, TrustedLocalBackend(), repo_copy,
                            (FIXTURE_DIR / "ticket.md").read_text(),
                            episode_id="ep-v3-unknown", protocol_version="3",
                            verification_policy="mandatory")
    before = (repo_copy / "src/calc.py").read_bytes()
    state = controller.run()
    assert state.outcome == "blocked"
    assert state.patch_attempts == 1
    assert state.auto_checks == 0, "rejected edits must not trigger verification"
    observation = json.loads(store.read_turn_file("ep-v3-unknown", 2, "observation.json"))
    assert observation["status"] == "rejected"
    assert observation["code"] == "unknown_span_id"
    assert (controller.workspace.root / "src/calc.py").read_bytes() == before


def test_v3_mandatory_verification_catches_wrong_replacement(repo_copy, state_dir):
    import sys

    from local_coder.config import load_config
    from local_coder.controller import Controller
    from local_coder.execution.trusted_local import TrustedLocalBackend
    from local_coder.persistence import EpisodeStore

    from .conftest import FIXTURE_DIR

    config = load_config(repo_copy)
    config.commands = {s: [sys.executable, *a[1:]] for s, a in config.commands.items()}
    store = EpisodeStore(state_dir)
    adapter = SpanSelectingFake(marker="a - b", replacement="a - b + 1",
                                script_tail=[CHECK, BLOCKED, BLOCKED, BLOCKED])
    controller = Controller(store, config, adapter, TrustedLocalBackend(), repo_copy,
                            (FIXTURE_DIR / "ticket.md").read_text(),
                            episode_id="ep-v3-wrong", protocol_version="3",
                            verification_policy="mandatory")
    state = controller.run()
    assert state.outcome == "blocked"
    assert state.current is not None and state.current.status == "fail"
    observation = json.loads(store.read_turn_file("ep-v3-wrong", 2, "observation.json"))
    assert observation["status"] == "applied"
    assert observation["auto_check"]["status"] == "fail"
    assert any("test_add" in t for t in observation["auto_check"]["failed"])
