"""Nicoder command line.

Installed entry point: ``local-coder``. Commands:
init, setup, doctor, server start|status|stop, run, inspect, resume, diff,
accept, discard, benchmark, export-traces, app-rpc.

The default run path uses profile ``d2`` (protocol v3 span editing,
mandatory post-edit verification, verified non-thinking server mode).
Profile ``d1`` is the explicitly selected exact-text fallback. Profiles are
never switched automatically, and a resume refuses a mismatched profile.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .accept import accept_episode, discard_episode
from .config import CONFIG_FILENAME, load_config, write_example_config
from .controller import Controller, git_is_dirty, git_revision
from .execution.isolated import IsolatedBackend, IsolationUnavailable
from .execution.trusted_local import TrustedLocalBackend
from .inference.base import AdapterError
from .machine import (
    config_path,
    load_machine_config,
    machine_status,
    write_machine_config,
)
from .persistence import EpisodeStore
from .profiles import DEFAULT_PROFILE, Profile, profile_identity
from .runtime import (
    DEFAULT_STATE_DIR,
    build_adapter,
    build_backend,
    build_llama_adapter,
    effective_channel,
    effective_protocol,
    effective_thinking,
    effective_verification,
    selected_profile,
)
from .server import ServerError, server_start, server_status, server_stop


def _store(args) -> EpisodeStore:
    raw = Path(args.state_dir) if getattr(args, "state_dir", None) else DEFAULT_STATE_DIR
    return EpisodeStore(raw.expanduser().resolve())


# -- profile and adapter construction ---------------------------------------


def _selected_profile(args) -> Profile | None:
    """Resolve the named profile for this run; fake profiles bypass names."""
    name = getattr(args, "profile", None)
    if name == "llama":
        print("note: profile 'llama' is a legacy alias; using default profile 'd2'")
        name = DEFAULT_PROFILE
    return selected_profile(name)


def _effective_protocol(args, profile: Profile | None) -> str:
    return effective_protocol(profile, getattr(args, "protocol", None))


def _effective_verification(args, profile: Profile | None) -> str:
    return effective_verification(profile, getattr(args, "verification_policy", None))


def _effective_channel(args, profile: Profile | None) -> str:
    return effective_channel(profile, getattr(args, "channel", None))


def _effective_thinking(args, profile: Profile | None) -> str:
    return effective_thinking(profile, getattr(args, "thinking", None))


def _build_llama_adapter(args, profile: Profile | None):
    """Build the live adapter from machine config, profile, and overrides."""
    return build_llama_adapter(
        profile,
        endpoint=getattr(args, "endpoint", None),
        model=getattr(args, "model", None),
        model_file=getattr(args, "model_file", None),
        quantization=getattr(args, "quantization", None),
        n_ctx=getattr(args, "n_ctx", None),
        max_tokens=getattr(args, "max_tokens", None),
        gen_timeout=getattr(args, "gen_timeout", None),
        protocol=getattr(args, "protocol", None),
        channel=getattr(args, "channel", None),
        thinking=getattr(args, "thinking", None),
    )


def _build_adapter(args, config):
    return build_adapter(
        getattr(args, "profile", None),
        endpoint=getattr(args, "endpoint", None),
        model=getattr(args, "model", None),
        model_file=getattr(args, "model_file", None),
        quantization=getattr(args, "quantization", None),
        n_ctx=getattr(args, "n_ctx", None),
        max_tokens=getattr(args, "max_tokens", None),
        gen_timeout=getattr(args, "gen_timeout", None),
        protocol=getattr(args, "protocol", None),
        channel=getattr(args, "channel", None),
        thinking=getattr(args, "thinking", None),
    )


def _build_backend(args, config):
    mode = getattr(args, "execution", None) or config.execution
    if mode == "trusted-local":
        print("execution: trusted-local — NON-SANDBOXED host execution (explicit opt-in)")
    try:
        return build_backend(config, getattr(args, "execution", None))
    except IsolationUnavailable as exc:
        print(f"error: isolated execution requested but {exc}", file=sys.stderr)
        print("Refusing to fall back to host execution. "
              "Re-run with --execution trusted-local for trusted fixtures.", file=sys.stderr)
        raise


# -- init / setup / doctor ---------------------------------------------------


def cmd_init(args) -> int:
    """Write an editable example config; never silently authorize scripts."""
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"error: repo not found: {repo}", file=sys.stderr)
        return 1
    try:
        path = write_example_config(repo, overwrite=args.force)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    state_dir = _store(args).state_dir
    print(f"wrote editable example config: {path}")
    print("Next steps:")
    print(f"  1. Review {CONFIG_FILENAME}: editable_paths, commands, acceptance.")
    print("     Commands are exact argv lists; tickets cannot authorize shell commands.")
    print("  2. Configure the machine once: local-coder setup --write")
    print("  3. Start the model:            local-coder server start")
    print(f"  4. Check readiness:            local-coder doctor --repo {repo}")
    print(f"Episode evidence will be stored outside the repo at: {state_dir}")
    return 0


def cmd_setup(args) -> int:
    """Detect and optionally persist machine-local configuration."""
    import os

    cfg = load_machine_config()
    updates = {
        "model_file": getattr(args, "model_file", None),
        "model_alias": getattr(args, "model_alias", None),
        "endpoint": getattr(args, "endpoint", None),
        "server_binary": getattr(args, "server_binary", None),
        "n_ctx": getattr(args, "n_ctx", None),
    }
    cfg = cfg.model_copy(update={k: v for k, v in updates.items() if v})
    if os.environ.get("LOCAL_CODER_LLAMA_URL"):
        cfg = cfg.model_copy(update={"endpoint": os.environ["LOCAL_CODER_LLAMA_URL"]})
    if args.write:
        path = write_machine_config(cfg)
        print(f"wrote machine configuration: {path}")
    else:
        print(f"machine configuration would be written to: {config_path()}")
        print("re-run with --write to persist it")
    print("Effective configuration:")
    for key, value in cfg.model_dump().items():
        print(f"  {key} = {value}")
    return 0


def cmd_doctor(args) -> int:
    """Report prerequisites without changing the repo or generating tokens."""
    repo = Path(args.repo).expanduser().resolve()
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
    cfg = load_machine_config()
    for name, row_ok, detail in machine_status(cfg):
        print(f"  {name}: {'ok' if row_ok else 'MISSING'} — {detail}")
        if not row_ok and name != "endpoint":
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
    status = server_status(cfg)
    if status["healthy"]:
        print(f"  model server: healthy at {cfg.endpoint} "
              f"(alias={status['model_alias'] or 'unknown'}, owned={status['owned']})")
    elif status["running"]:
        print(f"  model server: process present but not healthy at {cfg.endpoint} "
              f"(owned={status['owned']})")
    else:
        print(f"  model server: not running at {cfg.endpoint} "
              f"(start it with: local-coder server start)")
    if args.probe_model:
        try:
            profile = _selected_profile(args)
            adapter = _build_llama_adapter(args, profile)
            record = adapter.probe(save_dir=_store(args).state_dir,
                                   protocol_version=_effective_protocol(args, profile),
                                   profile=profile.name if profile else None)
            print(f"  model probe: VALID action returned ({adapter.endpoint})")
            print(f"  probe checks: {record['checks']}")
            print(f"  probe record saved under: {_store(args).state_dir}/_probes")
        except (AdapterError, ValueError) as exc:
            print(f"  model probe: FAILED ({exc})")
            ok = False
    else:
        print("  model probe: NOT RUN (explicit: --probe-model; generates one recorded probe)")
    print("doctor: " + ("OK" if ok else "BLOCKED"))
    return 0 if ok else 1


# -- server lifecycle ---------------------------------------------------------


def cmd_server(args) -> int:
    """start | status | stop the local model server owned by this tool."""
    cfg = load_machine_config()
    try:
        if args.server_command == "start":
            result = server_start(cfg, wait_s=args.wait)
            print(f"server started (pid {result['pid']}) at {result['endpoint']}")
            print(f"  model alias: {result['model_alias'] or 'unknown'}")
            print(f"  log: {result['log']}")
            print("  stop it with: local-coder server stop")
        elif args.server_command == "status":
            status = server_status(cfg)
            owned = "yes" if status["owned"] else "no"
            print(f"server: running={status['running']} healthy={status['healthy']} owned={owned}")
            if status["pid"]:
                print(f"  pid: {status['pid']}")
            if status["model_alias"]:
                print(f"  model alias: {status['model_alias']}")
            if status["note"]:
                print(f"  note: {status['note']}")
        else:
            result = server_stop()
            if result["stopped"]:
                print(f"server stopped (pid {result['pid']})")
            else:
                print(f"server not running: {result['detail']}")
        return 0
    except ServerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# -- run ----------------------------------------------------------------------


def cmd_run(args) -> int:
    """Run one bounded episode: baseline, then at most eight generations."""
    repo = Path(args.repo).expanduser().resolve()
    ticket_file = Path(args.ticket_file).expanduser().resolve()
    if not repo.is_dir() or not ticket_file.exists():
        print("error: --repo must exist and --ticket-file must exist", file=sys.stderr)
        return 1
    try:
        config = load_config(repo)
        profile = _selected_profile(args)
        adapter = _build_adapter(args, config)
        backend = _build_backend(args, config)
    except (AdapterError, IsolationUnavailable, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    ticket = ticket_file.read_text(encoding="utf-8")
    store = _store(args)
    controller = Controller(
        store, config, adapter, backend, repo, ticket, episode_id=args.episode_id,
        protocol_version=_effective_protocol(args, profile),
        verification_policy=_effective_verification(args, profile),
        profile_identity=profile_identity(profile) if profile else None)
    print(f"episode: {controller.episode_id}  (store: {store.episode_dir(controller.episode_id)})")
    print(f"profile: {profile.name if profile else getattr(args, 'profile', '?')}"
          f" ({profile.label if profile else 'test/legacy'})  "
          f"backend: {backend.name} — {backend.isolation_label}")
    try:
        state = controller.run()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_run_summary(store, state, adapter, backend, profile)
    return 0 if state.outcome == "solved" else 1


def _print_run_summary(store: EpisodeStore, state, adapter, backend, profile: Profile | None) -> None:
    """Honest run summary: stages, budgets, edits, checks, next command."""
    events = store.read_events(state.episode_id)
    applied = [e for e in events if e["type"] == "patch_applied"]
    reverted = [e for e in events if e["type"] == "patch_reverted"]
    rejected = [e["data"].get("code", "?") for e in events
                if e["type"] in ("patch_rejected", "protocol_error")]
    auto_checks = [e for e in events if e["type"] == "auto_check_completed"]
    model_checks = [e for e in events if e["type"] == "check_completed"]
    fake = "FAKE " if state.provenance.get("adapter") == "fake" else ""
    remaining_gens = state.budgets.max_generations - state.generations_used
    files = ",".join(state.last_patch.files) if state.last_patch else "(none)"
    print(f"{fake}episode: {state.episode_id}")
    print(f"goal: {state.ticket.strip().splitlines()[0][:120]}")
    print(f"profile: {profile.name if profile else 'fake/legacy'}"
          f"  protocol: v{state.provenance.get('protocol_version', '?')}"
          f"  verification: {state.provenance.get('verification_policy', '?')}")
    print(f"stage: {state.phase}  decisions left: {remaining_gens}  files: {files}")
    print(f"edits: applied={len(applied)} reverted={len(reverted)} rejected={rejected or 'none'}")
    for check in auto_checks:
        print(f"automatic check ({check['data'].get('scope')}): {check['data'].get('status')} "
              f"failed={check['data'].get('failed') or []}")
    for check in model_checks[-2:]:
        print(f"model check ({check['data'].get('scope')}): {check['data'].get('status')} "
              f"failed={check['data'].get('failed') or []}")
    if state.final is not None:
        print(f"final validation: {state.final.status}")
    elif state.current is not None:
        print(f"latest check: {state.current.scope} {state.current.status} "
              f"(focused evidence is not final validation)")
    print(f"outcome: {state.outcome} ({state.stop_reason})")
    print(f"inspect: local-coder inspect {state.episode_id}")
    print(f"diff:    local-coder diff {state.episode_id}")
    if state.outcome == "solved":
        print(f"accept:  local-coder accept {state.episode_id} --repo {state.repo_path}")
    print(f"handoff: {store.episode_dir(state.episode_id)}/handoff.md")
    print(f"tokens exact in/out: {state.prompt_tokens_exact}/{state.completion_tokens_exact}")


# -- inspect / resume / diff / accept / discard -------------------------------


def cmd_inspect(args) -> int:
    """Show stored episode evidence after a restart."""
    store = _store(args)
    try:
        state = store.load_state(args.episode)
        count = store.verify_events(args.episode)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    epdir = store.episode_dir(args.episode)
    print(json.dumps({
        "episode": state.episode_id, "outcome": state.outcome, "phase": state.phase,
        "stop_reason": state.stop_reason, "generations_used": state.generations_used,
        "patch_attempts": state.patch_attempts, "model_checks": state.model_checks,
        "auto_checks": state.auto_checks, "test_invocations": state.test_invocations,
        "baseline": state.baseline.status if state.baseline else None,
        "current": state.current.status if state.current else None,
        "final": state.final.status if state.final else None,
        "focus": [f.path for f in state.focus],
        "events": count,
        "provenance": state.provenance,
        "candidate_dir": str(store.candidate_dir(args.episode)),
        "handoff": str(epdir / "handoff.md"),
        "final_diff": str(epdir / "artifacts" / "final.diff"),
        "next": f"local-coder diff {state.episode_id}",
    }, indent=2))
    for event in store.read_events(args.episode)[-8:]:
        print(f"  #{event['seq']} {event['type']} {json.dumps(event['data'], sort_keys=True)[:160]}")
    return 0


def _check_profile_match(state, args) -> tuple[bool, str]:
    """Resume must not silently run under a different profile."""
    profile = _selected_profile(args)
    recorded = state.provenance.get("profile_name")
    if profile is None:
        return True, ""
    if recorded is None:
        return False, (f"episode {state.episode_id} predates named profiles; "
                       f"resume refused under profile '{profile.name}'")
    if recorded != profile.name:
        return False, (f"episode {state.episode_id} ran under profile '{recorded}'; "
                       f"refusing to resume under '{profile.name}'")
    return True, ""


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
    try:
        matched, detail = _check_profile_match(state, args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not matched:
        print(f"error: {detail}", file=sys.stderr)
        return 1
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
    print(f"inspect: local-coder inspect {args.episode}")
    return 0 if final.outcome == "solved" else 1


def cmd_diff(args) -> int:
    """Print the stored final diff for an episode (solved or blocked)."""
    store = _store(args)
    ref = "artifacts/final.diff"
    try:
        text = store.read_artifact_text(args.episode, ref)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(text, end="")
    return 0


def cmd_accept(args) -> int:
    """Transactional acceptance of a solved candidate onto the original tree."""
    store = _store(args)
    try:
        result = accept_episode(
            store, args.episode, Path(args.repo), dry_run=getattr(args, "dry_run", False))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not result["ok"]:
        print(f"error: {result['detail']}", file=sys.stderr)
        return 1
    if result.get("dry_run"):
        repo = Path(args.repo).expanduser().resolve()
        print(f"dry run for {args.episode}: would apply {len(result['files'])} file(s) to {repo}")
        print(json.dumps(result["plan"], indent=2, sort_keys=True))
        return 0
    print(f"accepted {args.episode}: {', '.join(result['applied'])}")
    return 0


def cmd_discard(args) -> int:
    """Remove the candidate workspace; preserve evidence unless asked."""
    store = _store(args)
    try:
        result = discard_episode(
            store, args.episode, with_evidence=getattr(args, "with_evidence", False))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"discarded {args.episode}: candidate "
          f"{'removed' if result['candidate_removed'] else 'already absent'}")
    print(f"original repository untouched: {result['repo']}")
    if result["evidence_deleted"]:
        print("evidence explicitly deleted (--with-evidence)")
    else:
        print("evidence preserved: state, events, handoff, artifacts (default)")
    return 0


# -- benchmark / export -------------------------------------------------------


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

    store = _store(args)
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


# -- parser -------------------------------------------------------------------


def _add_llama_args(parser) -> None:
    """Live-profile options. Explicit overrides; nothing is downloaded."""
    parser.add_argument("--model", default=None, help="model alias for the local server")
    parser.add_argument("--model-file", default=None, help="local model file path (hashed for provenance)")
    parser.add_argument("--quantization", default=None, help="e.g. Q4_K_M (recorded, never inferred)")
    parser.add_argument("--n-ctx", default=None, type=int, help="verified server context size")
    parser.add_argument("--max-tokens", default=None, type=int, help="per-generation output budget")
    parser.add_argument("--gen-timeout", default=None, type=int, help="per-generation deadline in seconds")
    parser.add_argument("--endpoint", default=None, help="loopback llama.cpp endpoint override")
    parser.add_argument("--protocol", default=None, choices=["1", "2", "3"],
                        help="override the profile protocol (advanced)")
    parser.add_argument("--verification-policy", default=None, choices=["off", "mandatory"],
                        help="override the profile verification policy (advanced)")
    parser.add_argument("--thinking", default=None, choices=["auto", "off"],
                        help="override the profile thinking expectation (advanced)")
    parser.add_argument("--channel", default=None, choices=["legacy", "final-only"],
                        help="override the profile channel policy (advanced)")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        prog="local-coder",
        description="Bounded autonomous patcher for small, testable Python repository tickets (experimental).")
    parser.add_argument("--state-dir", default=None, help="episode store (default: per-user app state)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write editable repo config + next steps")
    p.add_argument("--repo", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("setup", help="detect machine config; --write persists it")
    p.add_argument("--model-file", default=None)
    p.add_argument("--model-alias", default=None)
    p.add_argument("--endpoint", default=None)
    p.add_argument("--server-binary", default=None)
    p.add_argument("--n-ctx", default=None, type=int)
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("doctor", help="report prerequisites; generates nothing by default")
    p.add_argument("--repo", required=True)
    p.add_argument("--probe-model", action="store_true",
                   help="run one explicit, recorded generative capability probe")
    p.add_argument("--profile", default=DEFAULT_PROFILE, help="named profile for the probe (default d2)")
    _add_llama_args(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("server", help="start|status|stop the local model server")
    server_sub = p.add_subparsers(dest="server_command", required=True)
    s = server_sub.add_parser("start", help="start the configured server (records ownership)")
    s.add_argument("--wait", default=90, type=int)
    server_sub.add_parser("status", help="report ownership and health; no generations")
    server_sub.add_parser("stop", help="stop only the server this tool started")
    p.set_defaults(func=cmd_server)

    p = sub.add_parser("run", help="run one bounded episode (default profile d2)")
    p.add_argument("--repo", required=True)
    p.add_argument("--ticket-file", required=True)
    p.add_argument("--profile", default=DEFAULT_PROFILE,
                   help="d2 (default), d1 (fallback), or fake:<name> for tests")
    p.add_argument("--execution", default=None, help="isolated|trusted-local (default: repo config)")
    p.add_argument("--episode-id", default=None)
    _add_llama_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("inspect", help="show stored episode evidence")
    p.add_argument("episode")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("resume", help="verify, reconcile, continue an episode")
    p.add_argument("episode")
    p.add_argument("--profile", default=None, help="must match the recorded profile")
    p.add_argument("--execution", default=None)
    _add_llama_args(p)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("diff", help="print stored final diff")
    p.add_argument("episode")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("accept", help="apply a solved candidate to the original tree")
    p.add_argument("episode")
    p.add_argument("--repo", required=True)
    p.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("discard", help="remove candidate; evidence preserved by default")
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
            p.add_argument("--profile", default=DEFAULT_PROFILE)
            p.add_argument("--execution", default=None)
            p.add_argument("--run-id", default=None)
            p.add_argument("--split", default="dev,held-out")
            p.add_argument("--max-tickets", default=20, type=int)
            p.add_argument("--output", default=None, help="sanitized report directory")
            p.add_argument("--validate-only", action="store_true")
            _add_llama_args(p)
        p.set_defaults(func=func)

    p = sub.add_parser("app-rpc", help="JSON-lines helper for Nicoder.app (not a network service)")
    p.set_defaults(func=_cmd_app_rpc)

    return parser


def _cmd_app_rpc(_args) -> int:
    """Start the stdin/stdout application helper."""
    from .app_rpc import main as rpc_main

    return rpc_main()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (installed console script)."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
