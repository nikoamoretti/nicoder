# local-coder v0.2.0 — release verification record

Date: 2026-09-13/14 (UTC) · Commit under verification: this commit (see `git log`)
Scope: Prompt 5 — usable v1 CLI, installed package, local-model smoke.

## Status

| Status | Result |
|---|---|
| Software-complete | **YES** — full offline suite, lint, typecheck, wheel + sdist, installed CLI verified from an unrelated directory |
| Local-inference-verified | **YES** — installed wheel, profile `d2`, isolated backend, 2 curated dev fixtures solved 2/2 on real llama.cpp, 2 recorded probes pass |
| 16GB-hardware-qualified | **NOT RUN** — smoke measurements on 16GB Apple silicon are recorded below, but the formal qualification procedure (multi-day/long-run memory-pressure study) was not completed; no general claim is made |

## Environment

- Hardware: Apple M2 Pro, 16GB unified memory (`hw.memsize=17179869184`), macOS 26.6.2.
- Model: `Qwen_Qwen3.5-4B-Q4_K_M.gguf` (2,873.4 MB, `~/.cache/local-coder/models/`).
- Server: llama.cpp `0.4.0` build 10809 (`/opt/homebrew/bin/llama-server`), `-c 8192`, Metal offload.
- Container runtime: Docker 29.7.2, image `local-coder-worker:1` (149 MB, 2 GB cap).
- Package under test: `dist/local_coder-0.2.0-py3-none-any.whl`, installed into a fresh Python 3.13.11 venv; all runs executed from `/tmp` with no `PYTHONPATH`.

## Artifacts and checksums (SHA-256)

```
9b35c6436cf02653adbece1fd92b991aec70a0f721ee9c4af208ed83443cad40  local_coder-0.2.0-py3-none-any.whl  (105,300 bytes)
b31005b6809bdf61239090cc332421da7dcc62485c9fc2c8f59f8e760e9ebb68  local_coder-0.2.0.tar.gz           (192,502 bytes)
```

`dist/` is gitignored; rebuild with `.venv/bin/python -m build`.

## Software verification

```bash
.venv/bin/python -m pytest tests/ -q     # 264 passed in 189.74s
.venv/bin/python -m ruff check .         # All checks passed
PYTHONPATH=src .venv/bin/python -m mypy src/local_coder   # no issues, 39 files
```

New coverage this milestone: profiles frozen/refusals, machine-config
round-trip and malformed config, server ownership (start/status/stop, stale
pid, foreign pid refusal, unowned-endpoint refusal), transactional accept
(dry-run, rollback on replace failure), resume profile-mismatch refusal,
legacy-episode refusal, probe profile/protocol recording, setup write/no-write.

Installed-package evidence (fresh venv, `cwd=/tmp`):
`local-coder --help`; `run --profile fake:success --execution trusted-local`;
`inspect`; `diff`; `accept` — original changed only after explicit accept.

## Local-model smoke (installed wheel, profile d2, isolated backend)

Disposable copies of the curated dev fixtures (same fixtures as compare-03);
server started and stopped by `local-coder server start|stop` (pid recorded,
port released). No foreign server was touched.

| Session | Fixture | Outcome | Gens used | Edits | Final validation | Tokens in/out |
|---|---|---|---|---|---|---|
| `live-demo-d2` | demo_calc | **solved** | 4 (of 8) | 1 applied, 0 reverted | pass | 4,992 / 154 |
| `live-loc-d2` | localization | **solved** | 3 (of 8) | 1 applied, 0 reverted | pass | 6,402 / 153 |

- Durations: demo_calc ~40 s, localization ~30 s per episode (llama.cpp
  already resident; `server start` to healthy ~6.5 s warm).
- Both diffs are minimal one-line fixes; originals were byte-identical before
  accept (accept gate verified tree hash, candidate hash, and clean tree).
- Probes (post-fix records): `d2` → protocol 3 / `gbnf-action-v3b` /
  thinking off / channel content / all checks ok; `d1` → protocol 2 /
  `gbnf-action-v2b` / all checks ok. Two earlier probe records taken before
  the probe-metadata fix (they recorded the default protocol label) are
  retained under `_probes-prefix-fix/` and are not used as smoke evidence.

Memory (episode telemetry at `episode_created`, plus sampling):
server RSS ~593 MB idle, peak ~905 MB during generation, ~605 MB after;
controller RSS ~52 MB; worker image 149 MB (2 GB enforced cap, `--network none`,
2 CPUs, 256 pids). System-wide free memory: 30% before the model loaded,
49% after the server was stopped. Pageouts rose by 11,598 pages across the
session (model load through shutdown); no sustained-pressure or swap-exhaustion
study was performed.

