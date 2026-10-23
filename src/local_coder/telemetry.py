"""Best-effort memory telemetry with honest unknown fields.

Measures the actual workload where observable: controller process RSS,
local inference-server RSS (when a server process exists), worker image
size, and host memory totals. Anything unmeasurable stays None (rendered
as "unknown"), never zero. On Apple unified memory, summed RSS values are
labeled as process footprints, not as unique system/GPU totals.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def controller_rss_mb() -> float | None:
    """Current process RSS in MiB, or None when unmeasurable."""
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024, 1)
    except Exception:
        return None


def process_rss_mb(match: str) -> float | None:
    """Summed RSS of processes whose command line contains `match`.

    Used for a locally running inference server (e.g. "llama-server").
    Returns None when no such process exists or it cannot be observed.
    """
    try:
        out = subprocess.run(["pgrep", "-f", match], capture_output=True, text=True, timeout=10)
    except (OSError, ValueError):
        return None
    pids = [line.strip() for line in out.stdout.splitlines() if line.strip().isdigit()]
    if not pids:
        return None
    try:
        shown = subprocess.run(["ps", "-o", "rss=", "-p", ",".join(pids)],
                               capture_output=True, text=True, timeout=10)
    except (OSError, ValueError):
        return None
    if shown.returncode != 0:
        return None
    total_kb = sum(int(field) for field in shown.stdout.split() if field.isdigit())
    return round(total_kb / 1024, 1) if total_kb else None


def host_memory_mb() -> dict[str, float | None]:
    """Host totals on macOS (total + available); unknown elsewhere."""
    info: dict[str, float | None] = {"total_mb": None, "available_mb": None}
    if sys.platform != "darwin":
        return info
    try:
        total = subprocess.run(["sysctl", "-n", "hw.memsize"],
                               capture_output=True, text=True, timeout=10)
        info["total_mb"] = round(int(total.stdout.strip()) / (1024 * 1024), 1)
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10)
        pages_free = 0
        page_size = 16384
        for line in vm.stdout.splitlines():
            if "page size of" in line:
                digits = "".join(ch for ch in line if ch.isdigit())
                page_size = int(digits) if digits else page_size
            if line.startswith(("Pages free:", "Pages inactive:", "Pages speculative:")):
                digits = "".join(ch for ch in line.split(":")[1] if ch.isdigit())
                pages_free += int(digits) if digits else 0
        info["available_mb"] = round(pages_free * page_size / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError):
        pass
    return info


def worker_image_mb(image: str) -> float | None:
    """Installed worker image size in MiB, or None when absent."""
    try:
        out = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Size}}"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, ValueError):
        return None
    if out.returncode != 0 or not out.stdout.strip().isdigit():
        return None
    return round(int(out.stdout.strip()) / (1024 * 1024), 1)


def snapshot(server_match: str = "llama-server", worker_image: str | None = None) -> dict[str, str]:
    """One labeled memory snapshot for event records."""
    host = host_memory_mb()
    snap = {
        "controller_rss_mb": _fmt(controller_rss_mb()),
        "server_rss_mb": _fmt(process_rss_mb(server_match)),
        "host_total_mb": _fmt(host["total_mb"]),
        "host_available_mb": _fmt(host["available_mb"]),
    }
    if worker_image:
        snap["worker_image_mb"] = _fmt(worker_image_mb(worker_image))
    return snap


def _fmt(value: float | None) -> str:
    return str(value) if value is not None else "unknown"


def llama_server_version(binary: str = "llama-server", timeout_s: float = 3.0) -> str:
    """Local `llama-server --version` output, or 'unknown' (never invented, never raises).

    This describes the locally installed binary, which may differ from the
    actually serving server; the two are labeled separately in records. A warm
    call takes about 0.2 s; a cold start can take far longer (Homebrew's took
    18 s once), and that is 'unknown' rather than a wait.
    """
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=timeout_s,
                             stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unknown"
    text = (out.stdout.strip() or out.stderr.strip())[:200]
    return text if text and out.returncode == 0 else "unknown"


def model_file_sha(path: str | None) -> str:
    """SHA-256 of a configured model file, or 'unknown' when absent."""
    if not path:
        return "unknown"
    try:
        import hashlib

        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return "unknown"
