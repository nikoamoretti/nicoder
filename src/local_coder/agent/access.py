"""What the agent may read and write on this Mac.

Reads: files anywhere under the home folder, minus secrets and private app
data. Writes: only inside the request's working copy. In a change request the
original project folder is mapped onto that working copy, so the model sees
and edits one consistent tree. Every path is resolved (symlinks followed)
before it is checked, so a link cannot lead a tool around these rules.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

# Home-relative folders that are never read. Library holds keychains, mail,
# messages, browser profiles and app databases; the rest hold credentials.
DENIED_HOME_DIRS = (
    "Library",
    ".Trash",
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".fly",
    ".password-store",
    ".config/gh",
    ".config/gcloud",
    ".config/key",
    ".config/op",
    ".local/share/opencode",
)
# Readable even though it sits inside a denied folder: iCloud Drive documents.
ALLOWED_INSIDE_DENIED = ("Library/Mobile Documents/com~apple~CloudDocs",)
# Folder names refused wherever they appear.
DENIED_DIR_NAMES = frozenset({".ssh", ".gnupg", ".aws"})
# File names refused anywhere in the home folder, matched case-insensitively.
# Narrow on purpose: code such as CredentialsForm.tsx stays readable.
DENIED_NAMES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keychain",
    "*.keychain-db",
    "*.mobileprovision",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials",
    "credentials.*",
    ".credentials*",
    "*-credentials.json",
    "*_credentials.json",
    "service-account*.json",
    "secrets",
    "secrets.*",
    ".secrets*",
    "*.secret",
    "*-secret",
    "*_secret",
    "client_secret*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "*_history",
    ".claude.json",
    "auth.json",
    "keys.md",
)
# Public files that match a pattern above: example env files and CA bundles.
ALLOWED_NAMES = frozenset({
    ".env.example", ".env.sample", ".env.template", "cacert.pem", "ca-bundle.pem", "ca-bundle.crt",
})


class AccessDenied(Exception):
    """A tool asked for a path the agent may not use. The message is shown to the model."""


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def secret_name(name: str) -> bool:
    """True when a file name looks like a credential or key file."""
    lowered = name.lower()
    if lowered in ALLOWED_NAMES:
        return False
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in DENIED_NAMES)


@dataclass(frozen=True)
class AccessPolicy:
    """Read the home folder (minus secrets); write only the working copy.

    `workdir` is None for a read-only conversation. `project` is the user's
    original folder; with a workdir, paths inside it are redirected to the
    working copy. `limit_to` narrows reads to one folder (delegated tasks).
    """

    home: Path
    workdir: Path | None = None
    project: Path | None = None
    limit_to: Path | None = None
    home_real: Path = field(init=False)
    workdir_real: Path | None = field(init=False)
    project_real: Path | None = field(init=False)
    limit_real: Path | None = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "home_real", Path(os.path.realpath(self.home)))
        for name in ("workdir", "project", "limit_to"):
            value = getattr(self, name)
            target = "limit_real" if name == "limit_to" else f"{name}_real"
            object.__setattr__(self, target, Path(os.path.realpath(value)) if value else None)

    @property
    def can_write(self) -> bool:
        """Whether this conversation has a working copy to change."""
        return self.workdir_real is not None

    def expand(self, raw: str, base: Path) -> Path:
        """Absolute, symlink-resolved form of a tool path; relative paths start at `base`."""
        text = str(raw or "").strip()
        if not text:
            raise AccessDenied("No path was given.")
        if text == "~":
            path = self.home_real
        elif text.startswith("~/"):
            path = self.home_real / text[2:]
        else:
            path = Path(text)
            if not path.is_absolute():
                path = base / path
        return Path(os.path.realpath(path))

    def redirect(self, path: Path) -> Path:
        """Map a path in the original project onto the working copy (change requests only)."""
        if self.workdir_real is None or self.project_real is None:
            return path
        if _under(path, self.workdir_real) or not _under(path, self.project_real):
            return path
        return self.workdir_real / path.relative_to(self.project_real)

    def denied_reason(self, path: Path) -> str | None:
        """Why a resolved path is off limits, or None when it may be read."""
        inside_home = _under(path, self.home_real)
        inside_work = self.workdir_real is not None and _under(path, self.workdir_real)
        if not inside_home and not inside_work:
            return f"{self.show(path)} is outside the home folder, so it is off limits."
        if self.limit_real is not None and not inside_work and not _under(path, self.limit_real):
            return f"{self.show(path)} is outside {self.show(self.limit_real)}, the folder this task is limited to."
        if inside_home:
            rel = path.relative_to(self.home_real).as_posix()
            allowed = any(rel == item or rel.startswith(item + "/") for item in ALLOWED_INSIDE_DENIED)
            for denied in DENIED_HOME_DIRS:
                if not allowed and (rel == denied or rel.startswith(denied + "/")):
                    return f"{self.show(path)} is private (~/{denied} holds app data or credentials)."
        for part in path.parts:
            if part in DENIED_DIR_NAMES:
                return f"{self.show(path)} is private ({part} holds credentials)."
        if secret_name(path.name):
            return f"{self.show(path)} looks like a secret, so it is never read or written."
        return None

    def readable(self, raw: str, base: Path) -> Path:
        """Resolve a path the model wants to read or list. Raises AccessDenied."""
        expanded = self.expand(raw, base)
        path = self.redirect(expanded)
        if path != expanded and not path.exists() and expanded.exists():
            # Not part of the working copy (for example node_modules): read the original.
            path = expanded
        reason = self.denied_reason(path)
        if reason:
            raise AccessDenied(reason)
        return path

    def writable(self, raw: str, base: Path) -> Path:
        """Resolve a path the model wants to create or change. Raises AccessDenied."""
        if self.workdir_real is None:
            raise AccessDenied(
                "This conversation cannot change files. Describe the change; the user can ask "
                "Nicoder to make it in the project."
            )
        path = self.redirect(self.expand(raw, base))
        if not _under(path, self.workdir_real) or path == self.workdir_real:
            raise AccessDenied(
                f"{self.show(path)} is outside the project's working copy. Only files of the "
                "current project can change."
            )
        reason = self.denied_reason(path)
        if reason:
            raise AccessDenied(reason)
        return path

    def show(self, path: Path) -> str:
        """Short display form: project-relative inside the working copy, ~/… in the home folder."""
        if self.workdir_real is not None and _under(path, self.workdir_real):
            rel = path.relative_to(self.workdir_real).as_posix()
            return rel if rel != "." else "the project"
        if _under(path, self.home_real):
            rel = path.relative_to(self.home_real).as_posix()
            return "~" if rel == "." else f"~/{rel}"
        return str(path)
