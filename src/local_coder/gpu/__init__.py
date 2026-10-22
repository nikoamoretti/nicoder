"""Your own cloud GPU for Local Coder: a model library in a bucket and one GPU that serves any of them.

- `manifest.py`: the library format (models.json) and per-family server settings.
- `swapper.py`: runs inside the GPU container; loads the requested model, one at a time.
- `fetch.py`: runs as a CPU job; copies a Hugging Face model into the bucket.
- `cloud.py`: runs on the Mac; sets up and drives the Google Cloud pieces with gcloud.

Standard library only in manifest and swapper, so the container needs nothing extra.
"""
