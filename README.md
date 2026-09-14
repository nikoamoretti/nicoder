# Local Coder

A local coding application for an Apple-silicon Mac. Open the app, choose a
Python project with tests, describe a small change in ordinary language,
watch real progress, then keep or discard the result. The original files are
not changed until you keep them.

This is **not** a general-purpose coding assistant. The engine repairs small,
testable Python behavior using one local model, at most eight generation
attempts, isolated tests, and explicit acceptance. It does not build arbitrary
apps or edit JavaScript/TypeScript front ends.

## Open the Mac app

After packaging (or if it is already installed):

1. Open **Local Coder** from `~/Applications/Local Coder.app` or from
   `dist/macos/Local Coder.app`.
2. Choose a project folder.
3. Type what you want to change.
4. Review the proposed difference and the recorded checks.
5. Keep the change, or discard it and leave the project as it was.

Rebuild the app from this checkout:

```bash
macos/scripts/package_macos.sh
```

Details: [macos/README.md](macos/README.md). Engineering evidence:
[docs/macos-app-verification.md](docs/macos-app-verification.md). The first
Finder launch may require right-click → Open (ad-hoc signed, not notarized).

## Command-line engine

The same engine is available as `local-coder` for development and tests.

Requirements: Python 3.11+, a local GGUF model, [llama.cpp](https://github.com/ggml-org/llama.cpp)
`llama-server` on `PATH`, and Docker for the isolated execution backend.

### Development (venv)

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

### Durable everyday installation (recommended)

Install the wheel into a dedicated venv and symlink the binary into your `PATH`:

```bash
# Build the wheel
python -m build

# Create a dedicated venv and install
/usr/local/bin/python3 -m venv ~/.local/share/local-coder-venv
~/.local/share/local-coder-venv/bin/pip install dist/local_coder-0.3.0-py3-none-any.whl

# Symlink into a directory on your PATH
mkdir -p ~/.local/bin
ln -sf ~/.local/share/local-coder-venv/bin/local-coder ~/.local/bin/local-coder

# Now `local-coder` works from anywhere without activating a venv
local-coder --help
```

Then the everyday workflow from any directory:

```bash
local-coder setup --write          # detect model + server, write machine config
local-coder server start           # start the local model (owned by this tool)
local-coder init --repo /path/to/your/repo
local-coder doctor --repo /path/to/your/repo
local-coder run --repo /path/to/your/repo --ticket-file ticket.md --execution isolated
local-coder inspect EPISODE_ID
local-coder diff EPISODE_ID
local-coder accept EPISODE_ID --repo /path/to/your/repo
local-coder server stop
```

The durable install uses Python 3.12 (from `/usr/local/bin/python3` on macOS) and keeps
the package isolated from your shell's Python environment.

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
- Two installed-package live smoke tasks (v0.2.0, profile `d2`, isolated backend):
  `demo_calc` solved in 4 generations, `localization` solved in 3 generations.
  Server started/stopped by the tool; port released.
- v0.3.0 Mac app API, same profile and isolated worker, disposable copies:
  a bug-fix request and a small empty-input feature request both solved in 4
  generations. Keep applied the bug fix; discard left the feature original
  unchanged. This is still two small tasks, not a general solve rate.
- 16GB hardware qualification remains **NOT RUN** until the documented
  procedure is executed on the target machine.

## Troubleshooting

- `doctor` says `model server: not running` — `local-coder server start`; if
  the endpoint is occupied by a foreign server, the tool refuses to adopt it.
- `isolated backend: BLOCKED` — build the worker image:
  `docker build -t local-coder-worker:1 -f docker/worker.Dockerfile docker/`.
- Run fails with `refusing dirty git checkout` — commit or stash the target
  repo; v1 requires a clean source tree. The repo must be clean *before*
  `init` (or at the time of `run`); `init` writes `local-coder.toml` into the
  repo, which counts as a change. Run `init` on a clean tree, commit the
  generated config, then run tickets.
- `resume` refuses a profile mismatch — resume with the profile recorded in
  `inspect` (or leave `--profile` unset for fake episodes).
- Stale `server.json` — `local-coder server status` reports it; `server start`
  replaces a stale record only when the recorded process is gone.
- `local-coder server stop` exits with code 1 when no owned server is running
  (explicit error, not a silent no-op). This is a known usability quirk; the
  error message explains the condition.

## Upgrade / uninstall

### Upgrade (durable install)

Rebuild the wheel and reinstall into the dedicated venv:

```bash
python -m build
~/.local/share/local-coder-venv/bin/pip install --upgrade dist/local_coder-0.3.0-py3-none-any.whl
```

### Uninstall (package only)

```bash
~/.local/share/local-coder-venv/bin/pip uninstall local-coder
rm -f ~/.local/bin/local-coder
```

This removes the package but **preserves** all machine state, configuration, and episode evidence.

### Optional: permanent data deletion

Only run this if you want to erase all local-coder data from your machine:

```bash
rm -rf ~/.config/local-coder        # machine config, server ownership, logs
rm -rf ~/.local/share/local-coder   # episodes, evidence, handoffs
rm -rf ~/.local/share/local-coder-venv  # the dedicated venv
```

These directories are never touched by normal operation or uninstall; they are
explicitly yours to keep or remove.

## Layout

```text
src/local_coder/   application package (controller, skills, adapters, CLI, app API)
macos/             SwiftUI Local Coder.app sources and package script
tests/             offline test suite (no live calls by default)
fixtures/          real broken Python fixtures + tickets + gold diffs
                     demo_calc, localization, multi_hunk, recovery, greet_empty
docker/            isolated worker image definition
pilot/             frozen comparison drivers + committed sanitized reports
var/               gitignored runtime state (episodes, evidence)
```

## Scope and honesty rules

- Fake-adapter runs are labeled `FAKE`; they never supply test results.
- Missing runtimes, models, sandboxes, and hardware are `BLOCKED`/`NOT RUN`, never passed.
- The model's `DONE` prose can misstate episode history (e.g. "already returning
  the correct sum" after its own patch). The controller's recorded edits,
  validation evidence, and structured handoff are authoritative; model-authored
  commentary is labeled as such in traces and handoffs.
- No training, LoRA, learned verifier, cloud fallback, weight downloads, or
  editor extension in v1 scope.