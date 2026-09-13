"""Strict four-action schema: parsing, rejection, fingerprints."""
import json

import pytest

from local_coder.protocol import (
    CheckAction,
    DoneBlockedAction,
    DoneSolvedAction,
    FindAction,
    PatchApplyAction,
    PatchRevertAction,
    ProtocolError,
    fingerprint,
    parse_raw,
)


def test_valid_find():
    action = parse_raw('{"action":"find","query":"refresh session","symbols":["refresh_session"]}')
    assert isinstance(action, FindAction)
    assert action.symbols == ["refresh_session"]


def test_valid_patch_apply_and_revert():
    apply = parse_raw(json.dumps({"action": "patch", "operation": "apply",
                                  "diff": "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
                                  "expected_effect": "fix"}))
    assert isinstance(apply, PatchApplyAction)
    revert = parse_raw('{"action":"patch","operation":"revert_last","reason_code":"regression"}')
    assert isinstance(revert, PatchRevertAction)


def test_valid_check_and_done():
    assert isinstance(parse_raw('{"action":"check","scope":"focused"}'), CheckAction)
    assert isinstance(parse_raw('{"action":"done","status":"solved","summary":"ok"}'), DoneSolvedAction)
    blocked = parse_raw('{"action":"done","status":"blocked","reason_code":"x","summary":"y"}')
    assert isinstance(blocked, DoneBlockedAction)


def test_empty_rejected():
    with pytest.raises(ProtocolError) as exc:
        parse_raw("   ")
    assert exc.value.code == "empty"


def test_invalid_json_rejected():
    with pytest.raises(ProtocolError) as exc:
        parse_raw('{"action": "find",')
    assert exc.value.code == "invalid_json"


def test_markdown_fence_rejected():
    with pytest.raises(ProtocolError) as exc:
        parse_raw('```json\n{"action":"find","query":"x"}\n```')
    assert exc.value.code == "markdown_fence"


def test_multiple_actions_rejected():
    with pytest.raises(ProtocolError) as exc:
        parse_raw('[{"action":"find","query":"x"},{"action":"check","scope":"focused"}]')
    assert exc.value.code == "multiple_actions"


def test_unknown_action_rejected():
    with pytest.raises(ProtocolError) as exc:
        parse_raw('{"action":"exec","cmd":"rm -rf /"}')
    assert exc.value.code == "unknown_action"


def test_unknown_fields_rejected():
    with pytest.raises(ProtocolError) as exc:
        parse_raw('{"action":"find","query":"x","run_shell":"pytest"}')
    assert exc.value.code == "invalid_schema"
    with pytest.raises(ProtocolError) as exc:
        parse_raw('{"action":"check","scope":"focused","argv":["evil"]}')
    assert exc.value.code == "invalid_schema"


def test_bad_scope_and_missing_fields_rejected():
    with pytest.raises(ProtocolError) as exc:
        parse_raw('{"action":"check","scope":"everything"}')
    assert exc.value.code == "invalid_schema"
    with pytest.raises(ProtocolError) as exc:
        parse_raw('{"action":"patch","operation":"apply","diff":"x"}')
    assert exc.value.code == "invalid_schema"


def test_fingerprint_ignores_free_text_but_not_inputs():
    base = FindAction(action="find", query="q", symbols=["S"], hypothesis="h1")
    same = FindAction(action="find", query="q", symbols=["S"], hypothesis="h2")
    different = FindAction(action="find", query="q", symbols=["Other"])
    assert fingerprint(base, "tree") == fingerprint(same, "tree")
    assert fingerprint(base, "tree") != fingerprint(different, "tree")
    assert fingerprint(base, "tree-a") != fingerprint(base, "tree-b")


def _replace(**kwargs):
    base = {"action": "patch", "operation": "replace",
            "edits": [{"file_id": 1, "old": "a = 1", "new": "a = 2"}],
            "expected_effect": "fix"}
    base.update(kwargs)
    return json.dumps(base)


def test_v2_replace_parses():
    from local_coder.protocol import PatchReplaceAction

    action = parse_raw(_replace(), "2")
    assert isinstance(action, PatchReplaceAction)
    assert action.edits[0].file_id == 1


def test_v1_rejects_replace_operation():
    with pytest.raises(ProtocolError) as exc:
        parse_raw(_replace(), "1")
    assert exc.value.code == "unsupported_operation"


def test_v2_rejects_apply_operation():
    with pytest.raises(ProtocolError) as exc:
        parse_raw(json.dumps({"action": "patch", "operation": "apply",
                              "diff": "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
                              "expected_effect": "fix"}), "2")
    assert exc.value.code == "unsupported_operation"


def test_revert_parses_under_both_versions():
    from local_coder.protocol import PatchRevertAction

    raw = '{"action":"patch","operation":"revert_last","reason_code":"regression"}'
    assert isinstance(parse_raw(raw, "1"), PatchRevertAction)
    assert isinstance(parse_raw(raw, "2"), PatchRevertAction)


def test_v1_default_unchanged_for_legacy_callers():
    apply = parse_raw(json.dumps({"action": "patch", "operation": "apply",
                                  "diff": "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
                                  "expected_effect": "fix"}))
    assert isinstance(apply, PatchApplyAction)


def test_replace_field_validation():
    with pytest.raises(ProtocolError) as exc:
        parse_raw(_replace(edits=[]), "2")
    assert exc.value.code == "invalid_schema"
    with pytest.raises(ProtocolError) as exc:
        parse_raw(_replace(edits=[{"file_id": 0, "old": "a", "new": "b"}]), "2")
    assert exc.value.code == "invalid_schema"
    with pytest.raises(ProtocolError) as exc:
        parse_raw(_replace(edits=[{"file_id": 1, "old": "", "new": "b"}]), "2")
    assert exc.value.code == "invalid_schema"
    with pytest.raises(ProtocolError) as exc:
        parse_raw(_replace(edits=[{"file_id": 1, "old": "a", "new": "b", "extra": 1}]), "2")
    assert exc.value.code == "invalid_schema"


def test_replace_fingerprint_covers_edits_not_free_text():
    from local_coder.protocol import PatchReplaceAction

    base = PatchReplaceAction(action="patch", operation="replace",
                              edits=[{"file_id": 1, "old": "a", "new": "b"}],
                              expected_effect="e1")
    same_words = PatchReplaceAction(action="patch", operation="replace",
                                    edits=[{"file_id": 1, "old": "a", "new": "b"}],
                                    expected_effect="e2")
    different = PatchReplaceAction(action="patch", operation="replace",
                                   edits=[{"file_id": 1, "old": "a", "new": "c"}],
                                   expected_effect="e1")
    assert fingerprint(base, "tree") == fingerprint(same_words, "tree")
    assert fingerprint(base, "tree") != fingerprint(different, "tree")
