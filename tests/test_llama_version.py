"""`llama-server --version` on a cold Mac (cut core-fixes, problem 6).

Homebrew's llama-server took 18 s on its first call. The version used to be read in the adapter's
constructor with a 15 s timeout that was not caught, so every adapter test died on a cold Mac. Now the
version is "unknown" when the binary does not answer in time, and building an adapter never runs it.
"""
from __future__ import annotations

import stat
import time
from pathlib import Path

import pytest

from local_coder import telemetry
from local_coder.inference.llama_cpp import LlamaCppAdapter
from local_coder.telemetry import llama_server_version


def _stand_in(folder: Path, body: str) -> Path:
    """A fake `llama-server` first on PATH."""
    folder.mkdir(exist_ok=True)
    script = folder / "llama-server"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def slow_binary(tmp_path, monkeypatch) -> Path:
    """A llama-server whose --version takes 20 s, as a cold Homebrew start can."""
    script = _stand_in(tmp_path / "bin", "exec sleep 20")
    monkeypatch.setenv("PATH", f"{script.parent}:/usr/bin:/bin")
    return script


def test_a_timeout_is_unknown_and_never_raises(slow_binary):
    started = time.monotonic()
    assert llama_server_version(str(slow_binary), timeout_s=0.4) == "unknown"
    assert time.monotonic() - started < 5


def test_the_default_wait_is_short(slow_binary):
    """The default must not hold a legacy episode for 15 s on a cold start."""
    started = time.monotonic()
    assert llama_server_version(str(slow_binary)) == "unknown"
    assert time.monotonic() - started < 8


def test_no_binary_is_unknown(tmp_path):
    assert llama_server_version(str(tmp_path / "not-installed")) == "unknown"


def test_a_failing_binary_is_unknown(tmp_path):
    script = _stand_in(tmp_path / "bin", "echo broken >&2; exit 3")
    assert llama_server_version(str(script)) == "unknown"


def test_the_version_is_reported_when_the_binary_answers(tmp_path):
    script = _stand_in(tmp_path / "bin", "echo 'version: 6000 (abc1234)'")
    assert llama_server_version(str(script)) == "version: 6000 (abc1234)"


def test_building_an_adapter_never_runs_the_binary(slow_binary):
    started = time.monotonic()
    adapter = LlamaCppAdapter("http://127.0.0.1:9", model="mock-model")
    assert time.monotonic() - started < 1.0, "the constructor waited for llama-server"
    assert adapter.endpoint == "http://127.0.0.1:9"


def test_building_an_adapter_does_not_call_the_version_helper(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the adapter ran llama-server --version")

    monkeypatch.setattr("local_coder.inference.llama_cpp.llama_server_version", refuse)
    LlamaCppAdapter("http://127.0.0.1:9", model="mock-model")


def test_a_record_says_unknown_when_the_binary_hangs(monkeypatch):
    calls: list[list[str]] = []

    def hang(argv, **_kwargs):
        calls.append(argv)
        raise telemetry.subprocess.TimeoutExpired(argv, 15)

    monkeypatch.setattr(telemetry.subprocess, "run", hang)
    adapter = LlamaCppAdapter("http://127.0.0.1:9", model="mock-model")
    assert calls == []
    assert adapter.provenance()["local_binary_version"] == "unknown"
    assert adapter.provenance()["local_binary_version"] == "unknown"
    assert len(calls) == 1, "asked once per adapter"


def test_a_record_carries_the_version_when_the_binary_answers(tmp_path, monkeypatch):
    script = _stand_in(tmp_path / "bin", "echo 'version: 6000 (abc1234)'")
    monkeypatch.setenv("PATH", f"{script.parent}:/usr/bin:/bin")
    adapter = LlamaCppAdapter("http://127.0.0.1:9", model="mock-model")
    assert adapter.provenance()["local_binary_version"] == "version: 6000 (abc1234)"
