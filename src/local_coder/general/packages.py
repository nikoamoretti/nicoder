"""Packages for a preview come from a clone of another working copy's `node_modules`, not a fresh `npm install`.

`start_preview` used to run `npm install` in every request's `candidate/` the first time it was previewed: seconds on a
warm npm cache, tens of seconds when the network is slow, and 60+ MB each for a Vite app, though the project's other
working copies hold the same packages. `adopt_packages` clones one of them into the candidate first (APFS
copy-on-write, `cp -cR`: about 0.1 s, and no disk until a file changes).

Donors, best first: the sticky working copy (`live/candidate`), the question-preview copy (`preview/`), the project's
earlier request candidates (newest first) and the project's own folder. A donor whose `package.json` is the
candidate's is `EXACT`: the clone is the install. Otherwise the donor whose dependencies differ least is `CLOSE`, and
the `npm install` that follows brings it up to date inside the candidate. No donor, or a clone that cannot be made or
trusted, is `NONE`: today's plain install.

A donor is only read. The clone is made beside the candidate and moved in whole, and it is refused when any link in it
is absolute or leads out of the candidate, so nothing in a candidate points back at a donor or at his folder.
`node_modules` is left out of every snapshot (`snapshot.EXCLUDE`), so none of it reaches a review, Keep or a later copy.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODULES = "node_modules"
CLONE_SECONDS = 120
EARLIER_REQUESTS = 8  # how many earlier request candidates are looked at; older ones are not worth a longer search
EXACT = "exact"
CLOSE = "close"
NONE = "none"
# What npm leaves in `node_modules` when an install finishes, and reads again to bring the packages up to date.
HIDDEN_LOCK = ".package-lock.json"
# The sections of package.json that name the packages npm installs.
DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
# A package.json is "complete" in a folder when these are all there; an optional one may be missing on this machine.
REQUIRED_SECTIONS = ("dependencies", "devDependencies")


@dataclass(frozen=True)
class Adoption:
    """What `adopt_packages` did: how the packages came, the donor they were cloned from and how long it took."""

    how: str
    donor: Path | None = None
    seconds: float = 0.0

    def describe(self) -> str:
        if self.donor is None:
            return "no donor, so npm installs the packages"
        note = "same package.json, nothing to install" if self.how == EXACT else "npm install brings them up to date"
        return f"packages cloned from {self.donor} in {self.seconds:.2f} s ({note})"


def adopt_packages(target: Path, project_id: str) -> Adoption:
    """Clone the best donor's `node_modules` into `target`, which has none. Never raises for a donor that fails.

    `EXACT` means the clone holds every package `target/package.json` names: nothing is left to install. `CLOSE`
    means it is a head start that `npm install` must finish. `NONE` means `target` has no `node_modules` from here.
    """
    started = time.monotonic()
    wanted = read_package(target)
    if wanted is None or os.path.lexists(target / MODULES):
        return Adoption(NONE)
    for donor, whole in _ranked(wanted, donor_folders(project_id, target)):
        if clone_modules(donor / MODULES, target):
            how = EXACT if whole and _complete(target / MODULES, wanted) else CLOSE
            return Adoption(how, donor, time.monotonic() - started)
    return Adoption(NONE, None, time.monotonic() - started)


def read_package(folder: Path) -> dict[str, Any] | None:
    """The folder's package.json as a dict; None when it is missing, unreadable or not an object."""
    try:
        data = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def package_gap(donor: dict[str, Any], wanted: dict[str, Any]) -> int:
    """How many packages the two package.json files disagree about: named by one only, or with another version."""
    have, need = _dependencies(donor), _dependencies(wanted)
    return sum(1 for name in set(have) | set(need) if have.get(name) != need.get(name))


