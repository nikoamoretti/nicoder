"""Existing Babel Vite trees should switch to SWC so Bonsai TypeScript compiles."""
from __future__ import annotations

import json
from pathlib import Path

from local_coder.general.coordinator import prepare_workspace
from local_coder.general.toolchain import SWC_PACKAGE, ensure_ts_preview


def test_ensure_ts_preview_replaces_babel_react_plugin(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "name": "app",
            "devDependencies": {"@vitejs/plugin-react": "^5.0.2", "vite": "^7.1.5"},
        }, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "vite.config.ts").write_text(
        'import { defineConfig } from "vite";\n'
        'import react from "@vitejs/plugin-react";\n'
        "export default defineConfig({ plugins: [react()] });\n",
        encoding="utf-8",
    )
    written = ensure_ts_preview(tmp_path)
    assert "package.json" in written
    assert "vite.config.ts" in written
    package = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert SWC_PACKAGE in package["devDependencies"]
    assert "@vitejs/plugin-react" not in package["devDependencies"]
    assert "plugin-react-swc" in (tmp_path / "vite.config.ts").read_text(encoding="utf-8")


def test_ensure_ts_preview_is_noop_when_swc_is_already_set(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "name": "app",
            "devDependencies": {SWC_PACKAGE: "^4.0.1"},
        }, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "vite.config.ts").write_text(
        f'import react from "{SWC_PACKAGE}";\n',
        encoding="utf-8",
    )
    assert ensure_ts_preview(tmp_path) == []


def test_ensure_ts_preview_skips_trees_without_package_json(tmp_path):
    assert ensure_ts_preview(tmp_path) == []


def test_prepare_workspace_switches_candidate_to_swc(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    user = tmp_path / "app"
    user.mkdir()
    (user / "package.json").write_text(
        json.dumps({
            "name": "app",
            "devDependencies": {"@vitejs/plugin-react": "^5.0.2"},
        }, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (user / "vite.config.ts").write_text(
        'import react from "@vitejs/plugin-react";\n',
        encoding="utf-8",
    )
    meta = prepare_workspace(user, "req-swc", allow_starter=False)
    candidate = Path(meta["candidate"])
    package = json.loads((candidate / "package.json").read_text(encoding="utf-8"))
    assert SWC_PACKAGE in package["devDependencies"]
    baseline = json.loads((Path(meta["baseline"]) / "package.json").read_text(encoding="utf-8"))
    assert "@vitejs/plugin-react" in baseline["devDependencies"]
    assert "@vitejs/plugin-react" in json.loads((user / "package.json").read_text(encoding="utf-8"))["devDependencies"]
