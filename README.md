# Local Coder

A bounded autonomous patcher for small, testable Python repository tickets,
designed for an Apple-silicon Mac with 16GB unified memory. Experimental v1:
one small local model, one ticket, at most eight generation attempts, explicit
human acceptance. The original source is never modified until you accept.

Workflow: `ticket → compact briefing → one model-selected action →
deterministic skill execution → structured observation → runtime-owned
state update → another decision or termination`. The local model sees exactly
four actions — **FIND, PATCH, CHECK, DONE** — and software, not the model,
owns budgets, paths, test results, patch identity, progress, and completion.

## Quickstart

Requirements: Python 3.11+, a local GGUF model, [llama.cpp](https://github.com/ggml-org/llama.cpp)
`llama-server` on `PATH`, and Docker for the isolated execution backend.

```bash
python3 -m venv .venv
.venv/bin/pip install .                      # installs the `local-coder` command
.venv/bin/local-coder setup --write          # detect model + server, write machine config
.venv/bin/local-coder server start           # start the local model (owned by this tool)
.venv/bin/local-coder init --repo /path/to/your/repo
.venv/bin/local-coder doctor --repo /path/to/your/repo
.venv/bin/local-coder run --repo /path/to/your/repo --ticket-file ticket.md --execution isolated
.venv/bin/local-coder inspect EPISODE_ID
.venv/bin/local-coder diff EPISODE_ID
.venv/bin/local-coder accept EPISODE_ID --repo /path/to/your/repo
.venv/bin/local-coder server stop
```

`run` prints the episode id, stage, applied/reverted/rejected edits, check
status, outcome, and the exact next commands. Omit `--execution` to use the
repo config default (`isolated`). `--execution trusted-local` is an explicit
opt-in to NON-SANDBOXED host execution for trusted fixtures; when isolation is
unavailable the CLI refuses instead of silently falling back.

The default `init` path is `fake:success`-free: `init` writes
`local-coder.toml` with editable paths, exact-argv commands, and the acceptance
contract. Commands are argv lists; tickets can never authorize shell commands.

## Profiles

Profiles are frozen, named identities. They are selected explicitly and never
switched automatically after a failed task; `resume` refuses a mismatched
profile.

| Profile | Protocol | Editing | Constraint | Verification | Channel | Thinking |
|---------|----------|---------|------------|--------------|---------|----------|
| `d2` (default) | v3 | span edit, `span-catalogue-v2` | `gbnf-action-v3b` | mandatory post-edit | final-only | off |
| `d1` (fallback) | v2 | exact-text replacement | `gbnf-action-v2b` | mandatory post-edit | final-only | off |

Every run records the full profile identity (name, constraint, catalogue,
transport policy, protocol, verification policy, channel, thinking) in episode
provenance. `--profile fake:<name>` uses the scripted adapter for tests and is
labeled `FAKE` everywhere.

Advanced per-run overrides exist (`--protocol`, `--verification-policy`,
`--channel`, `--thinking`) but they change the recorded identity; prefer the
named profiles.

## Machine configuration

Machine-specific paths never live in the repo. Defaults are stored at
`~/.config/local-coder/config.toml` (override the directory with
`LOCAL_CODER_CONFIG_DIR`; env overrides: `LOCAL_CODER_LLAMA_URL` for the
endpoint, `LOCAL_CODER_WORKER_IMAGE` for the container image):

```toml
model_file = "/Users/you/.cache/local-coder/models/Qwen_Qwen3.5-4B-Q4_K_M.gguf"
model_alias = "Qwen3.5-4B-Q4_K_M"
endpoint = "http://127.0.0.1:8080"
server_binary = "llama-server"
quantization = "Q4_K_M"
n_ctx = 8192
max_tokens = 2048
gen_timeout = 300
```

`local-coder setup` prints the effective values; `setup --write` persists them.
No command downloads weights, installs runtimes, or upgrades llama.cpp.

## Server lifecycle

`local-coder server start|status|stop` manages only the server this tool
started. Ownership is recorded in `server.json` in the machine config
directory. There is never a port takeover and never a kill of an unrelated
process: an occupied endpoint, a foreign server, a missing model, or a missing
binary are explicit refusals. `stop` signals only a live, matching, owned pid.
Episode evidence lives outside the repo (default
`~/.local/share/local-coder/episodes`); override with `--state-dir`.

## Verify

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check .
PYTHONPATH=src .venv/bin/python -m mypy src/local_coder
```

The default suite is offline. Live tests are opt-in via `LOCAL_CODER_LLAMA_URL`
and a running model; `doctor --probe-model` performs one explicit, recorded
generative capability probe (never run implicitly).

## Measured evidence

- Fleets are small: compare-03 (frozen dev fixtures, D2): solved 3/4 curated
  dev fixtures; D1 fallback 2/4; capability probes 2/2. This is an
  experimental check on four small tasks, not a general solve rate.
- One preserved live episode (`live-demo-calc-01`, gitignored working-tree
  evidence): demo_calc solved in 5 generations / 65s on Apple M2 Pro / 16GB
  with `Qwen3.5-4B-Q4_K_M` (llama.cpp 0.4.0 build 10809, 8192 ctx, Metal
  offload); server RSS ~1.18GB, controller ~61MB.
- 16GB hardware qualification remains **NOT RUN** until the documented
  procedure is executed on the target machine.

## Troubleshooting

- `doctor` says `model server: not running` — `local-coder server start`; if
  the endpoint is occupied by a foreign server, the tool refuses to adopt it.
- `isolated backend: BLOCKED` — build the worker image:
  `docker build -t local-coder-worker:1 -f docker/worker.Dockerfile docker/`.
- Run fails with `refusing dirty git checkout` — commit or stash the target
  repo; v1 requires a clean source tree.
- `resume` refuses a profile mismatch — resume with the profile recorded in
  `inspect` (or leave `--profile` unset for fake episodes).
- Stale `server.json` — `local-coder server status` reports it; `server start`
  replaces a stale record only when the recorded process is gone.

## Upgrade / uninstall

```bash
.venv/bin/pip install --upgrade ./dist/local_coder-0.2.0-py3-none-any.whl
.venv/bin/pip uninstall local-coder
rm -rf ~/.config/local-coder ~/.local/share/local-coder   # machine state, evidence
```

## Layout

```text
src/local_coder/   application package (controller, skills, adapters, CLI)
tests/             offline test suite (no live calls by default)
fixtures/          real broken Python fixtures + tickets + gold diffs
                     demo_calc, localization, multi_hunk, recovery
docker/            isolated worker image definition
pilot/             frozen comparison drivers + committed sanitized reports
var/               gitignored runtime state (episodes, evidence)
```

## Scope and honesty rules

- Fake-adapter runs are labeled `FAKE`; they never supply test results.
- Missing runtimes, models, sandboxes, and hardware are `BLOCKED`/`NOT RUN`, never passed.
- No training, LoRA, learned verifier, cloud fallback, weight downloads, or
  editor extension in v1 scope.