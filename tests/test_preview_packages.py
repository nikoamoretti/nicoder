"""Request previews reuse packages (cut preview-fast).

A request's `candidate/` used to run its own `npm install` the first time it was previewed: 12 to 50 s and about
86 MB for every request of a Vite app. Now `start_preview` clones a donor's `node_modules` first (APFS copy-on-write):
the sticky working copy, the question-preview copy, the newest earlier request's candidate, else his own folder. `npm`
and the dev server are stood in for by recorders here (`FakeNpm`: `install` leaves what npm leaves, a folder for every
package named in package.json); the real npm and Vite runs are the probes of the cut. The clones are real `cp -cR`.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from local_coder.app_service import keep_changes
from local_coder.engines.opencode_config import data_root
from local_coder.general import coordinator, packages
from local_coder.general import preview as preview_module
from local_coder.general.coordinator import project_dir, project_id_for, run_general_request
from local_coder.general.packages import CLOSE, EXACT, NONE, adopt_packages
from local_coder.general.preview import start_preview
from local_coder.general.snapshot import index_tree, is_excluded

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="APFS clones (cp -c) are macOS only")

NPM = "/fake/bin/npm"
DEPS = {"react": "19.0.0", "react-dom": "19.0.0"}
DEV = {"vite": "7.1.5", "typescript": "5.9.2"}
APP = "export default function App() {\n  return <h1>Tasks</h1>;\n}\n"


def package_json(deps: dict | None = None, dev: dict | None = None, **extra) -> str:
    data = {"name": "web", "scripts": {"dev": "vite"}, "dependencies": dict(DEPS if deps is None else deps),
            "devDependencies": dict(DEV if dev is None else dev), **extra}
    return json.dumps(data, indent=2) + "\n"


def install_packages(folder: Path, marker: str = "") -> None:
    """What `npm install` leaves in `folder`: a folder for each package package.json names, none that it does not."""
    package = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    wanted = {name for section in ("dependencies", "devDependencies") for name in package.get(section, {})}
    modules = folder / "node_modules"
    modules.mkdir(exist_ok=True)
    for entry in list(modules.iterdir()):
        if not entry.name.startswith(".") and entry.name not in wanted and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
    for name in sorted(wanted):
        (modules / name).mkdir(parents=True, exist_ok=True)
        (modules / name / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8")
        (modules / name / "index.js").write_text(f"module.exports = {name!r};\n", encoding="utf-8")
    (modules / ".bin").mkdir(exist_ok=True)
    if "vite" in wanted and not os.path.lexists(modules / ".bin" / "vite"):
        (modules / ".bin" / "vite").symlink_to("../vite/index.js")
    (modules / ".package-lock.json").write_text("{}\n", encoding="utf-8")
    (modules / "marker.txt").write_text(marker or folder.name, encoding="utf-8")  # who made these packages
    (folder / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")


class FakeNpm:
    """Stands in for `npm install`: records where it ran and whether node_modules was already there."""

    def __init__(self) -> None:
        self.installs: list[dict] = []
        self.next: list[str] = []  # one of "ok", "fail", "timeout" for each install to come; "ok" when empty

    def run(self, argv, *args, **kwargs):
        assert argv[:2] == [NPM, "install"], argv
        cwd = Path(kwargs["cwd"])
        self.installs.append({"dir": cwd, "had_modules": (cwd / "node_modules").is_dir()})
        action = self.next.pop(0) if self.next else "ok"
        if action == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 180)
        if action == "fail":
            (cwd / "node_modules").mkdir(exist_ok=True)
            (cwd / "node_modules" / "half-installed.txt").write_text("npm died here\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 1, "", "npm error ERESOLVE could not resolve")
        install_packages(cwd, marker="installed by npm")
        return subprocess.CompletedProcess(argv, 0, "added packages\n", "")

    def where(self) -> list[Path]:
        return [row["dir"] for row in self.installs]


class FakeProcess:
    pid = 2_000_000  # not a process on this Mac: the preview's liveness checks must not find it

    def poll(self):
        return None


@pytest.fixture
def npm(monkeypatch) -> FakeNpm:
    """`start_preview` with no real npm, dev server or port: only its `node_modules` handling is real."""
    fake = FakeNpm()
    shim = types.SimpleNamespace(
        run=fake.run, Popen=lambda *a, **k: FakeProcess(), TimeoutExpired=subprocess.TimeoutExpired,
        DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT,
    )
    monkeypatch.setattr(preview_module, "subprocess", shim)
    monkeypatch.setattr(preview_module, "_which", lambda name: NPM if name == "npm" else None)
    monkeypatch.setattr(preview_module, "_http_ok", lambda url: True)
    monkeypatch.setattr(preview_module, "_port_open", lambda host, port: False)
    monkeypatch.setattr(preview_module, "_alive", lambda pid: False)
    return fake


@pytest.fixture
def folder(tmp_path) -> Path:
    """His project folder: a Vite-like app with a hidden file."""
    root = Path(os.path.realpath(tmp_path / "projects" / "web"))
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text(package_json(), encoding="utf-8")
    (root / "index.html").write_text('<div id="root"></div>\n', encoding="utf-8")
    (root / "src" / "App.tsx").write_text(APP, encoding="utf-8")
    (root / ".hidden").write_text("keep me\n", encoding="utf-8")
    return root


class Project:
    """The project's data folder: the sticky copy, the question-preview copy and the request candidates."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.id = project_id_for(folder)
        self.root = project_dir(self.id)

    def make(self, path: Path, package: str | None = None, *, installed: bool = False) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "package.json").write_text(package or package_json(), encoding="utf-8")
        (path / "src").mkdir(exist_ok=True)
        (path / "src" / "App.tsx").write_text(APP, encoding="utf-8")
        if installed:
            install_packages(path, marker=path.parent.name if path.name == "candidate" else path.name)
        return path

    def live(self, package: str | None = None, *, installed: bool = False) -> Path:
        return self.make(self.root / "live" / "candidate", package, installed=installed)

    def copy(self, package: str | None = None, *, installed: bool = False) -> Path:
        return self.make(self.root / "preview", package, installed=installed)

    def request(self, request_id: str, created: float, package: str | None = None, *, installed: bool = False) -> Path:
        base = self.root / "requests" / request_id
        candidate = self.make(base / "candidate", package, installed=installed)
        meta = {"request_id": request_id, "project_id": self.id, "user_path": str(self.folder), "created_ts": created,
                "candidate": str(candidate), "baseline": str(base / "baseline")}
        (base / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return candidate


@pytest.fixture
def project(folder) -> Project:
    return Project(folder)


def marker(candidate: Path) -> str:
    return (candidate / "node_modules" / "marker.txt").read_text(encoding="utf-8")


def digest(root: Path) -> dict[str, tuple]:
    """Every entry under root with mode, mtime and bytes, links by their target; "." is the folder's own mtime."""
    out: dict[str, tuple] = {".": ("root", os.lstat(root).st_mtime_ns)}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            full = Path(dirpath) / name
            info = os.lstat(full)
            rel = full.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                out[rel] = ("link", os.readlink(full))
            elif stat.S_ISDIR(info.st_mode):
                out[rel] = ("dir", info.st_mode, info.st_mtime_ns)
            else:
                out[rel] = ("file", info.st_mode, info.st_mtime_ns, full.read_bytes())
    return out


def links_into(root: Path, folder: Path) -> list[Path]:
    """Every link under root that, followed, ends up inside `folder`."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            if full.is_symlink() and str(Path(os.path.realpath(full))).startswith(str(folder)):
                found.append(full)
    return found


# --- Donors: which working copy lends its packages, in which order ------------------------------------------------


def test_donors_are_tried_in_order_sticky_copy_preview_copy_newest_earlier_request_his_folder(project, folder):
    """With all four exact, the first wins; take it away and the next one does."""
    live = project.live(installed=True)
    copy = project.copy(installed=True)
    newer = project.request("r2", 200, installed=True)
    older = project.request("r1", 100, installed=True)
    install_packages(folder)
    for path in (live, copy, newer, older, folder):
        (path / "node_modules" / "marker.txt").write_text(path.parent.name + "/" + path.name, encoding="utf-8")
    target = project.request("r3", 300)
    expected = [live, copy, newer, older, folder]
    for winner in expected:
        adoption = adopt_packages(target, project.id)
        assert adoption.how == EXACT and adoption.donor == winner, winner
        assert marker(target) == winner.parent.name + "/" + winner.name
        shutil.rmtree(winner / "node_modules")
        shutil.rmtree(target / "node_modules")
    assert adopt_packages(target, project.id).how == NONE, "nothing left to clone"


def test_only_earlier_requests_lend_and_none_while_another_request_runs(project):
    live = project.live(installed=True)
    later = project.request("r9", 900, installed=True)
    target = project.request("r2", 200)
    assert adopt_packages(target, project.id).donor == live
    shutil.rmtree(target / "node_modules")
    with coordinator._request_running(project.id, "r-other"):
        adoption = adopt_packages(target, project.id)
    assert adoption.how == NONE, "a later request is no donor, nor is a copy while another request changes it"
    assert later.joinpath("node_modules").is_dir() and not target.joinpath("node_modules").exists()
    with coordinator._request_running(project.id, "r2"):  # the request being previewed: its engine is done
        assert adopt_packages(target, project.id).donor == live


def test_an_exact_match_later_in_the_list_beats_a_close_one_earlier(project):
    live = project.live(package_json(dev={**DEV, "vitest": "3.0.0"}), installed=True)
    earlier = project.request("r1", 100, installed=True)
    target = project.request("r2", 200)
    adoption = adopt_packages(target, project.id)
    assert (adoption.how, adoption.donor) == (EXACT, earlier)
    assert live.joinpath("node_modules").is_dir()


def test_when_none_matches_the_closest_donor_is_cloned_and_npm_finishes_it(project):
    wanted = package_json(deps={**DEPS, "clsx": "2.1.1"})
    project.live(package_json(deps={}, dev={"vite": "7.1.5"}), installed=True)  # 4 differences
    close = project.request("r1", 100, package_json(), installed=True)  # 1 difference
    project.request("r0", 50, package_json(deps={"react": "18.0.0"}), installed=True)  # 2 differences
    target = project.request("r2", 200, wanted)
    adoption = adopt_packages(target, project.id)
    assert (adoption.how, adoption.donor) == (CLOSE, close)
    assert marker(target) == "r1" and not (target / "node_modules" / "clsx").exists(), "npm adds clsx, not the clone"


def test_package_gap_counts_the_dependencies_that_differ():
    one = json.loads(package_json())
    assert packages.package_gap(one, json.loads(package_json())) == 0
    assert packages.package_gap(one, json.loads(package_json(deps={**DEPS, "clsx": "2.1.1"}))) == 1
    older = json.loads(package_json(deps={"react": "18.0.0"}))
    assert packages.package_gap(one, older) == 2, "react-dom is missing and react has another version"
    assert packages.package_gap(one, {"name": "web"}) == len(DEPS) + len(DEV)


def test_a_package_json_formatted_differently_is_still_the_same(project):
    donor = project.request("r1", 100, installed=True)
    other = json.dumps(json.loads(package_json()), indent=4, sort_keys=True) + "\n"
    target = project.request("r2", 200, other)
    assert other != (donor / "package.json").read_text(encoding="utf-8")
    assert adopt_packages(target, project.id).how == EXACT


def test_a_tree_npm_did_not_make_is_cloned_only_when_exact_and_whole(project):
    wanted = package_json(deps={**DEPS, "clsx": "2.1.1"})
    donor = project.request("r1", 100, installed=True)
    (donor / "node_modules" / ".package-lock.json").unlink()  # made by another package manager
    inexact = project.request("r2", 200, wanted)
    assert adopt_packages(inexact, project.id).how == NONE, "npm could not bring it up to date"
    exact = project.request("r3", 300)
    assert adopt_packages(exact, project.id).how == EXACT
    shutil.rmtree(exact / "node_modules")
    shutil.rmtree(donor / "node_modules" / "react-dom")  # a half install with the same package.json
    adoption = adopt_packages(exact, project.id)
    assert adoption.how == NONE, "not npm's and not whole"


def test_an_exact_donor_that_lacks_a_package_is_only_a_head_start(project):
    donor = project.request("r1", 100, installed=True)
    shutil.rmtree(donor / "node_modules" / "react-dom")
    target = project.request("r2", 200)
    adoption = adopt_packages(target, project.id)
    assert adoption.how == CLOSE, "npm install has to finish it"


def test_nothing_is_cloned_without_a_readable_package_json_or_when_packages_are_there(project):
    project.request("r1", 100, installed=True)
    broken = project.request("r2", 200, "{ not json")
    assert adopt_packages(broken, project.id).how == NONE
    target = project.request("r3", 300)
    (target / "node_modules").mkdir()
    (target / "node_modules" / "mine.txt").write_text("x\n", encoding="utf-8")
    assert adopt_packages(target, project.id).how == NONE
    assert sorted(p.name for p in (target / "node_modules").iterdir()) == ["mine.txt"], "left as it was"


def test_a_donor_whose_node_modules_is_a_link_is_not_used(project, tmp_path):
    donor = project.request("r1", 100)
    real = project.make(tmp_path / "elsewhere", installed=True)
    (donor / "node_modules").symlink_to(real / "node_modules")
    target = project.request("r2", 200)
    assert adopt_packages(target, project.id).how == NONE
    assert not (target / "node_modules").exists()


# --- His folder is only read -----------------------------------------------------------------------------------


def test_his_folder_is_only_read_and_nothing_links_back_to_it(project, folder):
    install_packages(folder)
    (folder / "node_modules" / "react" / "lib").mkdir()
    (folder / "node_modules" / "react" / "lib" / "self").symlink_to("../index.js")  # a link that stays inside
    before = digest(folder)
    os.chmod(folder, 0o555)  # nothing may be created in the folder itself
    try:
        target = project.request("r1", 100)
        adoption = adopt_packages(target, project.id)
        assert (adoption.how, adoption.donor) == (EXACT, folder)
        assert digest(folder) == before, "every entry, node_modules included, mtimes and the folder's own"
        assert (target / "node_modules" / "react" / "index.js").stat().st_ino != (
            folder / "node_modules" / "react" / "index.js").stat().st_ino, "a separate file, not a link"
        assert os.readlink(target / "node_modules" / "react" / "lib" / "self") == "../index.js", "links stay links"
        assert links_into(target, folder) == [], "no link from the candidate into his folder"
        (target / "node_modules" / "react" / "index.js").write_text("changed in the candidate\n", encoding="utf-8")
        (target / "node_modules" / "new.txt").write_text("new\n", encoding="utf-8")
        assert digest(folder) == before, "what the candidate writes never reaches the folder"
        assert not [p for p in target.parent.iterdir() if p.name.startswith(".")], "no staging folder is left"
    finally:
        os.chmod(folder, 0o755)


def test_a_clone_with_a_link_to_his_folder_or_out_of_the_candidate_is_refused(project, folder, tmp_path):
    install_packages(folder)
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    for name, where in (("absolute", str(folder / "src")), ("leaves", "../../../../../../../../tmp"),
                        ("absolute-elsewhere", str(outside))):
        (folder / "node_modules" / name).symlink_to(where)
        target = project.request("r-" + name, 100 + len(name))
        adoption = adopt_packages(target, project.id)
        assert adoption.how == NONE, name
        assert not (target / "node_modules").exists() and links_into(target, folder) == [], name
        (folder / "node_modules" / name).unlink()
        shutil.rmtree(target)


def test_links_that_stay_inside_the_candidate_are_fine(project, folder):
    install_packages(folder)
    (folder / "node_modules" / ".bin" / "to-src").symlink_to("../../src")  # the candidate's own src/
    target = project.request("r1", 100)
    assert adopt_packages(target, project.id).how == EXACT
    assert os.readlink(target / "node_modules" / ".bin" / "to-src") == "../../src"


def test_a_donor_that_fails_to_clone_gives_way_to_the_next_one_then_to_a_plain_install(project, monkeypatch):
    first = project.live(installed=True)
    second = project.request("r1", 100, installed=True)
    real = subprocess.run

    def cp_fails_for(blocked: list[Path]):
        def run(argv, *args, **kwargs):
            if argv[:2] == ["/bin/cp", "-cR"] and Path(argv[2]).parent in blocked:
                Path(argv[3]).mkdir(parents=True)  # half a clone, then the copy fails
                Path(argv[3], "half.js").write_text("x\n", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 1, b"", b"clonefile failed")
            return real(argv, *args, **kwargs)
        return run

    target = project.request("r2", 200)
    monkeypatch.setattr(packages.subprocess, "run", cp_fails_for([first]))
    adoption = adopt_packages(target, project.id)
    assert (adoption.how, adoption.donor) == (EXACT, second)
    shutil.rmtree(target / "node_modules")
    monkeypatch.setattr(packages.subprocess, "run", cp_fails_for([first, second]))
    assert adopt_packages(target, project.id).how == NONE
    assert not (target / "node_modules").exists(), "half a clone is never trusted"
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".")], "and no staging folder is left"
    monkeypatch.setattr(packages.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("/bin/cp")))
    assert adopt_packages(target, project.id).how == NONE, "a cp that cannot start falls back too"


# --- start_preview: the second request starts without a second npm install ------------------------------------------


def test_a_second_request_gets_the_first_ones_packages_and_runs_no_npm_install(project, npm):
    first = project.request("r1", 100)
    second = project.request("r2", 200)
    one = start_preview(first, project.id)
    assert one["ok"] is True and one["packages"] == "installed" and npm.where() == [first], "nothing to clone yet"
    donor = digest(first / "node_modules")
    two = start_preview(second, project.id)
    assert two["ok"] is True and two["packages"] == "cloned"
    assert npm.where() == [first], "no npm install for the second request"
    assert marker(second) == "installed by npm" and (second / "node_modules" / "react-dom" / "index.js").is_file()
    assert (second / "node_modules" / "react" / "index.js").stat().st_ino != (
        first / "node_modules" / "react" / "index.js").stat().st_ino
    assert digest(first / "node_modules") == donor, "the donor is exactly as it was"
    log = (data_root() / "logs" / f"preview-{project.id}.log").read_text(encoding="utf-8")
    assert f"packages cloned from {first}" in log and "nothing to install" in log


def test_start_preview_leaves_packages_that_are_already_there_alone(project, npm):
    path = project.request("r1", 100, installed=True)
    before = digest(path / "node_modules")
    record = start_preview(path, project.id)
    assert record["ok"] is True and record["packages"] == "present" and npm.installs == []
    assert digest(path / "node_modules") == before


def test_a_new_dependency_is_installed_in_the_candidate_only(project, npm):
    first = project.request("r1", 100)
    start_preview(first, project.id)
    donor = digest(first / "node_modules")
    second = project.request("r2", 200, package_json(deps={**DEPS, "clsx": "2.1.1"}, dev={"vite": "7.1.5"}))
    record = start_preview(second, project.id)
    assert record["ok"] is True and record["packages"] == "cloned+installed"
    assert npm.installs[-1] == {"dir": second, "had_modules": True}, "npm ran on top of the clone"
    assert (second / "node_modules" / "clsx").is_dir(), "the new package"
    assert not (second / "node_modules" / "typescript").exists(), "the one this package.json dropped"
    assert digest(first / "node_modules") == donor, "the donor got neither"


def test_a_clone_npm_cannot_finish_is_thrown_away_and_installed_from_scratch(project, npm):
    first = project.request("r1", 100)
    start_preview(first, project.id)
    second = project.request("r2", 200, package_json(deps={**DEPS, "clsx": "2.1.1"}))
    npm.next = ["fail", "ok"]
    record = start_preview(second, project.id)
    assert record["ok"] is True and record["packages"] == "installed"
    assert [row["had_modules"] for row in npm.installs[1:]] == [True, False], "the second try had no clone under it"
    assert not (second / "node_modules" / "half-installed.txt").exists()
    assert (second / "node_modules" / "clsx").is_dir()


def test_a_failed_install_leaves_no_packages_behind_for_the_next_start_to_trust(project, npm):
    path = project.request("r1", 100)
    npm.next = ["fail"]
    record = start_preview(path, project.id)
    assert record["ok"] is False and record["code"] == "install_failed" and "ERESOLVE" in record["detail"]
    assert not (path / "node_modules").exists()
    again = start_preview(path, project.id)
    assert again["ok"] is True and npm.where() == [path, path], "the next start installs again"


def test_an_install_that_takes_too_long_is_a_refusal_not_an_exception(project, npm):
    path = project.request("r1", 100)
    npm.next = ["timeout"]
    record = start_preview(path, project.id)
    assert record["ok"] is False and record["code"] == "install_failed" and "longer than 180" in record["detail"]
    assert not (path / "node_modules").exists()


def test_a_donor_that_breaks_the_lookup_never_stops_the_preview(project, npm, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("a bad record")

    monkeypatch.setattr(packages, "donor_folders", boom)
    path = project.request("r1", 100)
    record = start_preview(path, project.id)
    assert record["ok"] is True and record["packages"] == "installed" and npm.where() == [path]


# --- node_modules never reaches a review, Keep or the folder ------------------------------------------------------


def test_node_modules_stays_out_of_snapshots_so_a_clone_never_reaches_keep(project, npm, folder, monkeypatch):
    assert is_excluded("node_modules/react/index.js") and is_excluded("node_modules/.package-lock.json")
    assert is_excluded("package-lock.json")
    monkeypatch.setattr(coordinator, "check_preview_health", lambda *a, **k: {"ok": True, "code": "ok", "detail": "ok"})
    monkeypatch.setattr(coordinator, "run_project_build", lambda root: {"ran": True, "ok": True, "code": "ok"})

    def engine(text: str):
        def write(candidate: Path, _text: str, _cancel) -> None:
            (candidate / "src" / "App.tsx").write_text(APP.replace("Tasks", text), encoding="utf-8")
            (candidate / "src" / f"{text}.tsx").write_text(f"export const {text} = 1;\n", encoding="utf-8")
        return write

    first = run_general_request(folder, "Show Alpha", request_id="r1", fake_engine=engine("Alpha"))
    second = run_general_request(folder, "Show Beta", request_id="r2", fake_engine=engine("Beta"))
    assert first["verification"]["page_mounted"] and second["verification"]["page_mounted"]
    c1, c2 = (data_root() / "projects" / project.id / "requests" / name / "candidate" for name in ("r1", "r2"))
    assert npm.where() == [c1], "the second request installed nothing"
    assert (c1 / "node_modules").is_dir() and (c2 / "node_modules").is_dir()
    for candidate in (c1, c2):
        assert not [rel for rel in index_tree(candidate).files if "node_modules" in rel or rel == "package-lock.json"]
    assert "src/Beta.tsx" in second["facts"]["files"]
    assert not [name for name in second["facts"]["files"] if "node_modules" in name or name == "package-lock.json"]
    keep_changes("r2", folder)
    assert not (folder / "node_modules").exists() and not (folder / "package-lock.json").exists()
    assert sorted(p.name for p in folder.iterdir()) == [".hidden", "index.html", "package.json", "src"]
    assert (folder / "src" / "Beta.tsx").is_file() and (folder / "src" / "Alpha.tsx").is_file()
