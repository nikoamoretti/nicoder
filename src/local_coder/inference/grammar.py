"""GBNF grammar for the Local Coder action protocol (constraint id gbnf-action-v1).

Mirrors protocol.py v1 exactly: the same four actions, required keys, and
enumerations; free-text values use the standard JSON string rule. Key order
is fixed to the canonical briefing order. The controller still validates
every response independently: the grammar shapes generation, it never
asserts truth, and one compliant response never proves decoder enforcement.
"""
# ruff: noqa: E501 -- GBNF rules are atomic; wrapping would corrupt the grammar.
from __future__ import annotations

CONSTRAINT_ID = "gbnf-action-v1"

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


def action_grammar() -> str:
    """Return the GBNF text sent as the generation constraint."""
    return _GRAMMAR


def constraint_id() -> str:
    """Stable identifier recorded per generation (never an enforcement claim)."""
    return CONSTRAINT_ID
