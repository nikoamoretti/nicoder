"""Bounded episode controller. Software owns budgets, paths, and truth.

State machine: INIT -> DISCOVER -> EDIT -> VERIFY -> TERMINAL, with RECOVER
entered when a check introduces a regression. Every attempted inference,
including malformed responses and transport failures, consumes one of eight
generation slots. Deterministic skill work never consumes a model slot but
has its own patch, check, command, and time budgets. No hidden retries, no
ninth generation: the loop condition and an explicit guard enforce it.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from . import briefing as briefing_mod
from .config import RepoConfig
from .execution.base import ExecutionBackend
from .execution.pytest_parser import clip_text
from .handoff import render_handoff
from .inference.base import AdapterError, ModelAdapter
from .paths import tree_hash as _tree_hash
from .persistence import EpisodeStore, new_episode_id, utc_now
from .protocol import (
    PROTOCOL_VERSION,
    PROTOCOL_VERSION_V2,
    PROTOCOL_VERSION_V3,
    CheckAction,
    DoneBlockedAction,
    DoneSolvedAction,
    FindAction,
    PatchApplyAction,
    PatchReplaceAction,
    PatchReplaceSpanAction,
    PatchRevertAction,
    ProtocolError,
    estimate_tokens,
    fingerprint,
    parse_raw,
)
from .retrieval.inventory import Inventory
from .skills import check as check_skill
from .skills import done as done_skill
from .skills import find as find_skill
from .skills import patch as patch_skill
from .state import CheckEvidence, EpisodeState
from .telemetry import controller_rss_mb
from .workspace import CandidateWorkspace

TERMINAL = "TERMINAL"

# Documented transition table: (phase, event) -> next phase.
TRANSITIONS = {
    ("INIT", "baseline_ok"): "DISCOVER",
    ("DISCOVER", "find"): "DISCOVER",
    ("DISCOVER", "patch"): "EDIT",
    ("DISCOVER", "check"): "DISCOVER",
    ("EDIT", "find"): "DISCOVER",
    ("EDIT", "patch"): "EDIT",
    ("EDIT", "check"): "VERIFY",
    ("VERIFY", "find"): "DISCOVER",
    ("VERIFY", "patch"): "EDIT",
    ("VERIFY", "check_clean"): "VERIFY",
    ("VERIFY", "check_regression"): "RECOVER",
    ("RECOVER", "find"): "DISCOVER",
    ("RECOVER", "patch"): "EDIT",
    ("RECOVER", "check_clean"): "VERIFY",
    ("RECOVER", "check_regression"): "RECOVER",
}


def allowed_actions(phase: str) -> set[str]:
    """Model-visible actions permitted in each controller phase."""
    if phase in ("DISCOVER", "EDIT", "VERIFY", "RECOVER"):
        return {"find", "patch", "check", "done"}
    return set()


class BudgetExhausted(Exception):
    """A ninth generation was refused without contacting any model."""


def _stored_raw(raw: str, limit: int = 65536) -> str:
    """Bound stored responses with an explicit marker (retention limit)."""
    if len(raw) <= limit:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return raw[:limit] + f"…[response artifact capped at {limit} chars; sha {digest}]"


def git_revision(repo: Path) -> str:
    """HEAD commit or a nogit marker with the tree identity."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, ValueError):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return "nogit"


