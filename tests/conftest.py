"""Shared fixtures: isolated package import, fixture copies, controller factory."""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from local_coder.config import load_config  # noqa: E402
from local_coder.controller import Controller  # noqa: E402
from local_coder.execution.trusted_local import TrustedLocalBackend  # noqa: E402
from local_coder.inference.fake import ScriptedFakeAdapter  # noqa: E402
from local_coder.persistence import EpisodeStore  # noqa: E402

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "demo_calc"
FIXTURE_REPO = FIXTURE_DIR / "repo"
GOLD_DIFF = FIXTURE_DIR / "gold.diff"
TICKET_FILE = FIXTURE_DIR / "ticket.md"


@pytest.fixture
def repo_copy(tmp_path):
    """Private writable copy of the demo fixture (originals never touched)."""
    dest = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, dest)
    return dest


@pytest.fixture
def state_dir(tmp_path):
    """Private episode store outside any source checkout."""
    path = tmp_path / "episodes"
    path.mkdir()
    return path


def make_controller(repo, state_dir, script, profile="test", episode_id=None,
                    backend=None, ticket_file=None, protocol_version="1",
                    verification_policy="off", profile_identity=None):
    """Build a controller with test-owned argv (current interpreter's pytest)."""
    from local_coder.execution.base import ExecutionBackend as _Backend

    config = load_config(repo)
    config.commands = {scope: [sys.executable, *argv[1:]] for scope, argv in config.commands.items()}
    store = EpisodeStore(state_dir)
    adapter = ScriptedFakeAdapter(list(script), profile)
    if backend is None:
        backend = TrustedLocalBackend()
    assert isinstance(backend, _Backend)
    ticket = Path(ticket_file).read_text(encoding="utf-8") if ticket_file else (
        FIXTURE_DIR / "ticket.md").read_text(encoding="utf-8")
    controller = Controller(store, config, adapter, backend, repo, ticket, episode_id=episode_id,
                            protocol_version=protocol_version,
                            verification_policy=verification_policy,
                            profile_identity=profile_identity)
    return controller, adapter


def fixture_paths(name):
    """Repo, ticket, gold, and labels for a named fixture."""
    root = Path(__file__).parent.parent / "fixtures" / name
    return {
        "repo": root / "repo",
        "ticket": root / "ticket.md",
        "gold": root / "gold.diff",
        "labels": root / "gold.json",
    }


@pytest.fixture
def isolated_backend():
    """Real container backend, or an honest BLOCKED skip (never host fallback)."""
    from local_coder.execution.isolated import IsolatedBackend, IsolationUnavailable

    backend = IsolatedBackend()
    try:
        backend.check_available()
    except IsolationUnavailable as exc:
        pytest.skip(f"BLOCKED: {exc}")
    return backend