Raw evidence (gitignored, local): `var/episodes-prompt5/`
- `live-demo-d2/state.json` sha256 `dd8f32d95b8936ba9d59dacd785ff7186fb3daeae8a1faea278fda4be7662db5`
- `live-demo-d2/events.jsonl` sha256 `ad8f00996d50e7ac7d6100ff7f437e8513e975e8fdb8f429bae8a3d5a3b89965`
- `live-loc-d2/state.json` sha256 `8d5577279eb53c53437a7cd7762f8918f63df959115e73e91a550e313883e7df`
- `live-loc-d2/events.jsonl` sha256 `88283b105b569d2ab6f5ea1133d2ff0eb12f64a311fe66a4727d2c7fc6b08e8a`
- `_probes/probe-20260914-035627.json` sha256 `3487d320dd51f6eb9baa1d303daaa131fd10c9ee77171a58a316cce421375a96`
- `_probes/probe-20260914-035630.json` sha256 `568b7db3c34fbd442cb0c38a1674b13091d79d53f586b50517255e4875b55cf8`
- `mem-demo.log`, `mem-loc.log`, `pressure-*.txt`, `llama-server.log`

## CI

`.github/workflows/ci.yml` adds three jobs: offline (ruff + mypy + pytest),
container (builds the worker image, runs the suite with the isolated backend
available), and package (builds wheel + sdist, installs the wheel in a fresh
venv, runs a fake episode from an unrelated directory, uploads `dist/`).
First GitHub run is pending the push; treat CI as **added, not yet observed**.

## Remaining defects and limits

1. Hardware qualification is a 2-episode dev-fixture smoke; it is not a
   general solve rate and does not establish sustained 16GB headroom.
2. Model `DONE` summaries can misstate history (e.g. "already returning the
   correct sum" after its own patch); the controller's recorded validation,
   not the prose, is authoritative.
3. `server stop` exits 1 when no owned server exists (explicit error, not a
   silent no-op).
4. The localization fixture was solved here and in compare-03 under `d2`
   only; `d1` fallback remains weaker on multi-hunk tasks.
5. `local-coder` was verified on Python 3.13.11 from the wheel and 3.12 in
   the frozen venv; other interpreters are untested locally (CI covers 3.12).

---

## Closeout addendum (2026-09-14)

### CI status

Remote CI (GitHub Actions) is **blocked by account billing**, not by code or
workflow defects.

- Run 34804733892 (commit fa34248): all three jobs failed to start with
  "The job was not started because recent account payments have failed or your
  spending limit needs to be increased."
- Run 34812164277 (commit 8307353, README updates): same billing block.
- Local verification (offline suite, lint, typecheck, package build, installed
  CLI smoke) all pass on the current commit.

**Required user action**: resolve the GitHub account payment issue in Settings →
Billing & plans. No code changes are needed; the workflow configuration is
correct and the test suite passes locally.

### Updated artifacts (commit 8307353)

Rebuilt after README documentation updates:

```
6762d39bc5227a1a0252bc33e89bd80a42835f3f6e255a74dade24e57fc1edd6  local_coder-0.2.0-py3-none-any.whl  (105,388 bytes)
28e289786d8570ef77ed2c0254a3f82cf36de4915dcd405e2feedfd839891843  local_coder-0.2.0.tar.gz           (192,590 bytes)
```

Original v0.2.0 artifacts (commit fa34248) preserved in the verification record
above.

### Durable installed executable

Verified installation path: `~/.local/bin/local-coder`
(symlink to `~/.local/share/local-coder-venv/bin/local-coder`,
venv created with `/usr/local/bin/python3` 3.12.3)

### Verified everyday commands (from outside source checkout)

```bash
# One-time setup
local-coder setup --write          # detects model, writes ~/.config/local-coder/config.toml
local-coder server start           # starts owned llama-server on 127.0.0.1:8080

# Per-repo setup (run on a CLEAN git tree)
local-coder init --repo /path/to/your/repo
# → commit the generated local-coder.toml
local-coder doctor --repo /path/to/your/repo

# Run a ticket (profile d2, isolated backend)
local-coder run --repo /path/to/your/repo --ticket-file ticket.md --execution isolated
# → prints episode ID, outcome, next commands

# Inspect and accept
local-coder inspect EPISODE_ID
local-coder diff EPISODE_ID
local-coder accept EPISODE_ID --repo /path/to/your/repo

# Stop the owned server
local-coder server stop            # exits 1 if no owned server (known quirk)
```

### Evidence preservation

- v0.2.0 verification record and raw episodes (`var/episodes-prompt5/`) unchanged.
- README updates are documentation-only; no functional code changes.
- Package version remains 0.2.0 (patch increment not warranted for docs-only).
- No force-push, history rewrite, or visibility change.
