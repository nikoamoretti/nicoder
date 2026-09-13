# Local Coder

A bounded autonomous patcher for small, testable repository tickets, designed
for an Apple-silicon Mac with 16GB unified memory.

Workflow: `ticket → compact briefing → one model-selected action →
deterministic skill execution → structured observation → runtime-owned
state update → another decision or termination`. The local model sees exactly
four actions — **FIND, PATCH, CHECK, DONE** — and gets at most **eight**
model-generation attempts per episode. Software, not the model, owns budgets,
paths, test results, patch identity, progress, and completion decisions.

Milestone 1 (offline core): strict four-action protocol, bounded controller,
runtime-owned `state.json` / `briefing.txt` / `events.jsonl` / artifacts,
scripted fake adapter, real FIND/PATCH/CHECK skills over a disposable
candidate workspace, and a CLI. Milestone 2: reliable retrieval (ignore rules,
allow/deny policy, gold-label recall), transactional multi-hunk edits with
revert/recovery, adversarial CHECK validation (timeouts, floods, tamper,
drift), and a real containerized isolated backend.

## Setup

Requires Python 3.11+ and Docker (for isolated execution). Use a venv with
the locked dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -r requirements-dev.lock  # ruff + mypy
docker build -t local-coder-worker:1 -f docker/worker.Dockerfile docker/
```

## Commands

```bash
export PATH="<venv-bin-dir>:$PATH" PYTHONPATH=src
python3 -m local_coder.cli init --repo PATH
python3 -m local_coder.cli doctor --repo PATH
python3 -m local_coder.cli run --repo PATH --ticket-file ticket.md \
  --profile fake:success --execution trusted-local --state-dir var/episodes
python3 -m local_coder.cli inspect EPISODE_ID
python3 -m local_coder.cli diff EPISODE_ID
python3 -m local_coder.cli accept EPISODE_ID --repo PATH
python3 -m local_coder.cli discard EPISODE_ID
```

Omit `--execution` to use the default `isolated` backend (container worker,
`--network none`, fixed non-root user, mem/cpu/pids caps, only the candidate
tree and a reports dir mounted). `--execution trusted-local` is an explicit
opt-in to NON-SANDBOXED host execution for trusted fixtures; the CLI refuses
rather than silently falling back when isolation is unavailable.
`benchmark` and `export-traces` arrive in a later milestone and exit
honestly until then. Live local-model profiles (`llama`) need
`LOCAL_CODER_LLAMA_URL` pointing at a loopback llama.cpp server.

## Verified local profile (Prompt 3)

One working profile, measured on Apple M2 Pro / 16GB (target hardware class):

- Model: `bartowski/Qwen_Qwen3.5-4B-GGUF`, file `Qwen_Qwen3.5-4B-Q4_K_M.gguf`
  (official `Qwen/Qwen3.5-4B` post-trained weights, community imatrix quant),
  SHA-256 `13c16f42…cda65f8a983`, served with llama.cpp 0.4.0 (build 10809),
  `-c 8192`, full Metal offload, alias `Qwen3.5-4B-Q4_K_M`.
- Live episode `var/episodes-live/live-demo-calc-01` (preserved in the
  working tree, gitignored): demo_calc solved in 5 generations / 65s —
  FIND, rejected malformed PATCH, correct PATCH, CHECK pass, DONE validated.
  Server-measured 4306 prompt / 1591 completion tokens; template overhead
  measured 9 tokens; server RSS ~1.18GB, controller ~61MB.
- Missing: the other report candidates (Qwen2.5-Coder-7B-Instruct, Qwen3.5-9B)
  are not installed; no 16GB-suitability claim is made from one episode.

## Verification

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check .
PYTHONPATH=src .venv/bin/python -m mypy src/local_coder
```

## Layout

```text
src/local_coder/   application package (controller, skills, adapters, CLI)
tests/             offline test suite (no live calls by default)
fixtures/          real broken Python fixtures + tickets + gold diffs
                     demo_calc, localization, multi_hunk, recovery
docker/            isolated worker image definition
var/               gitignored runtime state (episodes, evidence)
```

## Status and honesty rules

- Fake-adapter runs are labeled `FAKE`; they never supply test results.
- Missing runtimes, models, and sandboxes are reported `BLOCKED`/`NOT RUN`, never passed.
- No training, cloud fallback, weight downloads, or editor extension in v1 scope.
