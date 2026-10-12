"""Bring the agent's deliverables out of the (ephemeral) sandbox before it is deleted."""
from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path

from pinboard.gcp.skills import _safe_rel

log = logging.getLogger(__name__)

MAX_FILES = 50
MAX_TOTAL_BYTES = 25 * 1024 * 1024

_LIST_SCRIPT = (
    "import os,json,sys;r=sys.argv[1];"
    "print(json.dumps(sorted(os.path.relpath(os.path.join(d,f),r) "
    "for d,_,fs in os.walk(r) for f in fs)) if os.path.isdir(r) else '[]')"
)


def collect_outputs(sandbox, root: str, max_files: int = MAX_FILES, max_total: int = MAX_TOTAL_BYTES):
    """Download every file under *root* in the sandbox. Returns (files, skipped)."""
    r = sandbox.execute(f"python3 -c {shlex.quote(_LIST_SCRIPT)} {shlex.quote(root)}")
    if r.exit_code != 0:
        return [], [f"could not list {root}: {r.output.strip()[:200]}"]
    try:
        rels = json.loads(r.output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [], [f"could not read file list from {root}"]

    files, skipped, total = [], [], 0
    for rel in rels:
        safe = _safe_rel(rel)
        if safe is None:
            skipped.append(f"{rel} (unsafe path)")
            continue
        if len(files) >= max_files:
            skipped.append(f"{rel} (file count limit)")
            continue
        (dl,) = sandbox.download_files([f"{root.rstrip('/')}/{rel}"])
        if dl.error or dl.content is None:
            skipped.append(f"{rel} ({dl.error})")
            continue
        if total + len(dl.content) > max_total:
            skipped.append(f"{rel} (size limit)")
            continue
        total += len(dl.content)
        files.append((safe, dl.content))
    return files, skipped


def save_local(files, dest_dir: str | Path, run_id: str) -> list[Path]:
    base = (Path(dest_dir).expanduser() / run_id).resolve()
    saved = []
    for rel, data in files:
        target = (base / rel).resolve()
        if base not in target.parents:  # belt and braces on top of _safe_rel
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        saved.append(target)
    return saved


def save_gcs(files, uri: str, run_id: str, storage_client=None) -> list[str]:
    from pinboard.gcp.skills import parse_gcs_uri

    if storage_client is None:
        from google.cloud import storage

        storage_client = storage.Client()
    bucket_name, prefix = parse_gcs_uri(uri)
    bucket = storage_client.bucket(bucket_name)
    out = []
    for rel, data in files:
        key = f"{prefix}{run_id}/{rel}"
        bucket.blob(key).upload_from_string(data)
        out.append(f"gs://{bucket_name}/{key}")
    return out


def harvest(sandbox, settings, run_id: str, storage_client=None, local: bool = True) -> tuple[list[str], list[str]]:
    """Collect deliverables and save them locally (if local) and to GCS (if configured)."""
    files, skipped = collect_outputs(sandbox, settings.outputs_path)
    saved: list[str] = []
    if files:
        if local:
            saved += [str(p) for p in save_local(files, settings.output_dir, run_id)]
        if settings.outputs_gcs_uri:
            saved += save_gcs(files, settings.outputs_gcs_uri, run_id, storage_client)
    return saved, skipped
