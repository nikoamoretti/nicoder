"""A question that asks for the preview writes nothing into the project's folder (cut question-preview-writes).

"Open the app" used to run `npm install` and the dev server in the project's own folder: `node_modules` and a rewritten
`package-lock.json` landed there before any Keep. Now the preview runs from the result that waits for Keep or Discard
(that request's own `candidate/`) or from a copy of the folder under Nicoder's data folder. `start_preview` is
replaced by a recorder here (it stands in for npm: it "installs" when the folder it is given has no `node_modules`);
the real npm and Vite runs are the probes of the cut. The numbered tests are the cut's checks 1 to 6.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import types
from pathlib import Path

import httpx
import pytest

from local_coder import machine
from local_coder.app_rpc import RpcServer
from local_coder.app_service import chat_request, explain_request, keep_changes
from local_coder.engines.opencode_config import data_root
from local_coder.general import packages, preview_copy
from local_coder.general import preview as preview_module
from local_coder.general.coordinator import live_dir, load_meta, project_id_for, run_general_request
from local_coder.general.preview_copy import preview_project

APP = "export default function App() {\n  return <h1>New app</h1>;\n}\n"
PACKAGE = '{\n  "name": "app",\n  "scripts": {"dev": "vite"},\n  "dependencies": {"react": "19.0.0"}\n}\n'
WAITING_SENTENCE = (
    "A web preview of the change that is waiting for Keep or Discard (not in the folder yet) is running at"
)
COPY_SENTENCE = "A web preview of a copy of the project, made just now, is running at"
NO_PACKAGE = "There is no package.json, so a web preview could not be started."
URL = "http://127.0.0.1:4173/"
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")


class FakePreview:
    """Stands in for `preview.start_preview`: records where it was asked to run and "installs" like npm does."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []
        self.installed: list[Path] = []
        self.seen: list[dict[str, bool]] = []

    def __call__(self, project_dir, project_id, wait_s=45):
        folder = Path(project_dir)
        self.calls.append((folder, project_id))
        self.seen.append({"had_node_modules": (folder / "node_modules").is_dir()})
        if not (folder / "node_modules").is_dir():  # preview.py: npm install only when node_modules is missing
            (folder / "node_modules" / ".bin").mkdir(parents=True)
            (folder / "node_modules" / "installed-by-npm.txt").write_text("npm install", encoding="utf-8")
            (folder / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
            self.installed.append(folder)
        return {"ok": True, "started": True, "url": URL, "port": 4173, "dir": str(folder), "project_id": project_id}


@pytest.fixture
def fake(monkeypatch) -> FakePreview:
    recorder = FakePreview()
    monkeypatch.setattr(preview_module, "start_preview", recorder)
    monkeypatch.setattr(preview_module, "read_preview", lambda: None)
    return recorder


class Model:
    """The stubbed local model (the fake llama.cpp reply): remembers what each question arrived as, note included."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    @property
    def notes(self) -> list[str]:
        return [str(payload["messages"][-1]["content"]) for payload in self.payloads]


@pytest.fixture
def model(monkeypatch) -> Model:
    """Only app_service's own httpx is replaced, so the real `_local_chat_completion` and its project brief run."""
    stub = Model()

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "Okay."}}]}

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None):
            stub.payloads.append(json)
            return Response()

    monkeypatch.setattr("local_coder.app_service.ensure_server", lambda wait_s=90: {"ok": True})
    fake_httpx = types.SimpleNamespace(Client=Client, HTTPError=httpx.HTTPError)
    monkeypatch.setattr("local_coder.app_service.httpx", fake_httpx)
    return stub


@pytest.fixture
def folder(tmp_path) -> Path:
    """A Vite-like project folder with a hidden file in it."""
    root = Path(os.path.realpath(tmp_path / "projects" / "web"))
    (root / "src").mkdir(parents=True)
    (root / "public").mkdir()
    (root / "package.json").write_text(PACKAGE, encoding="utf-8")
    (root / "index.html").write_text('<div id="root"></div>\n', encoding="utf-8")
    (root / "src" / "App.tsx").write_text(APP, encoding="utf-8")
    (root / "public" / "logo.txt").write_text("logo\n", encoding="utf-8")
    (root / ".hidden").write_text("keep me\n", encoding="utf-8")
    return root


