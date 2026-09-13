# Capability probes for the repair comparison (task-independent prompts)

Six probe generations, all outside episodes and uncounted toward any
ticket budget. Prompts never mention tasks, repos, or gold content.

1. v2 grammar CHECK (`{"action":"check","scope":"focused"}` instruction):
   flat CHECK action in reasoning channel, finish stop. Grammar binds.
2. v2 replace toy (toy `src/toy.py` greeting, hello-comma change):
   replace-shaped action but single-line-collapsed old text, missing
   `expected_effect`, whitespace stall to `length`. Evidence that grammar
   shapes structure, not exactness or completion.
3. Rewritten-grammar CHECK re-verification (after a bad grammar rewrite):
   flat CHECK in reasoning, finish stop. Restored grammar binds.
4. thinking=auto probe on thinking server: legacy fallback reads the
   reasoning channel; valid action. Baseline behavior confirmed.
5. thinking=off probe on thinking server: compatibility failure raised
   (reasoning-only output rejected, no action executed). The probe verifies
   instead of assuming.
6. thinking=off + v2 grammar probe on `--reasoning off` server: action in
   final content channel, no reasoning output, 19 completion tokens.
   Non-thinking mode verified effective, not merely accepted.

Server for probes 1-5: llama.cpp 0.4.0 (build 10809), Qwen3.5-4B-Q4_K_M,
`-c 8192`, Metal offload, default thinking. Probe 6 and all B/C cells:
same binary/model with `--reasoning off` (template pre-closes thinking;
verified by empty reasoning channel, not by parameter acceptance —
a top-level `enable_thinking:false` request parameter was tested and
shown to be ignored by this build).
