"""The checks give a web candidate its packages first, so the project's own build can judge it.

Before, the build was skipped ("node_modules is not installed yet"), only the quick scan judged the code, and
its false alarms kept the preview of Nico's French app from ever starting (2026-10-25).
"""
from __future__ import annotations

from local_coder.general import coordinator, preview


def _candidate(tmp_path, *, package: bool = True, modules: bool = False):
    folder = tmp_path / "candidate"
    folder.mkdir(parents=True)
    if package:
        (folder / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    if modules:
        (folder / "node_modules").mkdir()
    return folder


def _spy(monkeypatch):
    calls = []
    monkeypatch.setattr(preview, "_which", lambda name: "/usr/bin/true")

    def provide(npm, folder, project_id, log):
        calls.append((folder, project_id))
        (folder / "node_modules").mkdir()
        return preview.PACKAGES_CLONED, None

    monkeypatch.setattr(preview, "_provide_packages", provide)
    return calls


def test_a_candidate_without_packages_gets_them(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    folder = _candidate(tmp_path)
    coordinator._ensure_packages({"project_id": "p1"}, folder)
    assert calls == [(folder, "p1")]
    assert (folder / "node_modules").is_dir()


def test_nothing_happens_with_packages_or_without_a_package_json(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    coordinator._ensure_packages({"project_id": "p1"}, _candidate(tmp_path / "a", modules=True))
    coordinator._ensure_packages({"project_id": "p1"}, _candidate(tmp_path / "b", package=False))
    assert calls == []


def test_a_failing_install_never_fails_the_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "_which", lambda name: "/usr/bin/true")

    def broken(*args):
        raise RuntimeError("network down")

    monkeypatch.setattr(preview, "_provide_packages", broken)
    coordinator._ensure_packages({"project_id": "p1"}, _candidate(tmp_path))  # no exception