def digest(root: Path) -> dict[str, tuple]:
    """Every entry under root, hidden ones included, with mode, mtime and bytes; "." is the folder's own mtime."""
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


def copy_of(folder: Path) -> Path:
    return data_root() / "projects" / project_id_for(folder) / "preview"


def waiting_result(folder: Path, request_id: str = "r1") -> dict:
    """A fake-engine request leaves a result waiting: the heading changed and Tasks.tsx added."""

    def engine(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "src" / "App.tsx").write_text(APP.replace("New app", "Tasks"), encoding="utf-8")
        (candidate / "src" / "Tasks.tsx").write_text("export const tasks = ['milk'];\n", encoding="utf-8")

    return run_general_request(
        folder, "Show Tasks", request_id=request_id, fake_engine=engine, start_preview_after=False,
    )


# --- Check 1: the folder stays untouched -----------------------------------------------------------------------


def test_1_the_preview_runs_from_a_copy_and_the_folder_stays_byte_identical(folder, fake, model):
    before = digest(folder)
    first = chat_request(folder, "open the app")
    second = chat_request(folder, "show me the app")
    assert digest(folder) == before, "every entry, hidden ones and the folder's own mtime"
    assert not any((folder / name).exists() for name in ("node_modules", "package-lock.json", "dist", "preview"))
    for result in (first, second):
        assert result["preview"]["url"] == URL and result["preview"]["ok"] is True
        assert result["preview"]["source"] == "copy"
        assert Path(result["preview"]["dir"]) == copy_of(folder)
    assert [call[0] for call in fake.calls] == [copy_of(folder)] * 2, "start_preview never saw the folder"
    assert fake.calls[0][1] == project_id_for(folder)
    copy = copy_of(folder)
    assert (copy / "src" / "App.tsx").read_text(encoding="utf-8") == APP
    assert (copy / "index.html").is_file() and (copy / "public" / "logo.txt").is_file() and (copy / ".hidden").is_file()
    assert (copy / "node_modules" / "installed-by-npm.txt").is_file(), "npm installed in the copy"
    assert all(COPY_SENTENCE in note and URL in note for note in model.notes)


