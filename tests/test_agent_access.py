"""Agent access rules: read the home folder minus secrets, write only the working copy."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from local_coder.agent.access import AccessDenied, AccessPolicy, secret_name


@pytest.fixture
def home(tmp_path) -> Path:
    root = Path(os.path.realpath(tmp_path / "home"))
    (root / "projects" / "app" / "src").mkdir(parents=True)
    (root / "projects" / "app" / "src" / "main.ts").write_text("export {}\n", encoding="utf-8")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_ed25519").write_text("key", encoding="utf-8")
    (root / "Library" / "Keychains").mkdir(parents=True)
    (root / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Notes").mkdir(parents=True)
    return root


def test_reads_anywhere_in_home(home):
    policy = AccessPolicy(home=home)
    assert policy.readable("~/projects/app/src/main.ts", home) == home / "projects/app/src/main.ts"
    assert policy.readable("projects/app", home) == home / "projects/app"


def test_outside_home_is_refused(home, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(AccessDenied, match="outside the home folder"):
        AccessPolicy(home=home).readable(str(outside), home)


@pytest.mark.parametrize("rel", [".ssh", ".ssh/id_ed25519", "Library/Keychains", "Library", ".config/gh/hosts.yml"])
def test_private_places_are_refused(home, rel):
    with pytest.raises(AccessDenied, match="private"):
        AccessPolicy(home=home).readable(f"~/{rel}", home)


def test_icloud_drive_stays_readable(home):
    policy = AccessPolicy(home=home)
    path = policy.readable("~/Library/Mobile Documents/com~apple~CloudDocs/Notes", home)
    assert path.name == "Notes"
    with pytest.raises(AccessDenied, match="secret"):
        policy.readable("~/Library/Mobile Documents/com~apple~CloudDocs/Notes/Keys.md", home)


@pytest.mark.parametrize("name", [
    ".env", ".env.local", "id_rsa", "server.pem", "Keys.md", "keys.md", "credentials.json", ".npmrc",
    ".zsh_history", "engine-secret", "auth.json", "client_secret_123.json", "secrets.yaml",
])
def test_secret_names(name):
    assert secret_name(name)


@pytest.mark.parametrize("name", [
    ".env.example", "CredentialsForm.tsx", "useSecretStore.ts", "secret-santa.md", "cacert.pem", "keys.ts",
])
def test_ordinary_names_stay_readable(name):
    assert not secret_name(name)


def test_symlink_to_a_private_place_is_refused(home):
    (home / "projects" / "app" / "shortcut").symlink_to(home / ".ssh")
    with pytest.raises(AccessDenied, match="private"):
        AccessPolicy(home=home).readable("~/projects/app/shortcut/id_ed25519", home)


def test_read_only_policy_refuses_writes(home):
    with pytest.raises(AccessDenied, match="cannot change files"):
        AccessPolicy(home=home).writable("~/projects/app/src/main.ts", home)


def test_project_paths_are_redirected_to_the_working_copy(home, tmp_path):
    project = home / "projects" / "app"
    work = home / ".local" / "share" / "lc" / "candidate"
    (work / "src").mkdir(parents=True)
    (work / "src" / "main.ts").write_text("export const copy = 1\n", encoding="utf-8")
    policy = AccessPolicy(home=home, workdir=work, project=project)
    assert policy.writable(str(project / "src" / "new.ts"), work) == work / "src" / "new.ts"
    assert policy.writable("src/new.ts", work) == work / "src" / "new.ts"
    assert policy.readable(str(project / "src" / "main.ts"), work) == work / "src" / "main.ts"
    assert policy.show(work / "src" / "main.ts") == "src/main.ts"


def test_files_missing_from_the_working_copy_are_read_from_the_original(home):
    project = home / "projects" / "app"
    (project / "node_modules" / "react").mkdir(parents=True)
    (project / "node_modules" / "react" / "package.json").write_text("{}", encoding="utf-8")
    work = home / "work"
    work.mkdir()
    policy = AccessPolicy(home=home, workdir=work, project=project)
    path = policy.readable(str(project / "node_modules" / "react" / "package.json"), work)
    assert path == project / "node_modules" / "react" / "package.json"


def test_writes_outside_the_working_copy_are_refused(home):
    work = home / "work"
    work.mkdir()
    policy = AccessPolicy(home=home, workdir=work, project=home / "projects" / "app")
    with pytest.raises(AccessDenied, match="outside the project's working copy"):
        policy.writable("~/projects/other.txt", work)
    with pytest.raises(AccessDenied, match="secret"):
        policy.writable("src/.env", work)


def test_display_forms(home):
    policy = AccessPolicy(home=home)
    assert policy.show(home) == "~"
    assert policy.show(home / "projects" / "app") == "~/projects/app"