def git_is_dirty(repo: Path) -> bool:
    """Whether a git checkout has uncommitted changes."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, ValueError):
        return False
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


class Controller:
    """Owns one episode from baseline to validated patch or handoff."""

    def __init__(
        self,
        store: EpisodeStore,
        config: RepoConfig,
        adapter: ModelAdapter,
        backend: ExecutionBackend,
        repo: Path,
        ticket: str,
        episode_id: str | None = None,
        task_id: str | None = None,
        protocol_version: str = PROTOCOL_VERSION,
        verification_policy: str = "off",
        profile_identity: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if protocol_version not in (PROTOCOL_VERSION, PROTOCOL_VERSION_V2, PROTOCOL_VERSION_V3):
            raise ValueError(f"unknown protocol version {protocol_version!r}")
        if verification_policy not in ("off", "mandatory"):
            raise ValueError(f"unknown verification policy {verification_policy!r}")
        self.store = store
        self.config = config
        self.adapter = adapter
        self.backend = backend
        self.repo = repo
        self.ticket = ticket
        self.episode_id = episode_id or new_episode_id()
        self.task_id = task_id
        self.protocol_version = protocol_version
        self.verification_policy = verification_policy
        self.profile_identity = dict(profile_identity or {})
        self.state: EpisodeState | None = None
        self.workspace: CandidateWorkspace | None = None
        self.original_manifest: dict[str, str] = {}
        self.reverse_cache: dict[str, dict[str, bytes]] = {}
        self.last_observation: dict | None = None
        self.started_mono = time.monotonic()
        self._tick = self.started_mono
        self._open_turn: int | None = None
        self._turn_obs_id: int | None = None
        self.cancel_event = cancel_event

    # -- lifecycle ------------------------------------------------------

    def start(self) -> EpisodeState:
        """Snapshot the tree, run baseline validation, enter DISCOVER."""
        from .paths import original_manifest, tree_hash

        if self.repo.joinpath(".git").exists() and git_is_dirty(self.repo):
            raise RuntimeError("refusing dirty git checkout; commit or stash first (v1 requires clean source)")
        try:
            self.store.state_dir.relative_to(self.repo)
            raise RuntimeError("state dir must live outside the source checkout (it would pollute retrieval)")
        except ValueError:
            pass
        if self.store.state_path(self.episode_id).exists():
            raise RuntimeError(f"episode {self.episode_id} already exists")
        self.original_manifest = original_manifest(self.repo)
        source_tree = tree_hash(self.original_manifest)
        self.store.create(self.episode_id)
        self.workspace = CandidateWorkspace.create(self.repo, self.store.candidate_dir(self.episode_id))
        candidate_tree = self.workspace.tree_hash()
        # Best-effort server metadata before provenance is recorded. Not a
        # model generation; failures leave honest unknowns in place.
        server_info = self.adapter.server_info()
        now = utc_now()
        state = EpisodeState(
            episode_id=self.episode_id,
            repo_path=str(self.repo),
            source_revision=git_revision(self.repo),
            source_tree_hash=source_tree,
            ticket=self.ticket,
            task_id=self.task_id,
            budgets=self.config.budgets,
            registry=dict(self.config.commands),
            registry_hash=self.config.registry_hash(),
            acceptance=self.config.acceptance,
            provenance={
                **self.adapter.provenance(),
                **self.profile_identity,
                "protocol_version": self.protocol_version,
                "verification_policy": self.verification_policy,
                "execution_backend": self.backend.name,
                "isolation": self.backend.isolation_label,
            },
            created_ts=now,
            updated_ts=now,
            candidate_tree_hash=candidate_tree,
        )
        self.state = state
        self.store.save_state(state)
        from .telemetry import snapshot as _memory_snapshot

        if server_info:
            state.provenance.update({
                f"server_{key}": str(server_info.get(key, "unknown"))[:200]
                for key in ("model_ftype", "build_info", "total_slots")
            })
            state.provenance["chat_template"] = (
                "present" if server_info.get("chat_template") else "unknown")
            self.store.save_state(state)

        worker_image = getattr(self.backend, "image", None)
        self._ev( "episode_created", {
            "repo": str(self.repo), "source_revision": state.source_revision,
            "source_tree": source_tree, "registry_hash": state.registry_hash,
            "memory": _memory_snapshot(worker_image=worker_image if isinstance(worker_image, str) else None),
        })
        self._run_baseline()
        return state

    # -- baseline / checks ----------------------------------------------

    def _check_number(self, scope: str) -> int:
        assert self.state is not None
        used = self.state.test_invocations
        return used + 1

    def _run_baseline(self) -> None:
        assert self.state is not None and self.workspace is not None
        state = self.state
        command = self.config.commands.get("baseline")
        if not command:
            self._finish("setup_failed", "no baseline command registered")
            return
        self._ev( "check_intent",
                                {"scope": "baseline", "tree": self.workspace.tree_hash()})
        state.test_invocations += 1
        evidence = check_skill.execute_scope(
            state, self.workspace, self.backend, "baseline", command,
            state.budgets.test_timeout_s, state.budgets.max_log_bytes,
            f"check-{self._check_number('baseline'):04d}.log",
            self.store.write_artifact_text, self.store.write_artifact_bytes,
        )
        state.baseline = evidence
        state.candidate_tree_hash = self.workspace.tree_hash()
        self._ev( "check_completed", {
            "scope": "baseline", "status": evidence.status,
            "failed": evidence.failed, "errors": evidence.errors,
            "collected": len(evidence.collected),
            "incomplete_reason": evidence.incomplete_reason or "",
        })
        if evidence.status == "incomplete":
            state.required_tests = []
            self.store.save_state(state)
            self._finish("setup_failed",
                         f"baseline setup failure: {evidence.incomplete_reason or 'unknown'}")
            return
        state.required_tests = list(evidence.collected)
        state.phase = TRANSITIONS[("INIT", "baseline_ok")]
        self.store.save_state(state)

    # -- generation slot --------------------------------------------------

    def _consume_generation(self) -> None:
        assert self.state is not None
        if self.state.generations_used >= self.state.budgets.max_generations:
            raise BudgetExhausted(
                f"generation budget spent ({self.state.generations_used}/"
                f"{self.state.budgets.max_generations}); ninth generation refused")
        self.state.generations_used += 1

    def _cancel_requested(self) -> bool:
        """Whether the application asked this episode to stop."""
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _finish_cancelled(self) -> None:
        """Stop after a user cancel. Evidence is preserved."""
        if hasattr(self.adapter, "cancel"):
            try:
                self.adapter.cancel()
            except (OSError, RuntimeError, ValueError):
                pass
        if self.state is not None and self.state.outcome == "running":
            self._ev("cancelled", {"detail": "cancelled by user"})
            self._finish("blocked", "cancelled by user")

    def step(self) -> bool:
        """Run one generation slot. Returns True when the episode continues."""
        assert self.state is not None and self.workspace is not None
        state = self.state
        if state.outcome != "running" or state.phase == TERMINAL:
            return False
        if self._cancel_requested():
            self._finish_cancelled()
            return False
        self._charge_elapsed()
        if state.elapsed_s > state.budgets.max_elapsed_s:
            self._finish("blocked", "elapsed time budget exceeded")
            return False
        if state.budgets.memory_rss_limit_mb is not None:
            rss = controller_rss_mb()
            if rss is not None and rss > state.budgets.memory_rss_limit_mb:
                self._finish("blocked", f"memory pressure: controller RSS {rss}MB over limit")
                return False
        if not self._drain_pending():
            return False
        counter = self.adapter.token_counter()
        try:
            text, spans, _omitted = briefing_mod.render_full(
                state, self.workspace, self.last_observation,
                counter=counter, context_limit=self.adapter.context_limit(),
                protocol_version=self.protocol_version,
                editable=self.config.editable_paths)
        except briefing_mod.ContextBudgetExceeded as exc:
            self._finish("blocked", f"context_budget_exceeded: {exc}")
            return False
        content_tokens = counter.count(text)
        count_label = "MEASURED" if counter.exact else "ESTIMATE"
        state.prompt_tokens_est += estimate_tokens(text)
        self.store.write_briefing(self.episode_id, text)
        briefing_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        step_index = state.generations_used
        turn = self._reserve_slot(text, counter, content_tokens, count_label, briefing_sha, step_index,
                                  spans=spans)
        try:
            return self._dispatch_turn(turn, text, step_index, content_tokens)
        finally:
            self._close_turn(turn)

    def _reserve_slot(
        self, text: str, counter, content_tokens: int, count_label: str,
        briefing_sha: str, step_index: int, spans: list | None = None,
    ) -> int:
        """Reserve and durably record a generation slot BEFORE dispatch.

        Raises BudgetExhausted without contacting any model when no slot
        remains. A reserved slot is consumed even if dispatch later fails
        with an unknown outcome: it is never silently resampled. The exact
        span list displayed in the briefing is persisted as spans.json so
        span IDs keep the meaning the model saw.
        """
        assert self.state is not None
        state = self.state
        turn = step_index + 1
        store, ep = self.store, self.episode_id
        store.write_turn_file(ep, turn, "state-pre.json", state.model_dump_json(indent=2))
        store.write_turn_file(ep, turn, "briefing.txt", text)
        store.write_turn_file(
            ep, turn, "focus.json",
            json.dumps(briefing_mod.focus_bindings(state), indent=2, sort_keys=True))
        if spans:
            store.write_turn_file(
                ep, turn, "spans.json",
                json.dumps([
                    {"span_id": span.span_id, "path": span.path,
                     "start_byte": span.start_byte, "end_byte": span.end_byte,
                     "file_sha256": span.file_sha256, "bytes_sha256": span.bytes_sha256,
                     "kind": span.kind, "enclosing": span.enclosing}
                    for span in spans
                ], indent=2, sort_keys=True))
        from .spans import SPAN_CATALOGUE_VERSION

        profile = {
            "protocol_version": self.protocol_version,
            "constraint": getattr(self.adapter, "constraint", "json_object"),
            "catalogue": SPAN_CATALOGUE_VERSION if self.protocol_version == "3" else "none",
            "counter": counter.name,
            "counter_exact": counter.exact,
            "adapter": self.adapter.provenance(),
        }
        store.write_turn_file(ep, turn, "profile.json", json.dumps(profile, indent=2, sort_keys=True))
        store.write_turn_file(ep, turn, "status.json", json.dumps({"status": "reserved", "turn": turn}))
        self._consume_generation()
        self._open_turn = turn
        self._turn_obs_id = id(self.last_observation)
        self.store.save_state(state)
        self._ev("model_request", {
            "step": step_index, "briefing_sha": briefing_sha,
            "prompt_tokens_est": str(estimate_tokens(text)),
            f"prompt_tokens_{count_label}": str(content_tokens),
            "counter": counter.name,
        })
        return turn

    def _dispatch_turn(self, turn: int, text: str, step_index: int, content_tokens: int) -> bool:
        """Generate, parse, and execute one reserved slot."""
        assert self.state is not None and self.workspace is not None
        state = self.state
        try:
            raw = self.adapter.generate(text, step_index)
        except AdapterError as exc:
            diagnostics = getattr(exc, "diagnostics", None)
            self._turn_write(turn, "response.json", json.dumps(
                {"ok": False, "stage": "dispatch", "detail": clip_text(str(exc), 300),
                 "diagnostics": diagnostics}, indent=2, default=str))
            self._turn_write(turn, "action.json", json.dumps(
                {"ok": False, "stage": "dispatch",
                 "detail": "no response to parse; slot stays consumed"}, indent=2))
            self._set_turn_status(turn, "dispatch_error")
            self.last_observation = {"status": "adapter_error", "detail": clip_text(str(exc), 300),
                                     "diagnostics": diagnostics}
            self._ev("adapter_error", {"detail": clip_text(str(exc), 300),
                                       "diagnostics": diagnostics})
            self.store.save_state(state)
            if self._cancel_requested():
                self._finish_cancelled()
                return False
            return self._still_running()
        state.completion_tokens_est += estimate_tokens(raw)
        usage = dict(self.adapter.last_usage or {})
        if isinstance(usage.get("prompt_tokens"), int):
            state.prompt_tokens_exact += usage["prompt_tokens"]
            overhead = usage["prompt_tokens"] - content_tokens
            if overhead >= 0:
                state.template_overhead_measured = overhead
        if isinstance(usage.get("completion_tokens"), int):
            state.completion_tokens_exact += usage["completion_tokens"]
        request = dict(getattr(self.adapter, "last_request", None) or {})
        if not request:
            request = {"note": "adapter did not report a request payload"}
        self._turn_write(turn, "request.json", json.dumps(request, indent=2, sort_keys=True, default=str))
        self._turn_write(turn, "response.json", json.dumps(
            {"ok": True, "raw": _stored_raw(raw), "usage": usage}, indent=2, default=str))
        self._set_turn_status(turn, "responded")
        self._ev("model_response",
                 {"step": step_index, "chars": str(len(raw)),
                  "usage": {k: str(v) for k, v in usage.items()}})
        if usage.get("finish_reason") == "length":
            return self._protocol_error("generation truncated by output limit", "truncated_output")
        if estimate_tokens(raw) > state.budgets.max_action_tokens:
            return self._protocol_error("output exceeds action token budget", "output_limit")
        try:
            action = parse_raw(raw, self.protocol_version)
        except ProtocolError as exc:
            self._turn_write(turn, "action.json", json.dumps(
                {"ok": False, "code": exc.code, "detail": str(exc)[:500]}, indent=2))
            return self._protocol_error(str(exc), exc.code)
        self._turn_write(turn, "action.json", json.dumps(
            {"ok": True, "action": action.model_dump()}, indent=2, sort_keys=True, default=str))
        state.consecutive_invalid = 0
        if (self.verification_policy == "mandatory" and state.pending_verification is not None
                and action.action in ("find", "patch", "check")):
            self.last_observation = {"status": "rejected", "code": "verification_pending",
                                     "detail": "an edit awaits mandatory verification; "
                                               "the runtime checks first"}
            self._ev( "observation",
                                    {"action": action.action, "status": "rejected"})
            self.store.save_state(state)
            return self._still_running()
        if action.action not in allowed_actions(state.phase):
            self.last_observation = {"status": "rejected", "code": "illegal_phase",
                                     "detail": f"{action.action} not allowed in {state.phase}"}
            self._ev( "observation",
                                    {"action": action.action, "status": "rejected"})
            self.store.save_state(state)
            return self._still_running()
        if isinstance(action, FindAction) and action.hypothesis:
            state.hypothesis = action.hypothesis
        if isinstance(action, PatchApplyAction | PatchRevertAction | CheckAction | PatchReplaceSpanAction) \
                and action.hypothesis:
            state.hypothesis = action.hypothesis
        state.last_action = briefing_mod.action_preview(action)
        if isinstance(action, FindAction):
            cont = self._do_find(action)
        elif isinstance(action, PatchApplyAction | PatchRevertAction | PatchReplaceAction
                         | PatchReplaceSpanAction):
            cont = self._do_patch(action)
        elif isinstance(action, CheckAction):
            cont = self._do_check(action)
        else:
            assert isinstance(action, DoneSolvedAction | DoneBlockedAction)
            cont = self._do_done(action)
        self.store.save_state(state)
        return cont

    def _charge_elapsed(self) -> None:
        """Accumulate active episode time. Resumes never renew the allowance."""
        assert self.state is not None
        now = time.monotonic()
        self.state.elapsed_s += max(0.0, now - self._tick)
        self._tick = now

    def _ev(self, event_type: str, data: dict | None = None) -> int:
        """Append an event, linked to the open turn when inside one."""
        payload = dict(data or {})
        if self._open_turn is not None:
            payload.setdefault("turn", self._open_turn)
        return self.store.append_event(self.episode_id, event_type, payload)

    def _turn_write(self, turn: int, name: str, text: str) -> None:
        """Write one turn-bundle file (immutable once written)."""
        try:
            self.store.write_turn_file(self.episode_id, turn, name, text)
        except FileExistsError:
            pass

    def _set_turn_status(self, turn: int, status: str) -> None:
        """Advance the mutable status pointer (evidence files stay immutable)."""
        path = self.store.turn_dir(self.episode_id, turn) / "status.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"status": status, "turn": turn}) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _turn_status(self, turn: int) -> str:
        """Effective turn status from the pointer file."""
        try:
            data = json.loads(self.store.read_turn_file(self.episode_id, turn, "status.json"))
        except (OSError, ValueError):
            return "missing"
        return str(data.get("status", "missing"))

    def _close_turn(self, turn: int) -> None:
        """Finalize the turn bundle. Never raises; crash-safe by construction.

        A turn that ends while an exception is in flight keeps its
        reserved/responded status so resume reconciles it explicitly; only a
        normally finished turn is marked complete.
        """
        try:
            import sys as _sys

            assert self.state is not None
            if self._open_turn != turn:
                return
            # An observation is recorded only when this turn produced one. A
            # crash beforehand leaves observation.json absent so resume
            # reconciles the turn explicitly instead of reading a guess.
            if self._turn_obs_id is not None and id(self.last_observation) != self._turn_obs_id:
                self._turn_write(turn, "observation.json", json.dumps(
                    self.last_observation, indent=2, sort_keys=True, default=str))
            self._turn_write(turn, "state-post.json", self.state.model_dump_json(indent=2))
            if self._turn_status(turn) in ("reserved", "responded") and _sys.exc_info()[0] is None:
                self._set_turn_status(turn, "complete")
            try:
                self.store.write_turn_manifest(self.episode_id, turn)
            except OSError:
                pass
        except OSError:
            pass
        finally:
            self._open_turn = None
            self._turn_obs_id = None

    def _still_running(self) -> bool:
        assert self.state is not None
        self.store.save_state(self.state)
        return self.state.outcome == "running"

    def _protocol_error(self, detail: str, code: str) -> bool:
        assert self.state is not None
        self.state.consecutive_invalid += 1
        self.last_observation = {"status": "protocol_error", "code": code, "detail": clip_text(detail, 300)}
        self._ev( "protocol_error", {"code": code})
        if self.state.consecutive_invalid >= 2:
            self._finish("blocked", f"repeated invalid model output ({code})")
            return False
        return self._still_running()

    # -- repetition -------------------------------------------------------

    def _repeat(self, fp: str, label: str) -> bool:
        """True when this fingerprint repeats: warn once, then block."""
        assert self.state is not None
        if fp in self.state.fingerprints:
            self.state.repeat_streak += 1
            self._ev( "repetition_warning",
                                    {"label": label, "streak": str(self.state.repeat_streak)})
            self.last_observation = {"status": "no_progress", "warning":
                                     f"identical {label} on unchanged state; change approach or stop"}
            if self.state.repeat_streak >= 2:
                self._finish("blocked", f"consecutive no-progress actions ({label})")
                return True
            return True
        self.state.repeat_streak = 0
        self.state.fingerprints.append(fp)
        return False

    # -- skills -------------------------------------------------------------

    def _do_find(self, action: FindAction) -> bool:
        assert self.state is not None and self.workspace is not None
        state = self.state
        tree = self.workspace.tree_hash()
        if self._repeat(fingerprint(action, tree), f"find {action.query[:40]}"):
            self.store.save_state(state)
            return self._still_running()
        inventory = Inventory.build(
            self.workspace.root,
            allow=self.config.retrieval_allow,
            deny=self.config.retrieval_deny,
        )
        latest = state.current if state.current is not None else state.baseline
        traceback_paths = list(latest.relevant_frames) if latest is not None else []
        _, fresh, observation = find_skill.run_find(
            self.workspace.root, inventory, action.query, action.symbols,
            traceback_paths, state.ticket, state.budgets.max_focus_files, state.focus)
        if len(state.focus) + len(fresh) > state.budgets.max_focus_files:
            fresh = fresh[: max(0, state.budgets.max_focus_files - len(state.focus))]
            observation["note"] = "focus capped at 4 files"
        self.store.write_artifact_text(
            self.episode_id, f"search-{state.generations_used:04d}.json",
            json.dumps(observation, indent=2)[:20000])
        state.focus.extend(fresh)
        # Refresh hashes of existing focus entries against current bytes.
        for foc in state.focus:
            full = self.workspace.root / foc.path
            if full.exists():
                from .paths import sha256_file

                foc.sha256 = sha256_file(full)
        state.phase = TRANSITIONS.get((state.phase, "find"), state.phase)
        self.last_observation = observation
        self._ev( "observation",
                                {"action": "find", "status": observation["status"],
                                 "new_focus": [f.path for f in fresh]})
        return self._still_running()

    def _do_patch(self, action: PatchApplyAction | PatchRevertAction | PatchReplaceAction
                    | PatchReplaceSpanAction) -> bool:
        assert self.state is not None and self.workspace is not None
        state = self.state
        if state.patch_attempts >= state.budgets.max_patch_attempts:
            self.last_observation = {"status": "rejected", "code": "patch_budget",
                                     "detail": f"patch attempts spent ({state.patch_attempts}/"
                                               f"{state.budgets.max_patch_attempts})"}
            self._ev( "observation",
                                    {"action": "patch", "status": "rejected"})
            return self._still_running()
        if self.verification_policy == "mandatory" and not self._validation_budget_ok():
            self._finish("blocked", "no validation budget for mandatory post-edit check")
            return False
        state.patch_attempts += 1
        if isinstance(action, PatchRevertAction):
            return self._do_revert(action)
        if isinstance(action, PatchReplaceAction):
            return self._do_replace(action)
        if isinstance(action, PatchReplaceSpanAction):
            return self._do_replace_span(action)
        if not state.focus:
            self.last_observation = {"status": "rejected", "code": "need_find",
                                     "detail": "PATCH requires current focus evidence; use FIND first"}
            self._ev( "observation",
                                    {"action": "patch", "status": "rejected"})
            return self._still_running()
        tree = self.workspace.tree_hash()
        if self._repeat(fingerprint(action, tree), "patch"):
            self.store.save_state(state)
            return self._still_running()
        diff_sha = hashlib.sha256(action.diff.encode("utf-8")).hexdigest()[:16]
        self._ev( "patch_intent",
                                {"diff_sha": diff_sha, "base_tree": tree})
        self.store.write_artifact_text(self.episode_id, f"patch-{state.generations_used:04d}.diff", action.diff)
        observation, record, reverse = patch_skill.run_patch_apply(
            state, self.workspace, self.store.artifacts_dir(self.episode_id),
            action.diff, action.expected_effect, self.config.editable_paths,
            self.episode_id, self.store.write_artifact_text)
        if record is None or reverse is None:
            self.last_observation = observation
            self._ev( "patch_rejected", observation)
            return self._still_running()
        if state.last_patch is not None:
            state.patch_history.append(state.last_patch)
        state.last_patch = record
        self.reverse_cache[record.sha] = reverse
        for new in record.files:
            if new not in state.modified_files:
                state.modified_files.append(new)
        for foc in state.focus:
            if foc.path in record.files:
                from .paths import sha256_file

                foc.sha256 = sha256_file(self.workspace.root / foc.path)
        state.candidate_tree_hash = self.workspace.tree_hash()
        state.phase = TRANSITIONS.get((state.phase, "patch"), state.phase)
        if not self._verify_after_edit(record.sha, observation):
            self.last_observation = observation
            return False
        self.last_observation = observation
        self._ev( "patch_applied", {
            "patch": record.sha, "files": record.files,
            "result_tree": record.result_tree})
        return self._still_running()

    def _do_replace(self, action: PatchReplaceAction) -> bool:
        """Execute one protocol-v2 exact-replacement PATCH attempt."""
        assert self.state is not None and self.workspace is not None
        state = self.state
        if self.protocol_version != PROTOCOL_VERSION_V2:
            self.last_observation = {"status": "rejected", "code": "wrong_protocol",
                                     "detail": "replace requires protocol v2"}
            self._ev( "observation",
                                    {"action": "patch", "status": "rejected"})
            return self._still_running()
        if not state.focus:
            self.last_observation = {"status": "rejected", "code": "need_find",
                                     "detail": "PATCH requires current focus evidence; use FIND first"}
            self._ev( "observation",
                                    {"action": "patch", "status": "rejected"})
            return self._still_running()
        tree = self.workspace.tree_hash()
        if self._repeat(fingerprint(action, tree), "patch-replace"):
            self.store.save_state(state)
            return self._still_running()
        bindings = self._turn_bindings()
        if bindings is None:
            self.last_observation = {"status": "rejected", "code": "missing_bindings",
                                     "detail": "no focus bindings for this turn; re-FIND before patching"}
            self._ev( "observation",
                                    {"action": "patch", "status": "rejected"})
            return self._still_running()
        resolved: list[tuple[str, str, str]] = []
        file_hashes: dict[str, str] = {}
        for edit in action.edits:
            binding = bindings.get(edit.file_id)
            if binding is None:
                self.last_observation = {"status": "rejected", "code": "unknown_file_id",
                                         "detail": f"file_id {edit.file_id} is not a current focus file"}
                self._ev( "observation",
                                        {"action": "patch", "status": "rejected"})
                return self._still_running()
            resolved.append((binding["path"], edit.old, edit.new))
            file_hashes[binding["path"]] = binding["sha256"]
        edits_sha = hashlib.sha256(
            json.dumps(action.model_dump()["edits"], sort_keys=True).encode("utf-8")).hexdigest()[:16]
        self._ev( "patch_intent",
                                {"edits_sha": edits_sha, "base_tree": tree, "interface": "replace"})
        observation, record, reverse, rendered = patch_skill.run_patch_replace(
            state, self.workspace, self.store.artifacts_dir(self.episode_id),
            resolved, file_hashes, self.config.editable_paths,
            self.episode_id, self.store.write_artifact_text)
        self.store.write_artifact_text(
            self.episode_id, f"patch-{state.generations_used:04d}.diff", rendered)
        if record is None or reverse is None:
            self.last_observation = observation
            self._ev( "patch_rejected", observation)
            return self._still_running()
        if state.last_patch is not None:
            state.patch_history.append(state.last_patch)
        state.last_patch = record
        self.reverse_cache[record.sha] = reverse
        for new in record.files:
            if new not in state.modified_files:
                state.modified_files.append(new)
        for foc in state.focus:
            if foc.path in record.files:
                from .paths import sha256_file

                foc.sha256 = sha256_file(self.workspace.root / foc.path)
        state.candidate_tree_hash = self.workspace.tree_hash()
        state.phase = TRANSITIONS.get((state.phase, "patch"), state.phase)
        if not self._verify_after_edit(record.sha, observation):
            self.last_observation = observation
            return False
        self.last_observation = observation
        self._ev( "patch_applied", {
            "patch": record.sha, "files": record.files,
            "result_tree": record.result_tree, "interface": "replace"})
        return self._still_running()

    def _turn_bindings(self) -> dict[int, dict[str, str]] | None:
        """Focus file-ID bindings snapshotted for the open turn, if any."""
        if self._open_turn is None:
            return None
        try:
            raw = self.store.read_turn_file(self.episode_id, self._open_turn, "focus.json")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        bindings: dict[int, dict[str, str]] = {}
        for key, value in data.items():
            try:
                bindings[int(key)] = {"path": str(value["path"]), "sha256": str(value["sha256"])}
            except (ValueError, KeyError, TypeError):
                return None
        return bindings

    def _turn_spans(self) -> dict[str, dict[str, object]] | None:
        """Span bindings snapshotted for the open turn, if any."""
        if self._open_turn is None:
            return None
        try:
            raw = self.store.read_turn_file(self.episode_id, self._open_turn, "spans.json")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, list):
            return None
        bindings: dict[str, dict[str, object]] = {}
        for entry in data:
            try:
                span_id = str(entry["span_id"])
                bindings[span_id] = {
                    "path": str(entry["path"]),
                    "file_sha256": str(entry["file_sha256"]),
                    "start_byte": int(entry["start_byte"]),
                    "end_byte": int(entry["end_byte"]),
                    "bytes_sha256": str(entry["bytes_sha256"]),
                }
            except (ValueError, KeyError, TypeError):
                return None
        return bindings

    def _do_replace_span(self, action: PatchReplaceSpanAction) -> bool:
        """Execute one protocol-v3 span-selection PATCH attempt."""
        assert self.state is not None and self.workspace is not None
        state = self.state
        if self.protocol_version != PROTOCOL_VERSION_V3:
            self.last_observation = {"status": "rejected", "code": "wrong_protocol",
                                     "detail": "replace_span requires protocol v3"}
            self._ev( "observation",
                                    {"action": "patch", "status": "rejected"})
            return self._still_running()
        if not state.focus:
            self.last_observation = {"status": "rejected", "code": "need_find",
                                     "detail": "PATCH requires current focus evidence; use FIND first"}
            self._ev( "observation",
                                    {"action": "patch", "status": "rejected"})
            return self._still_running()
        tree = self.workspace.tree_hash()
        if self._repeat(fingerprint(action, tree), "patch-replace-span"):
            self.store.save_state(state)
            return self._still_running()
        bindings = self._turn_spans()
        if bindings is None:
            self.last_observation = {"status": "rejected", "code": "missing_bindings",
                                     "detail": "no span bindings for this turn; re-FIND before patching"}
            self._ev( "observation",
                                    {"action": "patch", "status": "rejected"})
            return self._still_running()
        resolved: list[tuple[str, int, int, str, str]] = []
        file_hashes: dict[str, str] = {}
        for edit in action.edits:
            binding = bindings.get(edit.span_id)
            if binding is None:
                self.last_observation = {"status": "rejected", "code": "unknown_span_id",
                                         "detail": f"span_id {edit.span_id} is not an editable span "
                                                   "shown in this request"}
                self._ev( "observation",
                                        {"action": "patch", "status": "rejected"})
                return self._still_running()
            path = str(binding["path"])
            start_byte = binding["start_byte"]
            end_byte = binding["end_byte"]
            assert isinstance(start_byte, int) and isinstance(end_byte, int)
            resolved.append((path, start_byte, end_byte, edit.new, str(binding["bytes_sha256"])))
            file_hashes[path] = str(binding["file_sha256"])
        edits_sha = hashlib.sha256(
            json.dumps(action.model_dump()["edits"], sort_keys=True).encode("utf-8")).hexdigest()[:16]
        self._ev( "patch_intent",
                                {"edits_sha": edits_sha, "base_tree": tree, "interface": "replace_span"})
        observation, record, reverse, rendered = patch_skill.run_patch_replace_span(
            state, self.workspace, self.store.artifacts_dir(self.episode_id),
            resolved, file_hashes, self.config.editable_paths,
            self.episode_id, self.store.write_artifact_text)
        self.store.write_artifact_text(
            self.episode_id, f"patch-{state.generations_used:04d}.diff", rendered)
        if record is None or reverse is None:
            self.last_observation = observation
            self._ev( "patch_rejected", observation)
            return self._still_running()
        if state.last_patch is not None:
            state.patch_history.append(state.last_patch)
        state.last_patch = record
        self.reverse_cache[record.sha] = reverse
        for new in record.files:
            if new not in state.modified_files:
                state.modified_files.append(new)
        for foc in state.focus:
            if foc.path in record.files:
                from .paths import sha256_file

                foc.sha256 = sha256_file(self.workspace.root / foc.path)
        state.candidate_tree_hash = self.workspace.tree_hash()
        state.phase = TRANSITIONS.get((state.phase, "patch"), state.phase)
        if not self._verify_after_edit(record.sha, observation):
            self.last_observation = observation
            return False
        self.last_observation = observation
        self._ev( "patch_applied", {
            "patch": record.sha, "files": record.files,
            "result_tree": record.result_tree, "interface": "replace_span"})
        return self._still_running()

    def _do_revert(self, action: PatchRevertAction) -> bool:
        assert self.state is not None and self.workspace is not None
        state = self.state
        tree = self.workspace.tree_hash()
        if self._repeat(fingerprint(action, tree), "revert"):
            self.store.save_state(state)
            return self._still_running()
        reverse = self._reverse_for(state.last_patch.sha if state.last_patch else "")
        observation = patch_skill.run_patch_revert(state, self.workspace, reverse, action.reason_code)
        if observation.get("status") != "reverted":
            self.last_observation = observation
            self._ev( "patch_rejected", observation)
            return self._still_running()
        if state.last_patch is not None:
            state.patch_history.append(state.last_patch)
        reverted_sha = state.last_patch.sha if state.last_patch else ""
        reverted_files = list(state.last_patch.files) if state.last_patch else []
        state.last_patch = None
        from .paths import sha256_file as _sha

        for foc in state.focus:
            if foc.path in reverted_files:
                foc.sha256 = _sha(self.workspace.root / foc.path)
        state.candidate_tree_hash = self.workspace.tree_hash()
        state.phase = TRANSITIONS.get((state.phase, "patch"), state.phase)
        if not self._verify_after_edit(f"revert:{reverted_sha}", observation):
            self.last_observation = observation
            return False
        self.last_observation = observation
        self._ev( "patch_reverted", {"patch": reverted_sha})
        return self._still_running()

    # -- mandatory verification ---------------------------------------------

    def _validation_budget_ok(self) -> bool:
        """Room for one automatic check plus a future final validation."""
        assert self.state is not None
        state = self.state
        need_final = 0 if state.final_validations >= 1 else 1
        return state.test_invocations + 1 + need_final <= state.budgets.max_test_invocations

    def _run_automatic_check(self) -> CheckEvidence | None:
        """Execute the mandatory post-edit check. None iff the episode ended.

        Consumes one test invocation and one auto-check slot, never a model
        generation or a model-check slot. Recorded as controller-owned
        auto_check events, never as a model-selected CHECK.
        """
        assert self.state is not None and self.workspace is not None

        state = self.state
        command = self.config.commands.get("focused")
        if not command:
            self._finish("blocked", "no focused command for mandatory automatic verification")
            return None
        tree = self.workspace.tree_hash()
        state.test_invocations += 1
        state.auto_checks += 1
        self._ev( "auto_check", {"scope": "focused", "tree": tree})
        self.store.save_state(state)
        evidence = check_skill.execute_scope(
            state, self.workspace, self.backend, "focused", command,
            state.budgets.test_timeout_s, state.budgets.max_log_bytes,
            f"check-auto-{state.auto_checks:04d}.log",
            self.store.write_artifact_text, self.store.write_artifact_bytes)
        state.current = evidence
        state.candidate_tree_hash = self.workspace.tree_hash()
        comparison = check_skill.compare_to_baseline(state.baseline, evidence) if state.baseline else {}
        state.regression = bool(comparison.get("regression"))
        event = "check_regression" if state.regression else "check_clean"
        state.phase = TRANSITIONS.get((state.phase, event),
                                      TRANSITIONS.get((state.phase, "check"), state.phase))
        self._ev( "auto_check_completed", {
            "scope": evidence.scope, "status": evidence.status,
            "failed": evidence.failed, "errors": evidence.errors,
            "regression": str(state.regression)})
        self.store.save_state(state)
        return evidence

    def _verify_after_edit(self, patch_sha: str, observation: dict) -> bool:
        """Mandatory-policy hook after a content-changing edit. False => stop.

        Persists the pending verification, runs the automatic check at once,
        exposes its result inside the patch observation, and clears the
        pending record only against evidence on the exact pending tree. An
        incomplete check cannot verify anything: honest blocked outcome.
        """
        assert self.state is not None
        state = self.state
        if self.verification_policy != "mandatory":
            return True
        from .state import PendingVerification

        state.pending_verification = PendingVerification(tree=state.candidate_tree_hash,
                                                         patch_sha=patch_sha)
        self.store.save_state(state)
        evidence = self._run_automatic_check()
        if evidence is None:
            return False
        observation["auto_check"] = {
            "status": evidence.status,
            "scope": evidence.scope,
            "failed": list(evidence.failed),
            "errors": list(evidence.errors),
            "incomplete_reason": evidence.incomplete_reason,
        }
        if evidence.status == "incomplete":
            self._finish("blocked",
                         f"automatic verification incomplete: {evidence.incomplete_reason or 'unknown'}")
            return False
        pending = state.pending_verification
        if pending is not None and evidence.tree_hash == pending.tree:
            state.pending_verification = None
        self.store.save_state(state)
        return state.outcome == "running"

    def _drain_pending(self) -> bool:
        """Run persisted pending verification before model inference.

        Consumes no generation slot. Returns False when the drain finished
        the episode (blocked); True means proceed to the model decision.
        """
        assert self.state is not None and self.workspace is not None
        state = self.state
        if self.verification_policy != "mandatory" or state.pending_verification is None:
            return True
        if state.outcome != "running" or state.phase == TERMINAL:
            return False
        pending = state.pending_verification
        if self.workspace.tree_hash() != pending.tree:
            self._finish("blocked", "pending verification tree lost before its check ran")
            return False
        if state.test_invocations + 1 > state.budgets.max_test_invocations:
            self._finish("blocked", "no validation budget left for pending verification")
            return False
        evidence = self._run_automatic_check()
        if evidence is None:
            return False
        if evidence.status == "incomplete" or evidence.tree_hash != pending.tree:
            self._finish("blocked",
                         f"pending verification did not complete: "
                         f"{evidence.incomplete_reason or 'tree moved'}")
            return False
        state.pending_verification = None
        self.store.save_state(state)
        return True

    def _reverse_for(self, sha: str) -> dict[str, bytes] | None:
        assert self.state is not None
        if sha in self.reverse_cache:
            return self.reverse_cache[sha]
        for record in ([*self.state.patch_history, self.state.last_patch] if self.state.last_patch
                       else self.state.patch_history):
            if record.sha == sha:
                try:
                    raw = self.store.read_artifact_text(self.episode_id, record.reverse_artifact)
                    data = {k: bytes.fromhex(v) for k, v in json.loads(raw).items()}
                except (OSError, ValueError):
                    return None
                self.reverse_cache[sha] = data
                return data
        return None

    def _do_check(self, action: CheckAction) -> bool:
        assert self.state is not None and self.workspace is not None
        state = self.state
        if state.model_checks >= state.budgets.max_model_checks:
            self.last_observation = {"status": "rejected", "code": "check_budget",
                                     "detail": "model-requested checks spent"}
            self._ev( "observation",
                                    {"action": "check", "status": "rejected"})
            return self._still_running()
        if state.test_invocations >= state.budgets.max_test_invocations:
            self.last_observation = {"status": "rejected", "code": "test_budget",
                                     "detail": "total test-command invocations spent"}
            self._ev( "observation",
                                    {"action": "check", "status": "rejected"})
            return self._still_running()
        tree = self.workspace.tree_hash()
        if self._repeat(fingerprint(action, tree), f"check {action.scope}"):
            self.store.save_state(state)
            return self._still_running()
        command = self.config.commands.get(action.scope)
        if not command:
            self.last_observation = {"status": "rejected", "code": "unknown_scope",
                                     "detail": f"no registered command for {action.scope}"}
            return self._still_running()
        state.model_checks += 1
        state.test_invocations += 1
        self._ev( "check_intent",
                                {"scope": action.scope, "tree": tree})
        self.store.save_state(state)
        evidence = check_skill.execute_scope(
            state, self.workspace, self.backend, action.scope, command,
            state.budgets.test_timeout_s, state.budgets.max_log_bytes,
            f"check-{state.generations_used:04d}.log",
            self.store.write_artifact_text, self.store.write_artifact_bytes)
        state.current = evidence
        state.candidate_tree_hash = self.workspace.tree_hash()
        comparison = check_skill.compare_to_baseline(state.baseline, evidence) if state.baseline else {}
        state.regression = bool(comparison.get("regression"))
        event = "check_regression" if state.regression else "check_clean"
        state.phase = TRANSITIONS.get((state.phase, event),
                                      TRANSITIONS.get((state.phase, "check"), state.phase))
        observation = {
            "status": evidence.status,
            "scope": evidence.scope,
            "exit_code": evidence.exit_code,
            "passed": len(evidence.passed),
            "failed": evidence.failed[:10],
            "errors": evidence.errors[:10],
            "skipped": evidence.skipped[:10],
            "fixed_vs_baseline": comparison.get("fixed", []),
            "new_vs_baseline": comparison.get("new_failures", []),
            "regression": state.regression,
            "failure": evidence.failure_summary,
            "frames": evidence.relevant_frames[:5],
            "incomplete_reason": evidence.incomplete_reason,
            "log": evidence.log_artifact,
        }
        self.last_observation = observation
        self._ev( "check_completed", {
            "scope": evidence.scope, "status": evidence.status,
            "failed": evidence.failed, "regression": str(state.regression)})
        return self._still_running()

    def _do_done(self, action: DoneSolvedAction | DoneBlockedAction) -> bool:
        assert self.state is not None
        if isinstance(action, DoneBlockedAction):
            self.last_observation = {"status": "stopped", "reason_code": action.reason_code,
                                     "summary": clip_text(action.summary, 300)}
            self._finish("blocked", f"{action.reason_code}: {clip_text(action.summary, 300)}")
            return False
        return self._validate_solved_request(action.summary)

    def _validate_solved_request(self, summary: str) -> bool:
        assert self.state is not None and self.workspace is not None
        state = self.state
        if state.final_validations >= state.budgets.final_invocations:
            self._finish("blocked", "final validation budget spent; cannot prove solved")
            return False
        if state.test_invocations >= state.budgets.max_test_invocations:
            self._finish("blocked", "test budget spent; cannot run final validation")
            return False
        command = self.config.commands.get("final")
        if not command:
            self._finish("blocked", "no final validation command registered")
            return False
        state.test_invocations += 1
        state.final_validations += 1
        self._ev( "check_intent",
                                {"scope": "final", "tree": self.workspace.tree_hash()})
        self.store.save_state(state)
        evidence = check_skill.execute_scope(
            state, self.workspace, self.backend, "final", command,
            state.budgets.test_timeout_s, state.budgets.max_log_bytes,
            "check-final.log",
            self.store.write_artifact_text, self.store.write_artifact_bytes)
        state.final = evidence
        state.candidate_tree_hash = self.workspace.tree_hash()
        if (state.pending_verification is not None
                and evidence.tree_hash == state.pending_verification.tree):
            state.pending_verification = None
        ok, reasons = done_skill.validate_solved(state, self.workspace,
                                                 self.original_manifest, self.config.editable_paths)
        self._ev( "done_validated",
                                {"ok": str(ok), "reasons": reasons})
        if ok:
            self.last_observation = {"status": "solved", "summary": clip_text(summary, 300)}
            self._finish("solved", clip_text(summary, 300))
            return False
        self.last_observation = {"status": "not_solved", "reasons": reasons}
        return self._still_running()

    # -- finishing ----------------------------------------------------------

    def _finish(self, outcome: str, reason: str) -> None:
        assert self.state is not None and self.workspace is not None
        state = self.state
        self._charge_elapsed()
        state.outcome = outcome
        state.stop_reason = reason
        state.phase = TERMINAL
        changed = sorted(
            rel for rel in set(self.original_manifest) | set(self.workspace.manifest())
            if self.original_manifest.get(rel) != self.workspace.manifest().get(rel))
        final_diff = self._render_final_diff(changed)
        self.store.write_artifact_text(self.episode_id, "final.diff", final_diff)
        handoff = render_handoff(state, changed, str(self.store.episode_dir(self.episode_id)))
        self.store.write_handoff(self.episode_id, handoff)
        from .telemetry import snapshot as _memory_snapshot

        worker_image = getattr(self.backend, "image", None)
        self._ev( "handoff_written", {
            "outcome": outcome, "reason": clip_text(reason, 300),
            "memory": _memory_snapshot(worker_image=worker_image if isinstance(worker_image, str) else None),
            "prompt_tokens_exact": str(state.prompt_tokens_exact),
            "completion_tokens_exact": str(state.completion_tokens_exact),
            "prompt_tokens_est": str(state.prompt_tokens_est),
            "completion_tokens_est": str(state.completion_tokens_est)})
        self.store.save_state(state)

    def _render_final_diff(self, changed: list[str]) -> str:
        assert self.workspace is not None
        chunks: list[str] = []
        for rel in changed:
            old_bytes: bytes | None = None
            new_bytes: bytes | None = None
            # Original bytes live outside the candidate; recover text safely.
            orig = self.repo / rel
            if orig.exists():
                try:
                    old_bytes = orig.read_bytes()
                except OSError:
                    old_bytes = None
            new_path = self.workspace.root / rel
            if new_path.exists():
                try:
                    new_bytes = new_path.read_bytes()
                except OSError:
                    new_bytes = None
            try:
                old_lines = old_bytes.decode("utf-8").splitlines() if old_bytes is not None else []
                new_lines = new_bytes.decode("utf-8").splitlines() if new_bytes is not None else []
            except UnicodeDecodeError:
                continue
            chunks.extend(difflib.unified_diff(old_lines, new_lines,
                                               fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
        body = "\n".join(chunks)
        return body + ("\n" if body else "")

    # -- run ------------------------------------------------------------------

    def run(self) -> EpisodeState:
        """Baseline, then at most max_generations slots, then reserved finish."""
        self.start()
        assert self.state is not None
        while self.state.outcome == "running" and \
                self.state.generations_used < self.state.budgets.max_generations:
            if self._cancel_requested():
                self._finish_cancelled()
                break
            if not self.step():
                break
        if self.state.outcome == "running":
            if self._cancel_requested():
                self._finish_cancelled()
            else:
                self._finish_at_exhaustion()
        return self.state

    def _finish_at_exhaustion(self) -> None:
        """Reserved final validation without a ninth model call, or blocked."""
        assert self.state is not None and self.workspace is not None
        state = self.state
        changed = state.candidate_tree_hash != state.source_tree_hash
        can_validate = (changed
                        and state.final_validations < state.budgets.final_invocations
                        and state.test_invocations < state.budgets.max_test_invocations
                        and self.config.commands.get("final") is not None)
        if can_validate:
            command = self.config.commands["final"]
            state.test_invocations += 1
            state.final_validations += 1
            self._ev( "check_intent",
                                    {"scope": "final", "tree": state.candidate_tree_hash,
                                     "reserved": "true"})
            evidence = check_skill.execute_scope(
                state, self.workspace, self.backend, "final", command,
                state.budgets.test_timeout_s, state.budgets.max_log_bytes,
                "check-final.log",
                self.store.write_artifact_text, self.store.write_artifact_bytes)
            state.final = evidence
            if (state.pending_verification is not None
                    and evidence.tree_hash == state.pending_verification.tree):
                state.pending_verification = None
            ok, _ = done_skill.validate_solved(state, self.workspace,
                                               self.original_manifest, self.config.editable_paths)
            if ok:
                self._finish("solved", "reserved final validation passed at budget exhaustion")
                return
        self._finish("blocked", "decision budget exhausted (8 generations, no ninth call)")

    # -- resume -----------------------------------------------------------------

    def resume(self, episode_id: str, adapter: ModelAdapter | None = None) -> EpisodeState:
        """Verify candidate and events, reconcile intents, continue if running."""
        from .paths import original_manifest

        self.episode_id = episode_id
        state = self.store.load_state(episode_id)
        self.state = state
        stored_policy = state.provenance.get("verification_policy", "off")
        if stored_policy in ("off", "mandatory"):
            self.verification_policy = stored_policy
        if adapter is not None:
            self.adapter = adapter
        self.repo = Path(state.repo_path)
        self.original_manifest = original_manifest(self.repo)
        if self.original_manifest and state.source_tree_hash not in ("", None):
            current_orig = _tree_hash(self.original_manifest)
            if current_orig != state.source_tree_hash:
                self._ev("resume_warning",
                         {"detail": "original tree drifted since episode start"})
        self.workspace = CandidateWorkspace(self.store.candidate_dir(episode_id))
        if not self.store.candidate_dir(episode_id).exists():
            raise FileNotFoundError(f"candidate workspace missing for {episode_id}")
        self._reconcile_intents()
        self._ev("resumed",
                 {"outcome": state.outcome, "generations_used": str(state.generations_used),
                  "elapsed_s": str(round(state.elapsed_s, 1))})
        self.store.save_state(state)
        # Restore reverse cache for recorded patches.
        for record in ([*state.patch_history, state.last_patch] if state.last_patch
                       else state.patch_history):
            self._reverse_for(record.sha)
        if state.outcome != "running":
            return state
        self.started_mono = time.monotonic()
        self._tick = time.monotonic()
        if not self._drain_pending():
            return state
        while state.outcome == "running" and \
                state.generations_used < state.budgets.max_generations:
            if self._cancel_requested():
                self._finish_cancelled()
                break
            if not self.step():
                break
        if state.outcome == "running":
            if self._cancel_requested():
                self._finish_cancelled()
            else:
                self._finish_at_exhaustion()
        return state

    def _reconcile_intents(self) -> None:
        """Distinguish applied, unapplied, and interrupted actions from records."""
        assert self.state is not None and self.workspace is not None
        self._reconcile_turns()
        events = self.store.read_events(self.episode_id)
        intents = [e for e in events if e["type"] in ("patch_intent", "check_intent")]
        dones = [e for e in events if e["type"] in ("patch_applied", "patch_reverted",
                                                    "patch_rejected", "check_completed")]
        if not intents:
            return
        last_intent = intents[-1]
        completed_after = [e for e in dones if e["seq"] > last_intent["seq"]]
        if completed_after:
            return
        tree = self.workspace.tree_hash()
        if last_intent["type"] == "patch_intent":
            if tree == last_intent["data"].get("base_tree"):
                self._ev( "reconciled",
                                        {"intent": "patch", "result": "unapplied"})
            else:
                self._ev( "reconciled",
                                        {"intent": "patch", "result": "interrupted-unknown"})
                self.state.stop_reason = "interrupted patch reconciled as unknown; re-FIND and re-verify"
        else:
            self._ev( "reconciled",
                                    {"intent": "check", "result": "unknown-until-next-check"})

    def _reconcile_turns(self) -> None:
        """Reconcile interrupted model-request turns without resampling.

        A reserved turn with no response, or a responded turn with no
        observation, keeps its consumed slot and is marked via a new
        reconciled.json record (existing files are never rewritten). A
        recorded response is never executed twice. Turn directories beyond
        the consumed slot count are pre-reservation leftovers and are moved
        aside under a new name, never overwritten.
        """
        assert self.state is not None
        store, ep = self.store, self.episode_id
        for turn in store.list_turns(ep):
            if turn > self.state.generations_used:
                abandoned = store.episode_dir(ep) / "turns" / f"{turn:04d}.abandoned"
                try:
                    store.turn_dir(ep, turn).rename(abandoned)
                except OSError:
                    pass
                self._ev("reconciled", {"turn": turn, "result": "abandoned-prereserve"})
        for turn in range(1, self.state.generations_used + 1):
            directory = store.turn_dir(ep, turn)
            if not directory.exists():
                self._ev("reconciled", {"turn": turn, "result": "missing-turn-dir"})
                continue
            if (directory / "reconciled.json").exists():
                continue
            status = self._turn_status(turn)
            has_response = (directory / "response.json").exists()
            has_observation = (directory / "observation.json").exists()
            if not has_response:
                outcome = "interrupted-unknown-outcome"
            elif not has_observation:
                outcome = "response-without-observation"
            else:
                continue
            try:
                (directory / "reconciled.json").write_text(json.dumps(
                    {"turn": turn, "from": status, "to": outcome}) + "\n", encoding="utf-8")
            except OSError:
                pass
            self._ev("reconciled", {"turn": turn, "result": outcome})
