"""GBNF grammars for the Local Coder action protocol.

v1 mirrors protocol.py v1 exactly (unified-diff PATCH, legacy control).
v2 mirrors the exact-replacement PATCH; v3 mirrors span-ID selection PATCH.
Key order is fixed to the canonical briefing order. Versioned identities:
v2b/v3b bound JSON structural whitespace to a finite run per position while
leaving string contents (including escaped newlines) untouched. The
controller still validates every response independently: the grammar shapes
generation, it never asserts truth, and one compliant response never proves
decoder enforcement.
"""
# ruff: noqa: E501 -- GBNF rules are atomic; wrapping would corrupt the grammar.
from __future__ import annotations

CONSTRAINT_ID = "gbnf-action-v1"
CONSTRAINT_ID_V2 = "gbnf-action-v2b"
CONSTRAINT_ID_V3 = "gbnf-action-v3b"

_GRAMMAR = r"""root ::= "{" ws "\"action\"" ws ":" ws action ws "}"
action ::= find-action | patch-action | check-action | done-action
find-action ::= "\"find\"" ws "," ws "\"query\"" ws ":" ws string ws "," ws "\"symbols\"" ws ":" ws string-array ws ("," ws "\"hypothesis\"" ws ":" ws string ws)?
patch-action ::= "\"patch\"" ws "," ws "\"operation\"" ws ":" ws ("\"apply\"" ws "," ws "\"diff\"" ws ":" ws string ws "," ws "\"expected_effect\"" ws ":" ws string ws ("," ws "\"hypothesis\"" ws ":" ws string ws)? | "\"revert_last\"" ws "," ws "\"reason_code\"" ws ":" ws string ws ("," ws "\"hypothesis\"" ws ":" ws string ws)?)
check-action ::= "\"check\"" ws "," ws "\"scope\"" ws ":" ws ("\"focused\"" | "\"regression\"") ws ("," ws "\"hypothesis\"" ws ":" ws string ws)?
done-action ::= "\"done\"" ws "," ws "\"status\"" ws ":" ws ("\"solved\"" ws "," ws "\"summary\"" ws ":" ws string | "\"blocked\"" ws "," ws "\"reason_code\"" ws ":" ws string ws "," ws "\"summary\"" ws ":" ws string) ws
string-array ::= "[" ws (string (ws "," ws string)*)? ws "]"
string ::= "\"" ([^"\\\x7F\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\""
ws ::= [ \t\n]*
"""

_GRAMMAR_V2 = r"""root ::= "{" ws "\"action\"" ws ":" ws action ws "}"
action ::= find-action | patch-action | check-action | done-action
find-action ::= "\"find\"" ws "," ws "\"query\"" ws ":" ws string ws "," ws "\"symbols\"" ws ":" ws string-array ws ("," ws "\"hypothesis\"" ws ":" ws string ws)?
patch-action ::= "\"patch\"" ws "," ws "\"operation\"" ws ":" ws ("\"replace\"" ws "," ws "\"edits\"" ws ":" ws edit-array ws "," ws "\"expected_effect\"" ws ":" ws string ws ("," ws "\"hypothesis\"" ws ":" ws string ws)? | "\"revert_last\"" ws "," ws "\"reason_code\"" ws ":" ws string ws ("," ws "\"hypothesis\"" ws ":" ws string ws)?)
check-action ::= "\"check\"" ws "," ws "\"scope\"" ws ":" ws ("\"focused\"" | "\"regression\"") ws ("," ws "\"hypothesis\"" ws ":" ws string ws)?
done-action ::= "\"done\"" ws "," ws "\"status\"" ws ":" ws ("\"solved\"" ws "," ws "\"summary\"" ws ":" ws string | "\"blocked\"" ws "," ws "\"reason_code\"" ws ":" ws string ws "," ws "\"summary\"" ws ":" ws string) ws
edit-array ::= "[" ws (edit (ws "," ws edit)*)? ws "]"
edit ::= "{" ws "\"file_id\"" ws ":" ws ([1-9] [0-9]*) ws "," ws "\"old\"" ws ":" ws string ws "," ws "\"new\"" ws ":" ws string ws "}"
string-array ::= "[" ws (string (ws "," ws string)*)? ws "]"
string ::= "\"" ([^"\\\x7F\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\""
ws ::= [ \t\n]{0,40}
"""

_GRAMMAR_V3 = r"""root ::= "{" ws "\"action\"" ws ":" ws action ws "}"
action ::= find-action | patch-action | check-action | done-action
find-action ::= "\"find\"" ws "," ws "\"query\"" ws ":" ws string ws "," ws "\"symbols\"" ws ":" ws string-array ws ("," ws "\"hypothesis\"" ws ":" ws string ws)?
patch-action ::= "\"patch\"" ws "," ws "\"operation\"" ws ":" ws ("\"replace_span\"" ws "," ws "\"edits\"" ws ":" ws span-edit-array ws "," ws "\"expected_effect\"" ws ":" ws string ws ("," ws "\"hypothesis\"" ws ":" ws string ws)? | "\"revert_last\"" ws "," ws "\"reason_code\"" ws ":" ws string ws ("," ws "\"hypothesis\"" ws ":" ws string ws)?)
check-action ::= "\"check\"" ws "," ws "\"scope\"" ws ":" ws ("\"focused\"" | "\"regression\"") ws ("," ws "\"hypothesis\"" ws ":" ws string ws)?
done-action ::= "\"done\"" ws "," ws "\"status\"" ws ":" ws ("\"solved\"" ws "," ws "\"summary\"" ws ":" ws string | "\"blocked\"" ws "," ws "\"reason_code\"" ws ":" ws string ws "," ws "\"summary\"" ws ":" ws string) ws
span-edit-array ::= "[" ws (span-edit (ws "," ws span-edit)*)? ws "]"
span-edit ::= "{" ws "\"span_id\"" ws ":" ws "\"S" ([1-9] [0-9]*) "\"" ws "," ws "\"new\"" ws ":" ws string ws "}"
string-array ::= "[" ws (string (ws "," ws string)*)? ws "]"
string ::= "\"" ([^"\\\x7F\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\""
ws ::= [ \t\n]{0,40}
"""


def action_grammar() -> str:
    """Return the v1 GBNF text (legacy control, unbounded whitespace)."""
    return _GRAMMAR


def action_grammar_v2() -> str:
    """Return the v2b GBNF text sent as the generation constraint."""
    return _GRAMMAR_V2


def action_grammar_v3() -> str:
    """Return the v3b GBNF text sent as the generation constraint."""
    return _GRAMMAR_V3


def constraint_id() -> str:
    """Stable identifier recorded per generation (never an enforcement claim)."""
    return CONSTRAINT_ID


def constraint_id_v2() -> str:
    """Stable v2b identifier recorded per generation."""
    return CONSTRAINT_ID_V2


def constraint_id_v3() -> str:
    """Stable v3b identifier recorded per generation."""
    return CONSTRAINT_ID_V3


GRAMMARS = {
    CONSTRAINT_ID: action_grammar,
    CONSTRAINT_ID_V2: action_grammar_v2,
    CONSTRAINT_ID_V3: action_grammar_v3,
}