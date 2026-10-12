"""Skills: folders containing SKILL.md (plus scripts) synced into the sandbox.

Source is a Cloud Storage prefix (``gs://bucket/prefix``) or a local folder (dev).
Skills are CODE the agent can execute, so whoever can write to the bucket controls
the agent: restrict the bucket's write access accordingly.
"""
from __future__ import annotations

import posixpath
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILES = 500
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024


@dataclass
class SyncReport:
    files: int = 0
    bytes: int = 0
    skills: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    bucket, _, prefix = uri[5:].partition("/")
    if not bucket:
        raise ValueError(f"missing bucket in {uri!r}")
    prefix = prefix.strip("/")
    return bucket, (prefix + "/" if prefix else "")


def _safe_rel(rel: str) -> str | None:
    rel = posixpath.normpath(rel.replace("\\", "/")).lstrip("/")
    if rel in ("", ".") or rel.startswith("../") or "/../" in rel or rel == "..":
        return None
    return rel


def iter_gcs(uri: str, storage_client=None) -> Iterator[tuple[str, bytes | None]]:
    if storage_client is None:
        from google.cloud import storage

        storage_client = storage.Client()
    bucket, prefix = parse_gcs_uri(uri)
    for blob in storage_client.list_blobs(bucket, prefix=prefix):
        if blob.name.endswith("/"):
            continue
        if blob.size is not None and blob.size > MAX_FILE_BYTES:
            yield blob.name[len(prefix):], None  # None = too large, skipped by the caller
            continue
        yield blob.name[len(prefix):], blob.download_as_bytes()


def iter_local(folder: str) -> Iterator[tuple[str, bytes | None]]:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"skills folder not found: {root}")
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc"):
            yield path.relative_to(root).as_posix(), path.read_bytes()


def iter_source(source: str, storage_client=None) -> Iterator[tuple[str, bytes | None]]:
    return iter_gcs(source, storage_client) if source.startswith("gs://") else iter_local(source)


def sync_skills(sandbox, source: str, dest: str, storage_client=None) -> SyncReport:
    """Copy every file under *source* into *dest* inside the sandbox."""
    report = SyncReport()
    batch: list[tuple[str, bytes]] = []
    for rel, data in iter_source(source, storage_client):
        safe = _safe_rel(rel)
        if safe is None:
            report.skipped.append(f"{rel} (unsafe path)")
            continue
        if data is None or len(data) > MAX_FILE_BYTES:
            report.skipped.append(f"{rel} (over {MAX_FILE_BYTES} bytes)")
            continue
        if report.files >= MAX_FILES or report.bytes + len(data) > MAX_TOTAL_BYTES:
            report.skipped.append(f"{rel} (size/count limit reached)")
            continue
        batch.append((posixpath.join(dest, safe), data))
        report.files += 1
        report.bytes += len(data)
        if safe.endswith("SKILL.md"):
            report.skills.append(posixpath.dirname(safe) or ".")
    if batch:
        for resp in sandbox.upload_files(batch):
            if resp.error:
                report.skipped.append(f"{resp.path} ({resp.error})")
    return report


def upload_skills_dir(local_dir: str, gcs_uri: str, storage_client=None) -> int:
    """Upload a local skills folder to a gs:// prefix. Returns number of files."""
    if storage_client is None:
        from google.cloud import storage

        storage_client = storage.Client()
    bucket_name, prefix = parse_gcs_uri(gcs_uri)
    bucket = storage_client.bucket(bucket_name)
    count = 0
    for rel, data in iter_local(local_dir):
        bucket.blob(prefix + rel).upload_from_string(data)
        count += 1
    return count


_LOCAL_COPIES: dict[str, tuple[float, Path]] = {}
_LOCAL_LOCK = threading.Lock()
LOCAL_COPY_TTL_SECONDS = 300.0


def local_skills_dir(source: str, storage_client=None, *, ttl: float = LOCAL_COPY_TTL_SECONDS, clock=time.monotonic) -> Path:
    """A folder on THIS machine holding the skills, for reading. A local source is used as it is; a ``gs://`` source is copied to a
    temporary folder and refreshed after ``ttl`` seconds (shared by every session of the process)."""
    if not source.startswith("gs://"):
        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"skills folder not found: {root}")
        return root
    with _LOCAL_LOCK:
        cached = _LOCAL_COPIES.get(source)
        if cached and clock() - cached[0] < ttl and cached[1].is_dir():
            return cached[1]
        fresh = Path(tempfile.mkdtemp(prefix="pinboard-skills-"))
        for rel, data in iter_gcs(source, storage_client):
            safe = _safe_rel(rel)
            if safe is None or data is None or len(data) > MAX_FILE_BYTES:
                continue
            target = fresh / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if cached:
            shutil.rmtree(cached[1], ignore_errors=True)
        _LOCAL_COPIES[source] = (clock(), fresh)
        return fresh
