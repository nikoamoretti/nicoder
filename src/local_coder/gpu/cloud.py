"""Set up and drive the Local Coder GPU on Google Cloud from the Mac (gcloud must be signed in).

The pieces, all in one project and region:
- a bucket holding the model library (models.json plus one folder per model) and
  this package's GPU code under .local-coder/, so no container image is built;
- an Artifact Registry remote repository that caches Docker Hub images;
- a Cloud Run service running vLLM's image with the swapper, on one GPU, scaled
  to zero when idle (billed per second only while an instance is up);
- a Cloud Run job on CPU that copies Hugging Face models into the bucket.
Settings (no secrets) live in ~/.config/local-coder/gpu.json.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from local_coder.machine import config_dir

from .manifest import MANIFEST_NAME, dumps, empty, find, model_id_for, parse, remove

VLLM_IMAGE = "vllm/vllm-openai:v0.32.0"
FETCH_IMAGE = "library/python:3.12-slim"
REMOTE_REPO = "dockerhub"
DEFAULTS: dict[str, Any] = {
    "project": "faceblur-private-20260901",
    "region": "us-central1",
    "service": "local-coder-gpu",
    "fetch_job": "local-coder-fetch",
    "service_account_name": "local-coder-gpu",
    "gpu": "nvidia-rtx-pro-6000",
    "cpu": 20,
    "memory_gib": 80,
}
# Cloud Run us-central1 list prices without zonal redundancy (the same rates Veil's spending report uses).
GPU_RATE = {"nvidia-rtx-pro-6000": 0.00036522, "nvidia-l4": 0.0001867}
CPU_RATE = 0.000018
MEMORY_RATE = 0.000002
CODE_PREFIX = ".local-coder"
FETCH_SCRIPT = (
    "#!/bin/sh\nset -e\n"
    "pip install --quiet --no-cache-dir --disable-pip-version-check huggingface_hub google-cloud-storage requests\n"
    f'export PYTHONPATH=/models/{CODE_PREFIX}/src\n'
    'exec python -m local_coder.gpu.fetch "$@"\n'
)


class GpuError(RuntimeError):
    """A gcloud step failed or the GPU is not set up. The message is for the user."""


def config_file() -> Path:
    """Where the GPU settings live."""
    return config_dir() / "gpu.json"


def library_cache() -> Path:
    """Last-seen copy of the bucket's models.json, so the model picker never starts the GPU."""
    return config_dir() / "gpu-models.json"


def load_config() -> dict[str, Any] | None:
    """The saved GPU settings, or None before setup."""
    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("url") else None


def save_config(data: dict[str, Any]) -> Path:
    """Write the GPU settings."""
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def hourly_cost(gpu: str, cpu: int, memory_gib: int) -> float:
    """USD per hour while an instance is up."""
    return round((GPU_RATE.get(gpu, 0.0) + cpu * CPU_RATE + memory_gib * MEMORY_RATE) * 3600, 2)


def gcloud(args: list[str], *, timeout: float = 900, check: bool = True, stdin: str | None = None) -> str:
    """Run gcloud and return stdout. Raises GpuError with gcloud's own message."""
    binary = shutil.which("gcloud") or "/opt/homebrew/bin/gcloud"
    try:
        proc = subprocess.run([binary, *args, "--quiet"], capture_output=True, text=True, timeout=timeout,
                              input=stdin)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GpuError(f"gcloud {' '.join(args[:3])} failed: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise GpuError(f"gcloud {' '.join(args[:3])} failed: {' '.join(detail)}")
    return proc.stdout


def bucket_name(project_number: str, region: str) -> str:
    """The model bucket's name."""
    return f"local-coder-models-{project_number}-{region}"


def upload_code(bucket: str, root: Path | None = None) -> int:
    """Copy this package's GPU code into the bucket (the container imports it from the mount)."""
    package = root or Path(__file__).resolve().parents[1]
    staging = config_dir() / "gpu-code"
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "src" / "local_coder" / "gpu").mkdir(parents=True)
    shutil.copy2(package / "__init__.py", staging / "src" / "local_coder" / "__init__.py")
    count = 1
    for source in sorted((package / "gpu").glob("*.py")):
        shutil.copy2(source, staging / "src" / "local_coder" / "gpu" / source.name)
        count += 1
    (staging / "fetch.sh").write_text(FETCH_SCRIPT, encoding="utf-8")
    gcloud(["storage", "rsync", "--recursive", "--delete-unmatched-destination-objects", str(staging),
            f"gs://{bucket}/{CODE_PREFIX}"], timeout=300)
    shutil.rmtree(staging, ignore_errors=True)
    return count