def test_1_the_copy_leaves_out_what_a_snapshot_leaves_out(folder, fake, model):
    (folder / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (folder / "package-lock.json").write_text('{"lockfileVersion": 1}\n', encoding="utf-8")
    (folder / "dist").mkdir()
    (folder / "dist" / "bundle.js").write_text("x\n", encoding="utf-8")
    (folder / "src" / "link.ts").symlink_to(folder / "src" / "App.tsx")
    chat_request(folder, "open the app")
    copy = copy_of(folder)
    for name in (".env", "dist", "src/link.ts"):
        assert not (copy / name).exists(), name
    assert (copy / "package-lock.json").read_text(encoding="utf-8") == '{"lockfileVersion": 3}\n', "its own, from npm"
    assert (folder / "package-lock.json").read_text(encoding="utf-8") == '{"lockfileVersion": 1}\n', "his, untouched"


def test_1_node_modules_in_the_folder_are_cloned_so_nothing_is_installed(folder, fake, model):
    (folder / "node_modules" / "left-pad").mkdir(parents=True)
    (folder / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    (folder / "node_modules" / ".bin").mkdir()
    (folder / "node_modules" / ".bin" / "vite").symlink_to("../left-pad/index.js")
    before = digest(folder)
    chat_request(folder, "open the app")
    assert digest(folder) == before, "node_modules included"
    assert fake.seen == [{"had_node_modules": True}] and fake.installed == [], "the preview started without an install"
    clone = copy_of(folder) / "node_modules"
    assert (clone / "left-pad" / "index.js").read_text(encoding="utf-8") == "module.exports = 1;\n"
    assert os.readlink(clone / ".bin" / "vite") == "../left-pad/index.js", "links stay links"
    cloned, original = clone / "left-pad" / "index.js", folder / "node_modules" / "left-pad" / "index.js"
    assert cloned.stat().st_ino != original.stat().st_ino, "a separate copy, not a link"
    (clone / "left-pad" / "index.js").write_text("changed in the copy\n", encoding="utf-8")
    assert digest(folder) == before, "what the dev server writes into the copy never reaches his folder"
    assert not [p for p in clone.parent.parent.iterdir() if "modules" in p.name], "no staging folder is left"


def test_1_a_failed_clone_falls_back_to_npm_install_in_the_copy(folder, fake, model, monkeypatch):
    (folder / "node_modules" / "pkg").mkdir(parents=True)
    (folder / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")
    real_run = subprocess.run

    def broken_cp(argv, *args, **kwargs):
        if argv[:2] == ["/bin/cp", "-cR"]:
            Path(argv[3]).mkdir(parents=True)  # half a clone, then the copy fails
            (Path(argv[3]) / "half.js").write_text("x\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 1, b"", b"clonefile failed")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(packages.subprocess, "run", broken_cp)
    before = digest(folder)
    result = chat_request(folder, "open the app")
    assert result["preview"]["ok"] is True
    assert fake.seen == [{"had_node_modules": False}], "no node_modules, so start_preview installs"
    assert fake.installed == [copy_of(folder)], "npm install, in the copy"
    assert not (copy_of(folder) / "node_modules" / "half.js").exists(), "half a clone is never trusted"
    assert digest(folder) == before


def test_1_a_clone_that_cannot_start_falls_back_too(folder, fake, model, monkeypatch):
    (folder / "node_modules").mkdir()

    def no_cp(argv, *args, **kwargs):
        raise FileNotFoundError("/bin/cp")

    monkeypatch.setattr(packages.subprocess, "run", no_cp)
    chat_request(folder, "open the app")
    assert fake.installed == [copy_of(folder)]


def test_1_packages_installed_for_another_package_json_are_not_cloned(folder, fake):
    """Packages installed for another package.json would not match; and a linked node_modules is not cloned."""
    (folder / "node_modules" / "pkg").mkdir(parents=True)
    other = folder.parent / "other"
    other.mkdir()
    (other / "package.json").write_text('{"name": "other"}\n', encoding="utf-8")
    preview_copy._clone_modules(folder, other)
    assert not (other / "node_modules").exists()
    shared = folder.parent / "shared-modules"
    (folder / "node_modules").rename(shared)
    (folder / "node_modules").symlink_to(shared)
    target = folder.parent / "target"
    target.mkdir()
    (target / "package.json").write_text(PACKAGE, encoding="utf-8")
    preview_copy._clone_modules(folder, target)
    assert not (target / "node_modules").exists(), "a linked node_modules would let the dev server write into it"


# --- Check 2: the copy follows the folder when asked again -----------------------------------------------------


def test_2_asked_again_the_copy_shows_his_edit_and_installs_his_new_dependency(folder, fake, model, monkeypatch):
    stopped: list[bool] = []
    running = {"dir": str(copy_of(folder))}
    monkeypatch.setattr(preview_module, "read_preview", lambda: running)
    monkeypatch.setattr(preview_module, "stop_preview", lambda: stopped.append(True) or {"stopped": True})
    chat_request(folder, "open the app")
    copy = copy_of(folder)
    # his edit: the same node_modules stay, the file follows
    (folder / "src" / "App.tsx").write_text(APP.replace("New app", "Edited"), encoding="utf-8")
    (folder / "src" / "Extra.tsx").write_text("export const extra = 1;\n", encoding="utf-8")
    (copy / "stale.ts").write_text("export const stale = 1;\n", encoding="utf-8")  # not in the folder any more
    chat_request(folder, "open the app")
    assert "Edited" in (copy / "src" / "App.tsx").read_text(encoding="utf-8")
    assert (copy / "src" / "Extra.tsx").is_file() and not (copy / "stale.ts").exists()
    assert fake.installed == [copy], "no second install for an edit"
    assert stopped == [], "the dev server keeps running for an edit"
    # his new dependency: the packages go, the running server stops, and the preview installs again
    (copy / "node_modules" / "marker.txt").write_text("old packages\n", encoding="utf-8")
    (folder / "package.json").write_text(PACKAGE.replace('"react": "19.0.0"', '"react": "19.0.0", "ms": "2.1.3"'),
                                         encoding="utf-8")
    his_edits = digest(folder)
    chat_request(folder, "open the app")
    assert stopped == [True], "a server that stays up would never see the packages installed again"
    assert fake.installed == [copy, copy] and not (copy / "node_modules" / "marker.txt").exists()
    assert '"ms": "2.1.3"' in (copy / "package.json").read_text(encoding="utf-8")
    assert digest(folder) == his_edits, "the folder holds only his own edits: no node_modules, no lock file"


def test_2_a_dependency_with_his_node_modules_present_is_cloned_not_installed(folder, fake, model, monkeypatch):
    monkeypatch.setattr(preview_module, "stop_preview", lambda: {"stopped": True})
    chat_request(folder, "open the app")
    assert fake.installed == [copy_of(folder)]
    (folder / "package.json").write_text(PACKAGE.replace('"react": "19.0.0"', '"react": "19.0.0", "ms": "2.1.3"'),
                                         encoding="utf-8")
    (folder / "node_modules" / "ms").mkdir(parents=True)  # he ran npm install himself
    (folder / "node_modules" / "ms" / "index.js").write_text("ms\n", encoding="utf-8")
    chat_request(folder, "open the app")
    assert fake.installed == [copy_of(folder)], "the second preview needed no install"
    assert (copy_of(folder) / "node_modules" / "ms" / "index.js").is_file()
    assert not (copy_of(folder) / "node_modules" / "installed-by-npm.txt").exists(), "the old packages went"


def test_2_a_package_json_that_cannot_be_dropped_stops_the_preview_with_a_sentence(folder, fake, model, monkeypatch):
    chat_request(folder, "open the app")
    (folder / "package.json").write_text(PACKAGE.replace("19.0.0", "19.1.0"), encoding="utf-8")
    def refuse(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(preview_copy, "_remove", refuse)
    result = preview_project(folder)
    assert result["ok"] is False and result["code"] == "copy_failed"
    assert result["detail"] == "The preview copy could not be made: Permission denied."
    assert len(fake.calls) == 1, "no preview of a half-made copy"


# --- Check 3: a result waits ---------------------------------------------------------------------------------


def test_3_with_a_result_waiting_the_preview_is_that_result_and_keep_writes_only_its_files(folder, fake, model):
    before = digest(folder)
    review = waiting_result(folder)
    assert digest(folder) == before
    candidate = Path(load_meta(review["project_id"], "r1")["candidate"])
    result = chat_request(folder, "open the app")
    assert result["preview"]["source"] == "waiting" and Path(result["preview"]["dir"]) == candidate
    assert fake.calls == [(candidate, project_id_for(folder))]
    assert "Tasks" in (candidate / "src" / "App.tsx").read_text(encoding="utf-8")
    assert WAITING_SENTENCE in model.notes[0] and URL in model.notes[0]
    assert "not in the folder yet" in model.notes[0]
    assert digest(folder) == before, "the folder is identical until Keep"
    assert not copy_of(folder).exists(), "no copy is made while a result waits"
    kept = keep_changes("r1", folder)
    assert kept["ok"] is True and sorted(kept["applied"]) == ["src/App.tsx", "src/Tasks.tsx"]
    assert (folder / "src" / "Tasks.tsx").is_file()
    assert "Tasks" in (folder / "src" / "App.tsx").read_text(encoding="utf-8")
    assert not any((folder / name).exists() for name in ("node_modules", "package-lock.json", "dist", "preview"))
    after = digest(folder)
    assert set(after) - set(before) == {"src/Tasks.tsx"}, "Keep wrote the result's files and nothing else"
    # the candidate got node_modules and a lock file from its preview; neither is part of the result
    assert (candidate / "node_modules").is_dir() and (candidate / "package-lock.json").is_file()


def test_3_after_keep_the_question_previews_a_copy_of_the_folder_again(folder, fake, model):
    waiting_result(folder)
    first = chat_request(folder, "open the app")
    keep_changes("r1", folder)
    second = chat_request(folder, "open the app")
    assert first["preview"]["source"] == "waiting" and second["preview"]["source"] == "copy"
    assert Path(second["preview"]["dir"]) == copy_of(folder)
    kept_app = (copy_of(folder) / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "Tasks" in kept_app, "the kept result is the folder now"
    assert COPY_SENTENCE in model.notes[1]


def test_3_the_waiting_result_gets_the_folders_node_modules_when_its_package_json_is_the_same(folder, fake, model):
    (folder / "node_modules" / "pkg").mkdir(parents=True)
    (folder / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")
    review = waiting_result(folder)
    candidate = Path(load_meta(review["project_id"], "r1")["candidate"])
    before = digest(folder)
    chat_request(folder, "open the app")
    assert fake.seen == [{"had_node_modules": True}] and fake.installed == []
    assert (candidate / "node_modules" / "pkg" / "index.js").is_file()
    assert digest(folder) == before


def test_3_a_result_that_changed_package_json_installs_in_its_own_candidate(folder, fake, model):
    (folder / "node_modules" / "pkg").mkdir(parents=True)

    def engine(candidate: Path, _text: str, _cancel) -> None:
        added = PACKAGE.replace('"react": "19.0.0"', '"react": "19.0.0", "ms": "2.1.3"')
        (candidate / "package.json").write_text(added, encoding="utf-8")

    review = run_general_request(folder, "Add ms", request_id="r1", fake_engine=engine, start_preview_after=False)
    candidate = Path(load_meta(review["project_id"], "r1")["candidate"])
    chat_request(folder, "open the app")
    assert fake.installed == [candidate], "the folder's packages were installed for another package.json"


def test_3_a_waiting_result_without_a_package_json_previews_a_copy_of_the_folder(folder, fake, model):
    def engine(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "package.json").unlink()

    run_general_request(folder, "Drop it", request_id="r1", fake_engine=engine, start_preview_after=False)
    result = chat_request(folder, "open the app")
    assert result["preview"]["source"] == "copy" and COPY_SENTENCE in model.notes[0]


def test_3_a_bad_record_falls_back_to_the_copy_instead_of_failing_the_question(folder, fake, model, monkeypatch):
    def broken(_folder):
        raise ValueError("unreadable record")

    monkeypatch.setattr(preview_copy, "find_waiting_result", broken)
    assert chat_request(folder, "open the app")["preview"]["source"] == "copy"


def test_3_the_agent_is_told_the_same(folder, fake, tmp_path, monkeypatch):
    from local_coder.agent import runner

    sent: list[list[dict]] = []

    def hosted(cfg, body, *, thinking, **_):
        sent.append(json.loads(json.dumps(body["messages"])))  # the loop appends to this list afterwards
        return {"choices": [{"message": {"role": "assistant", "content": "Preview is up."}, "finish_reason": "stop"}]}

    machine.write_machine_config(machine.MachineConfig(
        model_alias="nvidia/nemotron-3.5-super-vl-120b-a12b", endpoint="https://integrate.api.nvidia.com",
        provider="nvidia", api_key_name="NVIDIA_API_KEY", n_ctx=131072, max_tokens=16384, quantization="hosted",
    ))
    monkeypatch.setattr(runner, "hosted_chat", hosted)
    monkeypatch.setenv("HOME", str(tmp_path))
    waiting_result(folder)
    result = chat_request(folder, "open the app")
    first = str(sent[0][-1]["content"])
    assert WAITING_SENTENCE in first and "not in the folder yet" in first
    assert result["preview"]["source"] == "waiting" and result["summary"] == "Preview is up."


# --- Check 4: stack, History and Keep are not moved by the question ------------------------------------------


def test_4_the_question_leaves_the_stack_history_and_keep_alone(folder, fake, model):
    def engine(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "src" / "More.tsx").write_text("export const more = 1;\n", encoding="utf-8")

    review = waiting_result(folder)
    pid = review["project_id"]
    rpc = RpcServer()
    history_before = rpc.dispatch({"id": "1", "method": "history", "params": {"path": str(folder)}})["result"]
    live_before = digest(live_dir(pid))
    chat_request(folder, "open the app")
    history_after = rpc.dispatch({"id": "2", "method": "history", "params": {"path": str(folder)}})["result"]
    assert history_after == history_before, "History lists the same requests as before the question"
    assert digest(live_dir(pid)) == live_before, "live/candidate is byte-identical across the question"
    follow_up = run_general_request(folder, "More", request_id="r2", fake_engine=engine, start_preview_after=False)
    assert load_meta(pid, "r2")["built_on"] == "r1", "the follow-up stacks on the waiting request"
    assert follow_up["facts"]["files"] == ["src/App.tsx", "src/More.tsx", "src/Tasks.tsx"]
    listed = rpc.dispatch({"id": "3", "method": "history", "params": {"path": str(folder)}})["result"]["episodes"]
    assert sorted(row.get("request_id") or row.get("episode_id") for row in listed) == ["r1", "r2"]
    kept = keep_changes("r2", folder)
    assert kept["ok"] is True and {"src/App.tsx", "src/More.tsx", "src/Tasks.tsx"} <= set(kept["applied"])
    assert not (folder / "node_modules").exists() and not (folder / "package-lock.json").exists()


# --- Check 5: RPC preview_start ------------------------------------------------------------------------------


def _rpc(method: str, **params) -> dict:
    reply = RpcServer().dispatch({"id": "1", "method": method, "params": params})
    return reply["result"] if reply.get("ok") else {"ERROR": reply.get("error")}


def test_5_rpc_preview_start_runs_any_project_from_a_copy_and_keeps_its_return_shape(folder, tmp_path, fake):
    never_opened = tmp_path / "projects" / "never-opened"
    (never_opened / "src").mkdir(parents=True)
    (never_opened / "package.json").write_text(PACKAGE.replace('"app"', '"other"'), encoding="utf-8")
    (never_opened / "src" / "App.tsx").write_text(APP, encoding="utf-8")
    for project in (folder, never_opened):
        before = digest(project)
        reply = _rpc("preview_start", path=str(project))
        assert reply["ok"] is True and reply["url"] == URL
        assert Path(reply["dir"]) == copy_of(project) and reply["source"] == "copy"
        assert digest(project) == before
        assert (copy_of(project) / "node_modules").is_dir()
    assert all(call[0] not in (folder, never_opened) for call in fake.calls)


def test_5_rpc_preview_start_refuses_what_is_not_a_project_and_creates_nothing(folder, tmp_path, fake):
    review = waiting_result(folder)
    candidate = Path(load_meta(review["project_id"], "r1")["candidate"])
    assert (candidate / "package.json").is_file()
    copy = copy_of(folder)
    copy.mkdir(parents=True)
    (copy / "package.json").write_text(PACKAGE, encoding="utf-8")
    missing = tmp_path / "projects" / "gone"
    data_before = digest(data_root())
    refused = {
        "missing": _rpc("preview_start", path=str(missing)),
        "a file": _rpc("preview_start", path=str(folder / "package.json")),
        "empty": _rpc("preview_start", path=""),
        "a waiting copy": _rpc("preview_start", path=str(candidate)),
        "the preview copy": _rpc("preview_start", path=str(copy)),
        "inside the data folder": _rpc("preview_start", path=str(data_root() / "projects")),
        "the data folder": _rpc("preview_start", path=str(data_root())),
    }
    for name, reply in refused.items():
        assert reply["ok"] is False and reply["code"], name
        detail = reply["detail"]
        assert isinstance(detail, str) and "\n" not in detail and 8 < len(detail) < 200, (name, detail)
        assert not reply.get("url"), name
    for name in ("a waiting copy", "the preview copy", "inside the data folder", "the data folder"):
        assert refused[name]["code"] == "inside_data_root", name
    assert refused["missing"]["code"] == "missing" and refused["a file"]["code"] == "not_a_folder"
    assert fake.calls == [], "start_preview, and so npm, never ran for a refused path"
    assert digest(data_root()) == data_before and not missing.exists(), "nothing was created"


def test_5_rpc_preview_start_ignores_a_caller_chosen_project_id(folder, fake):
    reply = _rpc("preview_start", path=str(folder), project_id="../../escape")
    assert Path(reply["dir"]) == copy_of(folder) and fake.calls[0][1] == project_id_for(folder)
    assert not (data_root().parent / "escape").exists()


# --- Check 6: read-only everywhere else ------------------------------------------------------------------------


def test_6_other_questions_and_explain_leave_the_folder_alone(folder, fake, model, monkeypatch):
    from local_coder.agent import runner

    monkeypatch.setenv("HOME", str(folder.parent.parent))
    before = digest(folder)
    chat_request(folder, "what does this app do?")
    assert model.notes == ["what does this app do?"], "no preview note for an ordinary question"
    assert explain_request(folder, "explain how App works")["kind"] == "explain"
    machine.write_machine_config(machine.MachineConfig(provider="nvidia", api_key_name="NVIDIA_API_KEY"))
    replies = iter([
        {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{
            "id": "1", "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"path": "src/App.tsx"})},
        }]}, "finish_reason": "tool_calls"}]},
        {"choices": [{"message": {"role": "assistant", "content": "It shows a heading."}, "finish_reason": "stop"}]},
    ])
    monkeypatch.setattr(runner, "hosted_chat", lambda cfg, body, *, thinking, **_: next(replies))
    assert chat_request(folder, "what does this app do?")["summary"] == "It shows a heading."
    assert digest(folder) == before
    assert fake.calls == [] and not copy_of(folder).exists()


def test_6_a_folder_without_package_json_gets_the_old_sentence_and_no_copy(tmp_path, fake, model):
    notes = tmp_path / "projects" / "notes"
    notes.mkdir(parents=True)
    (notes / "README.md").write_text("# notes\n", encoding="utf-8")
    before = digest(notes)
    result = chat_request(notes, "open the app")
    assert "preview" not in result and NO_PACKAGE in model.notes[0]
    assert digest(notes) == before and fake.calls == []
    assert not (data_root() / "projects" / project_id_for(notes)).exists(), "not even a project folder in the data dir"
    assert preview_project(notes) == {
        "ok": False, "code": "no_package", "detail": "This project has no package.json yet.",
    }


def test_6_a_failed_start_keeps_its_sentence_and_a_busy_port_is_not_called_running(folder, model, monkeypatch):
    def busy(project_dir, project_id, wait_s=45):
        return {
            "ok": False, "code": "port_busy", "url": "http://127.0.0.1:4321/",
            "detail": "Port 4321 is already in use, so the preview was not started.",
        }

    monkeypatch.setattr(preview_module, "start_preview", busy)
    monkeypatch.setattr(preview_module, "read_preview", lambda: None)
    result = chat_request(folder, "open the app")
    assert "(The preview did not start: Port 4321 is already in use, so the preview was not started.)" in model.notes[0]
    assert "is running at" not in model.notes[0]
    assert result["preview"]["code"] == "port_busy"


def test_6_a_question_in_a_git_project_leaves_the_git_folder_alone(folder, fake, model):
    """The project brief ran `git status` there, which refreshed .git/index even for a plain question."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CEILING_DIRECTORIES": str(folder.parent)}

    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=folder, env=env,
                       check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "start")
    app = folder / "src" / "App.tsx"
    os.utime(app, (1_000_000_000, 1_000_000_000))  # the index is now stale: a plain `git status` rewrites it
    before = digest(folder)
    chat_request(folder, "what does this app do?")
    chat_request(folder, "open the app")
    assert digest(folder) == before, ".git included"
    git("status", "--porcelain")  # the control: git itself would have refreshed the index
    if digest(folder) == before:
        pytest.skip("this git does not refresh a stale index here, so the test proves nothing")


@darwin_only
def test_6_the_question_agents_bash_cannot_touch_the_folder(folder, tmp_path, monkeypatch):
    """The real question agent's bash tool (its own sandbox-exec): `touch <folder>/x` is denied."""
    from local_coder.agent import runner

    home = Path(os.path.realpath(tmp_path))
    monkeypatch.setenv("HOME", str(home))
    machine.write_machine_config(machine.MachineConfig(provider="nvidia", api_key_name="NVIDIA_API_KEY"))
    outputs: list[str] = []
    replies = iter([
        {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{
            "id": "1", "type": "function", "function": {"name": "bash", "arguments": json.dumps(
                {"command": f"touch '{folder}/x'; echo done $?"})},
        }]}, "finish_reason": "tool_calls"}]},
        {"choices": [{"message": {"role": "assistant", "content": "Could not write."}, "finish_reason": "stop"}]},
    ])

    def hosted(cfg, body, *, thinking, **_):
        outputs.extend(str(m.get("content")) for m in body["messages"] if m.get("role") == "tool")
        return next(replies)

    monkeypatch.setattr(runner, "hosted_chat", hosted)
    before = digest(folder)
    chat_request(folder, "check that you can write here")
    assert re.search(r"Operation not permitted|Read-only", "\n".join(outputs)), outputs
    assert not (folder / "x").exists() and digest(folder) == before


def test_6_the_preview_sentences_name_what_is_shown(folder, fake):
    from local_coder.app_service import _start_preview_note

    preview, note = _start_preview_note(folder)
    assert preview is not None and note == f"{COPY_SENTENCE} {URL}."
    waiting_result(folder)
    preview, note = _start_preview_note(folder)
    assert note == f"{WAITING_SENTENCE} {URL}."
