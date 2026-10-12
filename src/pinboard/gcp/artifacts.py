"""Artifacts: files the agent produced, kept for the USER (not the session, not the sandbox) so they can be found days later.

At the end of every session turn the files under the output folder are copied out of the sandbox into a blob store (Cloud Storage in the cloud,
a local folder in development) and indexed in the database. A file whose content did not change since the last capture in that session is not
stored again; a changed file becomes a new version. Blob keys use a hash of the user id, so no e-mail address appears in a path:

    users/<hash(user)>/artifacts/<session id>/<turn id>/<path under the output folder>
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import posixpath
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from pinboard.gcp.outputs import MAX_FILES, MAX_TOTAL_BYTES, collect_outputs
from pinboard.gcp.skills import _safe_rel, parse_gcs_uri
from pinboard.state import ArtifactRecord, StateStore, new_artifact_id, utcnow

log = logging.getLogger(__name__)

READ_LIMIT_BYTES = 2 * 1024 * 1024  # largest file returned inline by read(); bigger ones are fetched through a download URL


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def signed_url(self, key: str, seconds: int) -> str | None: ...


class LocalBlobStore:
    """Development store: files in a folder. Cannot mint download URLs, so ``signed_url`` is None and clients use the API instead."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def _path(self, key: str) -> Path:
        safe = _safe_rel(key)
        p = (self.root / (safe or "")).resolve()
        if safe is None or self.root not in p.parents:
            raise ValueError(f"unsafe blob key {key!r}")
        return p

    def put(self, key, data, content_type):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key):
        return self._path(key).read_bytes()

    def signed_url(self, key, seconds):
        return None


class GCSBlobStore:
    def __init__(self, uri: str, client=None, *, signer: Callable[[object, int], str] | None = None):
        self.bucket_name, self.prefix = parse_gcs_uri(uri)
        self._client, self._signer = client, signer

    def _bucket(self):
        if self._client is None:
            from google.cloud import storage

            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def put(self, key, data, content_type):
        self._bucket().blob(self.prefix + key).upload_from_string(data, content_type=content_type)

    def get(self, key):
        return self._bucket().blob(self.prefix + key).download_as_bytes()

    def signed_url(self, key, seconds):
        blob = self._bucket().blob(self.prefix + key)
        if self._signer is not None:
            return self._signer(blob, seconds)
        # On Google compute (Cloud Run, Agent Runtime) there is no private key: sign through the IAM Credentials API with the runtime identity.
        import google.auth
        from google.auth.transport.requests import Request

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())
        return blob.generate_signed_url(version="v4", expiration=timedelta(seconds=seconds), method="GET",
                                        service_account_email=getattr(creds, "service_account_email", None), access_token=creds.token)


def make_blob_store(uri: str, client=None) -> BlobStore:
    return GCSBlobStore(uri, client) if uri.startswith("gs://") else LocalBlobStore(uri)


def user_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


class Artifacts:
    def __init__(self, store: StateStore, blobs: BlobStore, clock: Callable[[], datetime] = utcnow):
        self.store, self.blobs, self.clock = store, blobs, clock

    def capture(self, sandbox, root: str, *, user_id: str, session_id: str, run_id: str) -> tuple[list[ArtifactRecord], list[str]]:
        """Copy new or changed files under ``root`` out of the sandbox. Returns (new records, notes about files that were skipped)."""
        files, skipped = collect_outputs(sandbox, root, MAX_FILES, MAX_TOTAL_BYTES)
        created: list[ArtifactRecord] = []
        for rel, data in files:
            sha = hashlib.sha256(data).hexdigest()
            latest = self.store.latest_artifact(user_id, session_id, rel)
            if latest is not None and latest.sha256 == sha:
                continue  # unchanged since the last capture in this session
            key = f"users/{user_hash(user_id)}/artifacts/{session_id}/{run_id}/{rel}"
            ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            try:
                self.blobs.put(key, data, ctype)
            except Exception as exc:  # noqa: BLE001  one file failing must not lose the others
                log.exception("could not store artifact %s", rel)
                skipped.append(f"{rel} (could not be stored: {type(exc).__name__})")
                continue
            rec = ArtifactRecord(id=new_artifact_id(), user_id=user_id, session_id=session_id, run_id=run_id, name=posixpath.basename(rel), path=rel,
                                 size=len(data), content_type=ctype, sha256=sha, blob_key=key, created_at=self.clock())
            self.store.create_artifact(rec)
            created.append(rec)
        return created, skipped

    def list(self, user_id: str, session_id: str | None = None, limit: int = 50) -> list[ArtifactRecord]:
        return self.store.list_artifacts(user_id, session_id, limit)

    def get(self, user_id: str, artifact_id: str) -> ArtifactRecord:
        rec = self.store.get_artifact(artifact_id)
        if rec is None or rec.user_id != user_id:  # another user's artifact looks exactly like a missing one
            raise LookupError("artifact not found")
        return rec

    def read(self, user_id: str, artifact_id: str, max_bytes: int = READ_LIMIT_BYTES) -> tuple[ArtifactRecord, bytes]:
        rec = self.get(user_id, artifact_id)
        if rec.size > max_bytes:
            raise ValueError(f"{rec.name} is {rec.size} bytes, over the {max_bytes} byte inline limit; use a download URL")
        return rec, self.blobs.get(rec.blob_key)

    def url(self, user_id: str, artifact_id: str, seconds: int = 600) -> str | None:
        return self.blobs.signed_url(self.get(user_id, artifact_id).blob_key, seconds)


class NoArtifacts:
    """Stands in for Artifacts when artifacts are switched off (policy ``features.artifacts: false``): nothing is saved, the list is empty and
    every id is unknown. It is falsy, so an agent builder that adds artifact tools when it is given an artifact store adds none."""

    def __bool__(self) -> bool:
        return False

    def capture(self, sandbox, root: str, *, user_id: str, session_id: str, run_id: str) -> tuple[list, list[str]]:
        return [], []

    def list(self, user_id: str, session_id: str | None = None, limit: int = 50) -> list:
        return []

    def get(self, user_id: str, artifact_id: str):
        raise LookupError("artifact not found")

    def read(self, user_id: str, artifact_id: str, max_bytes: int = READ_LIMIT_BYTES):
        raise LookupError("artifact not found")

    def url(self, user_id: str, artifact_id: str, seconds: int = 600):
        raise LookupError("artifact not found")