def setup(**overrides: Any) -> dict[str, Any]:
    """Create or update every cloud piece; safe to run again. Returns the saved settings."""
    cfg = {**DEFAULTS, **(load_config() or {}), **{k: v for k, v in overrides.items() if v is not None}}
    project, region = str(cfg["project"]), str(cfg["region"])
    scope = ["--project", project]
    number = gcloud(["projects", "describe", project, "--format=value(projectNumber)"]).strip()
    bucket = bucket_name(number, region)
    account = f"{cfg['service_account_name']}@{project}.iam.gserviceaccount.com"

    if not gcloud(["storage", "buckets", "list", f"--filter=name:{bucket}", "--format=value(name)", *scope]).strip():
        gcloud(["storage", "buckets", "create", f"gs://{bucket}", f"--location={region}",
                "--uniform-bucket-level-access", "--public-access-prevention", *scope])
    if not gcloud(["storage", "ls", f"gs://{bucket}/{MANIFEST_NAME}"], check=False).strip():
        gcloud(["storage", "cp", "-", f"gs://{bucket}/{MANIFEST_NAME}"], stdin=dumps(empty()))
    repos = gcloud(["artifacts", "repositories", "list", f"--location={region}", "--format=value(name.basename())",
                    *scope])
    if REMOTE_REPO not in repos.split():
        gcloud(["artifacts", "repositories", "create", REMOTE_REPO, "--repository-format=docker",
                f"--location={region}", "--mode=remote-repository", "--remote-repo-config-desc=Docker Hub",
                "--remote-docker-repo=DOCKER-HUB", *scope])
    accounts = gcloud(["iam", "service-accounts", "list", "--format=value(email)", *scope])
    if account not in accounts.split():
        gcloud(["iam", "service-accounts", "create", str(cfg["service_account_name"]),
                "--display-name=Local Coder GPU", *scope])
        time.sleep(10)
    gcloud(["storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}", f"--member=serviceAccount:{account}",
            "--role=roles/storage.objectAdmin"])
    upload_code(bucket)

    registry = f"{region}-docker.pkg.dev/{project}/{REMOTE_REPO}"
    gpu = str(cfg["gpu"])
    cpu, memory = int(cfg["cpu"]), int(cfg["memory_gib"])
    volume = f"name=models,type=cloud-storage,bucket={bucket}"
    gcloud([
        "run", "deploy", str(cfg["service"]), f"--image={registry}/{VLLM_IMAGE}", f"--region={region}", *scope,
        "--command=python3", "--args=-m,local_coder.gpu.swapper,--models-root,/models",
        f"--set-env-vars=PYTHONPATH=/models/{CODE_PREFIX}/src,PYTHONDONTWRITEBYTECODE=1,HF_HUB_OFFLINE=1,"
        "VLLM_NO_USAGE_STATS=1,HF_HOME=/tmp/hf",
        "--gpu=1", f"--gpu-type={gpu}", "--no-gpu-zonal-redundancy", f"--cpu={cpu}", f"--memory={memory}Gi",
        "--no-cpu-throttling", "--min-instances=0", "--max-instances=1", "--concurrency=16", "--timeout=3600",
        "--port=8080", "--no-allow-unauthenticated", f"--service-account={account}", "--execution-environment=gen2",
        f"--add-volume={volume},readonly=true", "--add-volume-mount=volume=models,mount-path=/models",
    ], timeout=1800)
    url = gcloud(["run", "services", "describe", str(cfg["service"]), f"--region={region}", *scope,
                  "--format=value(status.url)"]).strip()
    gcloud([
        "run", "jobs", "deploy", str(cfg["fetch_job"]), f"--image={registry}/{FETCH_IMAGE}", f"--region={region}",
        *scope, "--command=sh", f"--args=/models/{CODE_PREFIX}/fetch.sh", "--cpu=4", "--memory=8Gi",
        "--task-timeout=3h", "--max-retries=0", f"--service-account={account}",
        f"--add-volume={volume}", "--add-volume-mount=volume=models,mount-path=/models",
    ], timeout=900)
    saved = {**cfg, "bucket": bucket, "url": url, "project_number": number,
             "hourly_usd": hourly_cost(gpu, cpu, memory)}
    save_config(saved)
    refresh_library(saved)
    return saved