def _dependencies(package: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for section in DEPENDENCY_SECTIONS:
        entries = package.get(section)
        if isinstance(entries, dict):
            found.update({str(name): str(spec) for name, spec in entries.items()})
    return found


def _ranked(wanted: dict[str, Any], donors: list[Path]) -> list[tuple[Path, bool]]:
    """The usable donors, each with whether its packages are all the candidate needs: those first, then the closest.

    A donor is `whole` when its package.json is the candidate's and every package it names is there. A tree npm
    made (it left its hidden lock file) can be brought up to date by `npm install`, so it is usable whatever its
    state; any other can only be used as it is.
    """
    rows: list[tuple[bool, int, int, Path]] = []
    for order, donor in enumerate(donors):
        package = read_package(donor)
        if package is None:
            continue
        whole = package == wanted and _complete(donor / MODULES, wanted)
        if not whole and not (donor / MODULES / HIDDEN_LOCK).is_file():
            continue
        rows.append((not whole, package_gap(package, wanted), order, donor))
    rows.sort(key=lambda row: row[:3])
    return [(donor, not partial) for partial, _gap, _order, donor in rows]


def _complete(modules: Path, wanted: dict[str, Any]) -> bool:
    """True when every package the candidate's package.json requires has a folder in `modules`."""
    for section in REQUIRED_SECTIONS:
        entries = wanted.get(section)
        if isinstance(entries, dict) and not all((modules / str(name) / "package.json").is_file() for name in entries):
            return False
    return True


def donor_folders(project_id: str, target: Path) -> list[Path]:
    """The project's folders that hold a real `node_modules` and can lend it to `target`, best first. Reads only.

    The sticky copy goes first while no other request is running in the project (its packages could be half way
    through being installed), then the question-preview copy, the newest earlier requests, and his own folder: the one
    the requests' records name, when it is the folder this project id was made from.
    """
    # Imported here: coordinator imports preview, which imports this module.
    from local_coder.general.coordinator import _project_metas, _requests_in_flight, project_dir, project_id_for
    from local_coder.general.preview_copy import preview_copy_dir

    project = project_dir(project_id)
    metas = _project_metas(project_id)
    own = _request_of(project, target)
    busy = _requests_in_flight(project_id) - {own or ""}
    cutoff = (_created(metas[own]) or None) if own is not None and own in metas else None
    earlier = sorted(
        ((_created(meta), name) for name, meta in metas.items() if name != own and name not in busy
         and (cutoff is None or _created(meta) < cutoff)),
        reverse=True,
    )[:EARLIER_REQUESTS]
    folders = [] if busy else [project / "live" / "candidate"]
    folders.append(preview_copy_dir(project_id))
    folders.extend(project / "requests" / name / "candidate" for _created_ts, name in earlier)
    for meta in sorted(metas.values(), key=_created, reverse=True):
        text = meta.get("user_path")
        if isinstance(text, str) and text and project_id_for(Path(text)) == project_id:
            folders.append(Path(text))
            break
    return _real_donors(folders, target)


def _real_donors(folders: list[Path], target: Path) -> list[Path]:
    """Each folder once, without the target, and only those with a `node_modules` that is a real folder, not a link."""
    skip = {_resolved(target)}
    donors: list[Path] = []
    for folder in folders:
        key = _resolved(folder)
        modules = folder / MODULES
        if key in skip or modules.is_symlink() or not modules.is_dir():
            continue  # a linked node_modules would let the dev server write into whatever it points at
        skip.add(key)
        donors.append(folder)
    return donors


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _request_of(project: Path, target: Path) -> str | None:
    """The id of the request whose `candidate/` is `target`, else None (the preview copy, say)."""
    if target.name == "candidate" and _resolved(target.parent.parent) == _resolved(project / "requests"):
        return target.parent.name
    return None


def _created(meta: dict[str, Any]) -> float:
    try:
        return float(meta.get("created_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def clone_modules(source: Path, target: Path) -> bool:
    """Give `target` a copy-on-write clone of `source`, a `node_modules` folder; True when it is there whole and safe.

    Only when `target` has none yet and `source` is a real folder (not a link: the dev server and Vite's cache write
    into `target`'s own). The clone is made beside `target` and moved in only when whole and when no link in it is
    absolute or leads out of `target`, so a clone that fails, is cut short or points back at the donor never leaves
    a `node_modules` that `start_preview` would trust. `source` is only read.
    """
    destination = target / MODULES
    if os.path.lexists(destination) or source.is_symlink() or not source.is_dir():
        return False
    staging = target.parent / f".{target.name}-modules"
    shutil.rmtree(staging, ignore_errors=True)
    done = False
    try:
        copied = subprocess.run(
            ["/bin/cp", "-cR", str(source), str(staging)], capture_output=True, timeout=CLONE_SECONDS, check=False,
        )
        if copied.returncode == 0 and _links_stay_inside(staging, destination, target):
            os.rename(staging, destination)
            done = True
    except (OSError, subprocess.SubprocessError):
        pass
    shutil.rmtree(staging, ignore_errors=True)
    return done


def _links_stay_inside(staging: Path, destination: Path, root: Path) -> bool:
    """False when a link in the clone is absolute or, read from where the clone will live, leaves `root`."""
    inside = os.path.normpath(root)
    try:
        for link in _links(staging):
            where = os.readlink(link)
            if os.path.isabs(where):
                return False
            home = os.path.join(destination, os.path.relpath(os.path.dirname(link), staging))
            resolved = os.path.normpath(os.path.join(home, where))
            if resolved != inside and not resolved.startswith(inside + os.sep):
                return False
    except OSError:
        return False
    return True


def _links(root: Path) -> Iterator[str]:
    """Every symbolic link under `root`, none followed."""
    pending = [str(root)]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_symlink():
                    yield entry.path
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
