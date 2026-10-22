"""Copy a Hugging Face model into the model bucket, file by file, then list it in models.json.

Runs as a Cloud Run job on CPU, which has no disk: each file streams from
Hugging Face straight into Cloud Storage. Files already in the bucket at the
same size are skipped, so a re-run resumes. The library update uses a
generation precondition, so two jobs cannot overwrite each other's entries.

Run: python -m local_coder.gpu.fetch --repo Qwen/Qwen3.8-27B-FP8 --bucket NAME [--id ID] [--preset P]
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import sys
import time
from typing import Any

from .manifest import MANIFEST_NAME, MODELS_PREFIX, dumps, make_entry, parse, upsert

SKIP = ("original/*", "*.pth", "*.pt", "*.bin.index.json", "*.gguf", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.mp4",
        ".gitattributes")
CHUNK = 64 * 1024 * 1024


def wanted(name: str, include: list[str]) -> bool:
    """Whether a repo file belongs in the bucket copy."""
    if include:
        return any(fnmatch.fnmatch(name, pattern) for pattern in include)
    return not any(fnmatch.fnmatch(name, pattern) for pattern in SKIP)


def log(message: str) -> None:
    print(f"fetch: {message}", flush=True)


def copy_file(bucket: Any, repo: str, revision: str, name: str, size: int, folder: str, token: str | None) -> bool:
    """Stream one file into the bucket; False when an identical copy was already there."""
    import requests
    from huggingface_hub import hf_hub_url

    path = f"{MODELS_PREFIX}/{folder}/{name}"
    existing = bucket.get_blob(path)
    if existing is not None and size and existing.size == size:
        return False
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    started = time.monotonic()
    with requests.get(hf_hub_url(repo, name, revision=revision), headers=headers, stream=True,
                      timeout=(30, 300)) as response:
        response.raise_for_status()
        response.raw.decode_content = True
        with bucket.blob(path).open("wb", chunk_size=CHUNK, ignore_flush=True) as out:
            shutil.copyfileobj(response.raw, out, 8 * 1024 * 1024)
    stored = bucket.get_blob(path)
    if size and (stored is None or stored.size != size):
        raise RuntimeError(f"{name}: stored {stored.size if stored else 0} bytes, expected {size}")
    seconds = max(time.monotonic() - started, 0.001)
    log(f"{name}: {size / 1e6:.0f} MB in {seconds:.0f} s ({size / 1e6 / seconds:.0f} MB/s)")
    return True


def record(bucket: Any, entry: dict[str, Any]) -> None:
    """Add the entry to models.json without losing a concurrent writer's change."""
    from google.api_core.exceptions import PreconditionFailed

    for _attempt in range(8):
        current = bucket.get_blob(MANIFEST_NAME)
        generation = current.generation if current is not None else 0
        library = upsert(parse(current.download_as_bytes() if current is not None else None), entry)
        try:
            bucket.blob(MANIFEST_NAME).upload_from_string(
                dumps(library), content_type="application/json", if_generation_match=generation,
            )
            return
        except PreconditionFailed:
            time.sleep(1.0)
    raise RuntimeError("models.json kept changing; the entry was not recorded")


def main(argv: list[str] | None = None) -> int:
    """Entry point inside the fetch job."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--revision", default="main")
    parser.add_argument("--id", dest="model_id")
    parser.add_argument("--name")
    parser.add_argument("--preset")
    parser.add_argument("--include", action="append", default=[], help="only files matching this glob (repeatable)")
    args = parser.parse_args(argv)

    from google.cloud import storage
    from huggingface_hub import HfApi

    token = os.environ.get("HF_TOKEN") or None
    info = HfApi(token=token).model_info(args.repo, revision=args.revision, files_metadata=True)
    files = [(sibling.rfilename, int(sibling.size or 0)) for sibling in info.siblings or []
             if wanted(sibling.rfilename, args.include)]
    if not any(name.endswith((".safetensors", ".gguf")) for name, _ in files):
        log(f"{args.repo} has no safetensors or GGUF weights to serve")
        return 2
    entry = make_entry(args.repo, preset=args.preset, model_id=args.model_id, name=args.name,
                       size_bytes=sum(size for _, size in files), revision=args.revision)
    bucket = storage.Client().bucket(args.bucket)
    total = sum(size for _, size in files)
    log(f"{args.repo} -> gs://{args.bucket}/{MODELS_PREFIX}/{entry['dir']}: {len(files)} files, {total / 1e9:.1f} GB")
    copied = 0
    for name, size in sorted(files, key=lambda row: row[1]):
        copied += copy_file(bucket, args.repo, args.revision, name, size, entry["dir"], token)
    record(bucket, entry)
    log(f"done: {copied} copied, {len(files) - copied} already there; {entry['id']} is in the library")
    return 0


if __name__ == "__main__":
    sys.exit(main())
