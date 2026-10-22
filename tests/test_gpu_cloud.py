"""GPU library format, Google Cloud setup commands, the model picker, and GPU sign-in and warm-up."""
from __future__ import annotations

import json

import pytest

from local_coder import hosted, machine
from local_coder.gpu import cloud, manifest


def test_entries_get_family_settings():
    qwen = manifest.make_entry("Qwen/Qwen3.8-27B-FP8")
    assert (qwen["id"], qwen["dir"], qwen["preset"]) == ("qwen3.8-27b-fp8", "qwen3.8-27b-fp8", "qwen3.8")
    assert "--tool-call-parser" in qwen["args"] and "qwen3_coder" in qwen["args"]
    assert any("mtp" in arg for arg in qwen["args"]) and not any("mtp" in arg for arg in qwen["fallback_args"])
    assert (qwen["temperature"], qwen["top_p"]) == (1.0, 0.95)
    assert manifest.preset_for("nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4") == "nemotron3.5"
    assert manifest.preset_for("mistralai/Devstral-Small") == "generic"
    with pytest.raises(ValueError, match="unknown preset"):
        manifest.make_entry("x/y", preset="nope")


def test_library_upsert_remove_and_bad_files():
    library = manifest.upsert(manifest.empty(), manifest.make_entry("Qwen/Qwen3.8-27B-FP8"))
    library = manifest.upsert(library, manifest.make_entry("Qwen/Qwen3.8-27B-FP8", name="Renamed"))
    assert [row["name"] for row in library["models"]] == ["Renamed"]
    kept, gone = manifest.remove(library, "qwen3.8-27b-fp8")
    assert gone is not None and kept["models"] == []
    assert manifest.parse("not json") == manifest.empty()
    assert manifest.parse('{"models": [{"id": "x"}]}')["models"] == []


def test_hourly_cost_matches_veils_rates():
    assert cloud.hourly_cost("nvidia-rtx-pro-6000", 20, 80) == 3.19
    assert cloud.hourly_cost("nvidia-l4", 4, 16) == 1.05


