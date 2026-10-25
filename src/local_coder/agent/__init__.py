"""Nicoder's own agent: a model calls file and shell tools until it answers.

Not OpenCode, not Conductor, not any foreign agent CLI. The model may read the
home folder (minus secrets), change only the request's working copy, and run
commands in a macOS sandbox. Keep/Discard still decides what reaches the
user's folders.
"""
