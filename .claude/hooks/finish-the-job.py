#!/usr/bin/env python3
"""Stop hook: when Claude is about to end its turn by asking Nico for permission,
offering to do something, or handing him a step, send it back once to do the work.

It blocks at most once per turn (stop_hook_active), so a question that really
needs Nico still goes through on the second stop.
"""
import json
import re
import sys

OFFER = re.compile(
    r"\b("
    r"want me to|do you want|would you like|should i\b|shall i\b|"
    r"let me know|if you(?:'d| would)? (?:like|want|prefer)|"
    r"say (?:the word|go|yes)\b|say [\"“'‘]|just say|reply (?:go|yes|with)|type [\"“'‘]|"
    r"ready when you are|whenever you(?:'re| are) ready|"
    r"until you (?:give|say|approve|confirm|ok|reply|decide)|"
    r"(?:once|when|after) you (?:give|say|approve|confirm|ok|reply|decide)|"
    r"needs? your (?:go|ok|okay|approval|go-ahead|sign-?off|confirmation)|"
    r"waiting (?:for|on) (?:you|your)|your (?:call|decision)\b|up to you|"
    r"you(?:'ll| will)? need to|you need to|you can (?:now )?(?:run|open|click|go to)|"
    r"please (?:run|open|click|go to|paste|add|set|sign in|log in|approve|confirm)|"
    r"tell me (?:when|if|once|whether) you"
    r")",
    re.IGNORECASE,
)

REASON = (
    "Before ending: your last message asks Nico for permission, offers to do something, "
    "or hands him a step. Under 'How Claude works for Nico' (CLAUDE.md), if it is within "
    "your scope, do it now and report the result instead. Only these wait for him: spending "
    "beyond the ~$5/day API budget, emails or posts to other people in his name, his "
    "passwords/2FA or a login/consent screen, tests only he can do on his iPhone, and "
    "permanent deletion, force-pushes or history rewrites. If it is one of those, or he only "
    "asked a question, end your turn again unchanged."
)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if data.get("stop_hook_active"):
        return
    msg = (data.get("last_assistant_message") or "").strip()
    if not msg:
        return
    if OFFER.search(msg[-600:]):
        print(json.dumps({"decision": "block", "reason": REASON}))


if __name__ == "__main__":
    main()