class FakeGcloud:
    """Records gcloud calls and answers the read-only ones."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.stdin: list[str | None] = []

    def __call__(self, args, *, timeout=900, check=True, stdin=None):
        self.calls.append(list(args))
        self.stdin.append(stdin)
        joined = " ".join(args)
        if joined.startswith("projects describe"):
            return "123456\n"
        if joined.startswith("run services describe"):
            return "https://local-coder-gpu-abc-uc.a.run.app\n"
        if joined.startswith("storage cat"):
            return manifest.dumps(manifest.upsert(manifest.empty(), manifest.make_entry("Qwen/Qwen3.8-27B-FP8")))
        return ""

    def find(self, *words: str) -> list[str]:
        return next(call for call in self.calls if all(word in call for word in words))


def test_setup_creates_a_private_scale_to_zero_gpu(monkeypatch):
    fake = FakeGcloud()
    monkeypatch.setattr(cloud, "gcloud", fake)
    saved = cloud.setup()
    deploy = fake.find("run", "deploy")
    assert "--gpu-type=nvidia-rtx-pro-6000" in deploy and "--no-gpu-zonal-redundancy" in deploy
    assert "--min-instances=0" in deploy and "--max-instances=1" in deploy
    assert "--no-allow-unauthenticated" in deploy
    assert "--image=us-central1-docker.pkg.dev/faceblur-private-20260901/dockerhub/vllm/vllm-openai:v0.32.0" in deploy
    assert "--add-volume=name=models,type=cloud-storage,bucket=local-coder-models-123456-us-central1,readonly=true" \
        in deploy
    job = fake.find("run", "jobs", "deploy")
    assert "--args=/models/.local-coder/fetch.sh" in job and "--cpu=4" in job
    assert fake.find("storage", "rsync")[-1] == "gs://local-coder-models-123456-us-central1/.local-coder"
    assert saved["url"] == "https://local-coder-gpu-abc-uc.a.run.app" and saved["hourly_usd"] == 3.19
    assert cloud.load_config()["bucket"] == "local-coder-models-123456-us-central1"
    assert [row["id"] for row in cloud.cached_library()["models"]] == ["qwen3.8-27b-fp8"]


def test_add_and_remove_models(monkeypatch):
    fake = FakeGcloud()
    monkeypatch.setattr(cloud, "gcloud", fake)
    cloud.setup()
    entry = cloud.add_model("Qwen/Qwen3.8-27B-FP8")
    assert entry["id"] == "qwen3.8-27b-fp8"
    execute = fake.find("run", "jobs", "execute")
    assert "--wait" in execute
    assert any(arg.startswith("--args=/models/.local-coder/fetch.sh,--repo,Qwen/Qwen3.8-27B-FP8,--bucket,") for arg
               in execute)
    with pytest.raises(cloud.GpuError, match="commas"):
        cloud.add_model("Qwen/x", name="a,b")
    gone = cloud.remove_model("qwen3.8-27b-fp8")
    assert gone["dir"] == "qwen3.8-27b-fp8"
    assert fake.find("storage", "rm")[-1] == "gs://local-coder-models-123456-us-central1/models/qwen3.8-27b-fp8"
    assert json.loads(fake.stdin[-2])["models"] == []


def test_gpu_models_appear_in_the_picker_and_can_be_selected(monkeypatch):
    from local_coder import app_service
    from local_coder.catalog import list_models

    fake = FakeGcloud()
    monkeypatch.setattr(cloud, "gcloud", fake)
    assert not [row for row in list_models(machine.MachineConfig())["models"] if row["id"].startswith("gpu:")]
    cloud.setup()
    row = next(item for item in list_models(machine.MachineConfig())["models"] if item["id"].startswith("gpu:"))
    assert row["id"] == "gpu:qwen3.8-27b-fp8" and row["available"] is True
    assert "$3.19/hour" in row["note"]
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: {"ok": True})
    result = app_service.select_model("gpu:qwen3.8-27b-fp8")
    cfg = machine.load_machine_config()
    assert result["catalog"]["selected"] == "gpu:qwen3.8-27b-fp8"
    assert (cfg.provider, cfg.endpoint, cfg.model_alias) == (
        "cloudrun", "https://local-coder-gpu-abc-uc.a.run.app", "qwen3.8-27b-fp8")
    assert (cfg.temperature, cfg.top_p, cfg.n_ctx) == (1.0, 0.95, 131072)


def test_gpu_sign_in_uses_a_cached_identity_token(monkeypatch):
    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "id-token-value\n"

    monkeypatch.setattr(hosted, "_token_cache", {})
    monkeypatch.setattr(hosted.subprocess, "run", lambda args, **kwargs: calls.append(args) or Done())
    cfg = machine.MachineConfig(provider="cloudrun", endpoint="https://gpu.example", model_alias="m")
    assert hosted.credential(cfg) == ("id-token-value", "gcloud identity token")
    assert hosted.credential(cfg)[0] == "id-token-value"
    assert len(calls) == 1 and calls[0][1:3] == ["auth", "print-identity-token"]
    rows = {name: (ok, detail) for name, ok, detail in machine.machine_status(cfg)}
    assert rows["gcloud sign-in"] == (True, "gcloud identity token")
    assert "your Google Cloud GPU" in rows["model"][1]


def test_warm_up_follows_load_progress(monkeypatch):
    lines = [
        'data: {"status": "loading", "model": "m", "seconds": 0}',
        'data: {"status": "loading", "model": "m", "seconds": 5}',
        'data: {"status": "ready", "model": "m"}',
    ]
    sent: dict = {}

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self):
            yield from lines

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json=None, headers=None):
            sent.update(method=method, url=url, json=json, auth=headers["Authorization"])
            return Response()

    monkeypatch.setattr(hosted, "credential", lambda cfg: ("tok", "gcloud identity token"))
    monkeypatch.setattr(hosted.httpx, "Client", Client)
    seen: list[float] = []
    cfg = machine.MachineConfig(provider="cloudrun", endpoint="https://gpu.example/", model_alias="m")
    took = hosted.warm_up(cfg, seen.append)
    assert took >= 0 and len(seen) == 2
    assert sent == {"method": "POST", "url": "https://gpu.example/lc/load", "json": {"model": "m"},
                    "auth": "Bearer tok"}
    lines[-1] = 'data: {"status": "failed", "model": "m", "detail": "out of memory"}'
    with pytest.raises(hosted.HostedError, match="out of memory"):
        hosted.warm_up(cfg)
    assert hosted.warm_up(machine.MachineConfig()) == 0.0
