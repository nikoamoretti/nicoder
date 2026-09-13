"""Local Coder command line: init/doctor/run/inspect/resume/diff/accept/discard."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .config import CONFIG_FILENAME, load_config, write_example_config
from .controller import Controller, git_is_dirty, git_revision
from .execution.isolated import IsolatedBackend, IsolationUnavailable
from .execution.trusted_local import TrustedLocalBackend
from .inference.base import AdapterError
from .inference.fake import ScriptedFakeAdapter, demo_script
from .inference.llama_cpp import LlamaCppAdapter
from .persistence import EpisodeStore

DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "local-coder" / "episodes"


def _store(args) -> EpisodeStore:
    raw = Path(args.state_dir) if args.state_dir else DEFAULT_STATE_DIR
    return EpisodeStore(raw.expanduser().resolve())


def cmd_init(args) -> int:
    """Write an editable example config; never silently authorize scripts."""
    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"error: repo not found: {repo}", file=sys.stderr)
        return 1
    try:
        path = write_example_config(repo, overwrite=args.force)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote editable example config: {path}")
    print("Review commands and editable_paths before running. Nothing was authorized.")
    return 0


def cmd_doctor(args) -> int:
    """Report prerequisites without changing the repo or generating."""
    repo = Path(args.repo)
    ok = True
    print(f"repo: {repo}")
    if not repo.is_dir():
        print("  MISSING: repo directory not found")
        return 1
    print(f"  python: {sys.version.split()[0]}")
    try:
        import pydantic
        import pytest  # noqa: F401

        print(f"  deps: pytest + pydantic {pydantic.VERSION} present")
    except ImportError as exc:
        print(f"  MISSING: {exc}")
        ok = False
    try:
        config = load_config(repo)
        print(f"  config: {CONFIG_FILENAME} ok "
              f"(execution={config.execution}, scopes={sorted(config.commands)})")
    except ValueError as exc:
        print(f"  config INVALID: {exc}")
        ok = False
    if (repo / ".git").exists():
        print(f"  git: {git_revision(repo)[:12]} dirty={git_is_dirty(repo)}")
    else:
        print("  git: not a checkout (tree-hash mode)")
    try:
        IsolatedBackend().check_available()
        print("  isolated backend: AVAILABLE")
    except IsolationUnavailable as exc:
        print(f"  isolated backend: BLOCKED ({exc})")
    if args.probe_model:
        import os

        from .inference.llama_cpp import check_configured
        from .telemetry import llama_server_version, snapshot

        try:
            url = check_configured(os.environ.get("LOCAL_CODER_LLAMA_URL"))
            adapter = _build_llama_adapter(args, os.environ)
            record = adapter.probe(save_dir=_store(args).state_dir)
            print(f"  model probe: VALID action returned ({url})")
            print(f"  probe checks: {record['checks']}")
            print(f"  provenance: {adapter.provenance()}")
            print(f"  local llama-server binary: {llama_server_version()}")
            print(f"  memory: {snapshot(worker_image='local-coder-worker:1')}")
        except AdapterError as exc:
            print(f"  model probe: FAILED ({exc})")
            ok = False
    else:
        print("  model probe: NOT RUN (use --probe-model with LOCAL_CODER_LLAMA_URL)")
    print("doctor: " + ("OK" if ok else "BLOCKED"))
    return 0 if ok else 1


def _build_llama_adapter(args, environ) -> LlamaCppAdapter:
    """Build the live adapter from explicit loopback config (no downloads)."""
    from .inference.llama_cpp import check_configured

    url = check_configured(environ.get("LOCAL_CODER_LLAMA_URL", ""))
    from .inference.grammar import constraint_id, constraint_id_v2, constraint_id_v3

    protocol = getattr(args, "protocol", "1")
    if protocol not in ("1", "2", "3"):
        raise AdapterError(f"unknown protocol version {protocol!r}")
    constraints = {"1": constraint_id(), "2": constraint_id_v2(), "3": constraint_id_v3()}
    return LlamaCppAdapter(
        url,
        model=getattr(args, "model", None) or environ.get("LOCAL_CODER_MODEL", "unknown"),
        max_tokens=getattr(args, "max_tokens", None) or 2048,
        timeout_s=getattr(args, "gen_timeout", None) or 300,
        model_file=getattr(args, "model_file", None) or environ.get("LOCAL_CODER_MODEL_FILE"),
        quantization=getattr(args, "quantization", None)
        or environ.get("LOCAL_CODER_QUANTIZATION", "unknown"),
        n_ctx=getattr(args, "n_ctx", None),
        constraint=constraints[protocol],
        channel_policy=getattr(args, "channel", None) or "legacy",
        thinking=getattr(args, "thinking", None) or "auto",
    )


def _build_adapter(args, config):
    profile = args.profile
    if profile.startswith("fake:"):
        return ScriptedFakeAdapter(demo_script(profile), profile)
    if profile == "llama":
        import os

        return _build_llama_adapter(args, os.environ)
    raise AdapterError(f"unknown profile {profile!r} (expected fake:* or llama)")


def _build_backend(args, config):
    mode = args.execution or config.execution
    if mode == "trusted-local":
        print("execution: trusted-local — NON-SANDBOXED host execution (explicit opt-in)")
        return TrustedLocalBackend()
    backend = IsolatedBackend()
    try:
        backend.check_available()
    except IsolationUnavailable as exc:
        print(f"error: isolated execution requested but {exc}", file=sys.stderr)
        print("Refusing to fall back to host execution. "
              "Re-run with --execution trusted-local for trusted fixtures.", file=sys.stderr)
        raise
    return backend


def cmd_run(args) -> int:
    """Run one bounded episode: baseline, then at most eight generations."""
    repo = Path(args.repo).expanduser().resolve()
    ticket_file = Path(args.ticket_file).expanduser().resolve()
    if not repo.is_dir() or not ticket_file.exists():
        print("error: --repo must exist and --ticket-file must exist", file=sys.stderr)
        return 1
    try:
        config = load_config(repo)
        adapter = _build_adapter(args, config)
        backend = _build_backend(args, config)
    except (AdapterError, IsolationUnavailable, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    ticket = ticket_file.read_text(encoding="utf-8")
    store = _store(args)
    controller = Controller(store, config, adapter, backend, repo, ticket, episode_id=args.episode_id,
                            protocol_version=getattr(args, "protocol", "1") or "1",
                            verification_policy=getattr(args, "verification_policy", "off") or "off")
    try:
        state = controller.run()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    epdir = store.episode_dir(state.episode_id)
    files = ",".join(state.last_patch.files) if state.last_patch else "(none)"
    test_status = state.final.status if state.final else (state.current.status if state.current else "none")
    fake = "FAKE " if state.provenance.get("adapter") == "fake" else ""
    print(f"{fake}episode: {state.episode_id}")
    print(f"goal: {ticket.strip().splitlines()[0][:120]}")
    print(f"outcome: {state.outcome} ({state.stop_reason})")
    print(f"files: {files}  tests: {test_status}  decisions: {state.generations_used}")
    print(f"protocol: v{state.provenance.get('protocol_version', '?')}  "
          f"profile: {adapter.name}/{getattr(adapter, 'profile', '')}  "
          f"backend: {backend.name} ({backend.isolation_label})")
    print(f"tokens exact in/out: {state.prompt_tokens_exact}/{state.completion_tokens_exact}  "
          f"est in/out: {state.prompt_tokens_est}/{state.completion_tokens_est}")
    print(f"diff: {epdir}/artifacts/final.diff  handoff: {epdir}/handoff.md")
    return 0 if state.outcome == "solved" else 1


def cmd_inspect(args) -> int:
    """Show stored state, evidence, and recent events after a restart."""
    store = _store(args)
    try:
        state = store.load_state(args.episode)
        count = store.verify_events(args.episode)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "episode": state.episode_id, "outcome": state.outcome, "phase": state.phase,
        "stop_reason": state.stop_reason, "generations_used": state.generations_used,
        "patch_attempts": state.patch_attempts, "model_checks": state.model_checks,
        "test_invocations": state.test_invocations,
        "baseline": state.baseline.status if state.baseline else None,
        "current": state.current.status if state.current else None,
        "final": state.final.status if state.final else None,
        "focus": [f.path for f in state.focus],
        "events": count,
        "provenance": state.provenance,
    }, indent=2))
    for event in store.read_events(args.episode)[-8:]:
        print(f"  #{event['seq']} {event['type']} {json.dumps(event['data'], sort_keys=True)[:160]}")
    return 0


def cmd_resume(args) -> int:
    """Verify, reconcile, and continue a stored episode (no double-apply)."""
    store = _store(args)
    try:
        state = store.load_state(args.episode)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if state.outcome != "running":
        print(f"episode {args.episode} is {state.outcome} ({state.stop_reason}); nothing to continue")
        return 0 if state.outcome == "solved" else 1
    config = load_config(Path(state.repo_path))
    try:
        adapter = _build_adapter(args, config)
        backend = _build_backend(args, config)
    except (AdapterError, IsolationUnavailable, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    controller = Controller(store, config, adapter, backend, Path(state.repo_path), state.ticket,
                            protocol_version=state.provenance.get("protocol_version", "1"))
    try:
        final = controller.resume(args.episode)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"resumed {args.episode}: {final.outcome} ({final.stop_reason})")
    return 0 if final.outcome == "solved" else 1


def cmd_diff(args) -> int:
    """Print the stored final diff for an episode."""
    store = _store(args)
    ref = "artifacts/final.diff"
    try:
        print(store.read_artifact_text(args.episode, ref), end="")
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_accept(args) -> int:
    """Human-invoked acceptance: dry-run, then apply the solved candidate."""
    import os

    from .paths import candidate_manifest, original_manifest, tree_hash

    store = _store(args)
    try:
        state = store.load_state(args.episode)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    repo = Path(args.repo).expanduser().resolve()
    if state.outcome != "solved" or state.last_patch is None:
        print(f"error: episode is {state.outcome}; only solved episodes can be accepted", file=sys.stderr)
        return 1
    if (repo / ".git").exists() and git_is_dirty(repo):
        print("error: refusing accept onto a dirty checkout", file=sys.stderr)
        return 1
    current_orig = tree_hash(original_manifest(repo))
    if current_orig != state.source_tree_hash:
        print("error: original tree drifted since the episode; refusing accept", file=sys.stderr)
        return 1
    candidate = store.candidate_dir(args.episode)
    if tree_hash(candidate_manifest(candidate)) != state.candidate_tree_hash:
        print("error: candidate changed after final validation; re-verify first", file=sys.stderr)
        return 1
    applied: list[str] = []
    for rel in state.last_patch.files:
        src = candidate / rel
        dst = repo / rel
        tmp = dst.with_suffix(dst.suffix + ".accepttmp")
        tmp.write_bytes(src.read_bytes())
        os.replace(tmp, dst)
        applied.append(rel)
    store.append_event(args.episode, "accepted", {"files": applied, "repo": str(repo)})
    print(f"accepted {args.episode}: {', '.join(applied)}")
    return 0


def cmd_discard(args) -> int:
    """Delete the candidate workspace; keep audit unless --with-evidence."""
    store = _store(args)
    try:
        state = store.load_state(args.episode)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    candidate = store.candidate_dir(args.episode)
    if candidate.exists():
        shutil.rmtree(candidate)
    state.outcome = "discarded"
    state.stop_reason = "human discard; candidate removed"
    state.phase = "TERMINAL"
    store.save_state(state)
    store.append_event(args.episode, "discarded", {})
    if args.with_evidence:
        shutil.rmtree(store.episode_dir(args.episode))
        print(f"discarded {args.episode} including evidence (explicitly requested)")
    else:
        print(f"discarded {args.episode}; audit/handoff preserved")
    return 0


def cmd_benchmark(args) -> int:
    """Validate and/or run a frozen pilot suite; write sanitized reports."""
    from .benchmark import load_manifest, run_suite, validate_suite

    suite_path = Path(args.suite).expanduser().resolve()
    try:
        load_manifest(suite_path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    python = sys.executable
    if args.validate_only:
        result = validate_suite(suite_path, python)
        print(f"suite {result['suite']}: {len(result['tasks'])} tasks, "
              f"invalid={result['invalid'] or 'none'}")
        return 0 if result["valid"] else 1
    store = _store(args)
    from .persistence import new_episode_id

    run_id = args.run_id or new_episode_id()

    def adapter_factory(entry):
        return _build_adapter(args, None)

    def backend_factory():
        from .execution.isolated import IsolatedBackend
        from .execution.trusted_local import TrustedLocalBackend

        if (args.execution or "isolated") == "trusted-local":
            print("execution: trusted-local — NON-SANDBOXED host execution (explicit opt-in)")
            return TrustedLocalBackend()
        return IsolatedBackend()

    report = run_suite(
        suite_path, args.profile, adapter_factory, backend_factory, store.state_dir, run_id,
        splits=tuple(args.split.split(",")) if args.split else ("dev", "held-out"),
        max_tickets=args.max_tickets,
        report_dir=Path(args.output).expanduser().resolve() if args.output else None)
    from .benchmark import aggregate

    agg = aggregate(report)
    print(f"run {run_id}: scheduled={agg['scheduled']} attempted={agg['attempted']} "
          f"solved={agg['solved']} by_status={agg['by_status']}")
    if report.get("stopped_early"):
        print(f"stopped early: {report['stopped_early']}")
    return 0


def cmd_export_traces(args) -> int:
    """Export versioned JSONL trace records (held-out excluded by default)."""
    from .benchmark import load_manifest
    from .export import export_episode, write_jsonl
    from .persistence import EpisodeStore

    store = EpisodeStore(Path(args.state_dir).expanduser().resolve()
                         if args.state_dir else DEFAULT_STATE_DIR)
    splits: dict[str, str] = {}
    if args.suite:
        try:
            manifest = load_manifest(Path(args.suite).expanduser().resolve())
            for entry in manifest["entries"]:
                splits[entry["id"]] = entry["split"]
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.episodes:
        episode_ids = args.episodes.split(",")
    else:
        episode_ids = sorted(p.name for p in store.state_dir.iterdir() if p.is_dir())
    records = []
    for episode_id in episode_ids:
        try:
            state = store.load_state(episode_id)
        except (FileNotFoundError, ValueError):
            continue
        split = splits.get(state.task_id or "", "unsplit")
        records.extend(export_episode(store, episode_id, split=split,
                                      include_held_out=args.include_held_out))
    dest = write_jsonl(records, Path(args.output).expanduser().resolve())
    eligible = sum(1 for r in records if r.get("training_eligible"))
    print(f"exported {len(records)} turn records ({eligible} training-eligible) to {dest}")
    return 0


def cmd_future(args) -> int:
    """Benchmark and trace export arrive in later milestones (honest stub)."""
    print(f"error: {args.command} is not implemented in Milestone 1 (see Prompt 4 scope)",
          file=sys.stderr)
    return 3


def _add_llama_args(parser) -> None:
    """Live-profile options. All explicit; nothing is downloaded or guessed."""
    parser.add_argument("--model", default=None, help="model alias for the local server")
    parser.add_argument("--model-file", default=None, help="local model file path (hashed for provenance)")
    parser.add_argument("--quantization", default=None, help="e.g. Q4_K_M (recorded, never inferred)")
    parser.add_argument("--n-ctx", default=None, type=int, help="verified server context size")
    parser.add_argument("--max-tokens", default=None, type=int, help="per-generation output budget")
    parser.add_argument("--gen-timeout", default=None, type=int, help="per-generation deadline in seconds")
    parser.add_argument("--protocol", default="1", choices=["1", "2", "3"],
                        help="action protocol version (v2 = exact-replacement PATCH, "
                             "v3 = span-ID selection PATCH)")
    parser.add_argument("--verification-policy", default="off", choices=["off", "mandatory"],
                        help="mandatory runs a controller-owned check after every edit; "
                             "resume always adopts the episode's recorded policy")
    parser.add_argument("--thinking", default="auto", choices=["auto", "off"],
                        help="declared thinking expectation (verified by probe, never assumed)")
    parser.add_argument("--channel", default="legacy", choices=["legacy", "final-only"],
                        help="output channel policy (final-only rejects reasoning-only output)")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(prog="local-coder", description="Bounded autonomous patcher (M1 offline core)")
    parser.add_argument("--state-dir", default=None, help="episode store (default: per-user app state)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write editable example config")
    p.add_argument("--repo", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="report prerequisites, change nothing")
    p.add_argument("--repo", required=True)
    p.add_argument("--probe-model", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="run one bounded episode")
    p.add_argument("--repo", required=True)
    p.add_argument("--ticket-file", required=True)
    p.add_argument("--profile", required=True, help="fake:success|fake:wrong|fake:blocked|llama")
    p.add_argument("--execution", default=None, help="isolated|trusted-local (default: repo config)")
    p.add_argument("--episode-id", default=None)
    _add_llama_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("inspect", help="show stored episode evidence")
    p.add_argument("episode")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("resume", help="verify, reconcile, continue an episode")
    p.add_argument("episode")
    p.add_argument("--profile", required=True)
    p.add_argument("--execution", default=None)
    _add_llama_args(p)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("diff", help="print stored final diff")
    p.add_argument("episode")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("accept", help="apply a solved candidate to the original tree")
    p.add_argument("episode")
    p.add_argument("--repo", required=True)
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("discard", help="remove candidate, keep audit")
    p.add_argument("episode")
    p.add_argument("--with-evidence", action="store_true")
    p.set_defaults(func=cmd_discard)

    for name, func, help_text in (
        ("benchmark", cmd_benchmark, "validate/run a frozen suite"),
        ("export-traces", cmd_export_traces, "export versioned trace records"),
    ):
        p = sub.add_parser(name, help=help_text)
        if name == "export-traces":
            p.add_argument("--output", required=True)
            p.add_argument("--episodes", default=None, help="comma-separated episode ids (default: all)")
            p.add_argument("--suite", default=None, help="suite manifest for split labels")
            p.add_argument("--include-held-out", action="store_true")
        if name == "benchmark":
            p.add_argument("--suite", required=True)
            p.add_argument("--profile", required=True)
            p.add_argument("--execution", default=None)
            p.add_argument("--run-id", default=None)
            p.add_argument("--split", default="dev,held-out")
            p.add_argument("--max-tickets", default=20, type=int)
            p.add_argument("--output", default=None, help="sanitized report directory")
            p.add_argument("--validate-only", action="store_true")
            _add_llama_args(p)
        p.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