def require() -> dict[str, Any]:
    """The settings, or a plain error before setup."""
    cfg = load_config()
    if cfg is None:
        raise GpuError("The GPU is not set up yet. Run: local-coder gpu setup")
    return cfg


def refresh_library(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read models.json from the bucket and cache it for the model picker."""
    cfg = cfg or require()
    text = gcloud(["storage", "cat", f"gs://{cfg['bucket']}/{MANIFEST_NAME}"], timeout=60)
    library = parse(text)
    library_cache().write_text(dumps(library), encoding="utf-8")
    return library


def cached_library() -> dict[str, Any]:
    """The last-seen library (no network)."""
    try:
        return parse(library_cache().read_text(encoding="utf-8"))
    except OSError:
        return empty()


def add_model(repo: str, *, model_id: str | None = None, name: str | None = None, preset: str | None = None,
              include: list[str] | None = None) -> dict[str, Any]:
    """Copy a Hugging Face model into the library (runs the fetch job and waits)."""
    cfg = require()
    upload_code(str(cfg["bucket"]))
    args = [f"/models/{CODE_PREFIX}/fetch.sh", "--repo", repo, "--bucket", str(cfg["bucket"]),
            "--id", model_id or model_id_for(repo)]
    if name:
        args += ["--name", name]
    if preset:
        args += ["--preset", preset]
    for pattern in include or []:
        args += ["--include", pattern]
    if any("," in item for item in args):
        raise GpuError("Names and patterns cannot contain commas.")
    gcloud(["run", "jobs", "execute", str(cfg["fetch_job"]), f"--region={cfg['region']}",
            f"--project={cfg['project']}", f"--args={','.join(args)}", "--wait"], timeout=3 * 3600 + 300)
    library = refresh_library(cfg)
    entry = find(library, model_id or model_id_for(repo))
    if entry is None:
        raise GpuError(f"The job finished but {repo} is not in the library; see the job's logs.")
    return entry


def remove_model(model_id: str) -> dict[str, Any]:
    """Delete a model's files from the bucket and drop it from the library."""
    cfg = require()
    current = parse(gcloud(["storage", "cat", f"gs://{cfg['bucket']}/{MANIFEST_NAME}"], timeout=60))
    library, gone = remove(current, model_id)
    if gone is None:
        raise GpuError(f"{model_id} is not in the library.")
    gcloud(["storage", "cp", "-", f"gs://{cfg['bucket']}/{MANIFEST_NAME}"], stdin=dumps(library))
    gcloud(["storage", "rm", "--recursive", f"gs://{cfg['bucket']}/models/{gone['dir']}"], timeout=600)
    library_cache().write_text(dumps(library), encoding="utf-8")
    return gone


def spend(days: int = 7) -> dict[str, Any]:
    """Estimated cost from Cloud Run's billable instance time over the last `days` days."""
    cfg = require()
    end = time.time()
    start = end - days * 86400
    token = gcloud(["auth", "print-access-token"], timeout=60).strip()
    import httpx

    query = {
        "filter": ('metric.type="run.googleapis.com/container/billable_instance_time" '
                   f'AND resource.labels.service_name="{cfg["service"]}"'),
        "interval.startTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
        "interval.endTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)),
        "aggregation.alignmentPeriod": f"{days * 86400}s",
        "aggregation.perSeriesAligner": "ALIGN_SUM",
        "aggregation.crossSeriesReducer": "REDUCE_SUM",
    }
    response = httpx.get(f"https://monitoring.googleapis.com/v3/projects/{cfg['project']}/timeSeries",
                         params=query, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if response.status_code != 200:
        raise GpuError(f"Cloud Monitoring answered HTTP {response.status_code}.")
    seconds = 0.0
    for series in response.json().get("timeSeries", []):
        for point in series.get("points", []):
            value = point.get("value", {})
            seconds += float(value.get("doubleValue") or value.get("int64Value") or 0)
    hourly = float(cfg.get("hourly_usd") or hourly_cost(str(cfg["gpu"]), int(cfg["cpu"]), int(cfg["memory_gib"])))
    return {"days": days, "gpu_hours": round(seconds / 3600, 2), "estimated_usd": round(seconds / 3600 * hourly, 2),
            "hourly_usd": hourly}
