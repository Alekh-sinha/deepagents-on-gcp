import hashlib
from datetime import timedelta

import pytest

from pinboard.gcp.artifacts import Artifacts, GCSBlobStore, LocalBlobStore, make_blob_store, user_hash
from pinboard.gcp.sandbox import GCPShellSandbox
from pinboard.core.guardrails import CommandGuard
from pinboard.state import MemoryStore, utcnow

from helpers import FakeClient


class Fixture:
    def __init__(self, tmp_path):
        self.box = tmp_path / "box"
        (self.box / "out").mkdir(parents=True)
        self.sb = GCPShellSandbox(FakeClient(self.box), "sb", guard=CommandGuard("allowlist", str(self.box)), cwd=str(self.box))
        self.store, self.blobs = MemoryStore(), LocalBlobStore(tmp_path / "blobs")
        self.clock = [utcnow()]
        self.art = Artifacts(self.store, self.blobs, lambda: self.clock[0])
        self.root = f"{self.box}/out"

    def write(self, rel, data=b"x"):
        p = self.box / "out" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())

    def capture(self, run="r1", user="me@example.com", session="s1"):
        return self.art.capture(self.sb, self.root, user_id=user, session_id=session, run_id=run)


@pytest.fixture
def fx(tmp_path):
    return Fixture(tmp_path)


def test_new_files_are_stored_for_the_user_and_indexed(fx):
    fx.write("report.md", "# Q3")
    fx.write("data/rows.csv", "a,b\n1,2\n")
    fx.write("chart.png", b"\x89PNG")
    new, skipped = fx.capture()
    assert skipped == [] and sorted(a.path for a in new) == ["chart.png", "data/rows.csv", "report.md"]
    rec = {a.path: a for a in new}
    assert rec["data/rows.csv"].name == "rows.csv" and rec["data/rows.csv"].content_type == "text/csv" and rec["chart.png"].content_type == "image/png"
    assert rec["report.md"].size == 4 and rec["report.md"].sha256 == hashlib.sha256(b"# Q3").hexdigest() and rec["report.md"].session_id == "s1"
    assert fx.blobs.get(rec["report.md"].blob_key) == b"# Q3"
    assert rec["report.md"].blob_key == f"users/{user_hash('me@example.com')}/artifacts/s1/r1/report.md" and "@" not in rec["report.md"].blob_key
    assert len(fx.art.list("me@example.com")) == 3


def test_unchanged_files_are_not_stored_again_but_changed_ones_become_new_versions(fx):
    fx.write("report.md", "v1")
    assert len(fx.capture(run="r1")[0]) == 1
    assert fx.capture(run="r2")[0] == []  # same content: nothing new
    fx.write("report.md", "v2 with edits")
    fx.clock[0] += timedelta(minutes=1)
    new, _ = fx.capture(run="r3")
    assert len(new) == 1 and new[0].run_id == "r3" and new[0].size == 13
    versions = [a for a in fx.art.list("me@example.com") if a.path == "report.md"]
    assert len(versions) == 2 and fx.blobs.get(versions[0].blob_key) == b"v2 with edits" and fx.blobs.get(versions[1].blob_key) == b"v1"
    assert len(fx.capture(run="r4", session="s2")[0]) == 1  # another session keeps its own history


def test_an_empty_or_missing_output_folder_is_fine(fx, tmp_path):
    assert fx.capture() == ([], [])
    new, skipped = fx.art.capture(fx.sb, f"{fx.box}/does-not-exist", user_id="u", session_id="s", run_id="r")
    assert new == [] and skipped == []


def test_limits_are_reported_not_fatal(fx, monkeypatch):
    import pinboard.gcp.artifacts as mod

    monkeypatch.setattr(mod, "MAX_FILES", 2)
    for i in range(4):
        fx.write(f"f{i}.txt", f"file {i}")
    new, skipped = fx.capture()
    assert len(new) == 2 and any("file count limit" in s for s in skipped)


def test_a_failing_blob_store_loses_only_that_file(fx):
    fx.write("a.txt", "a")
    fx.write("b.txt", "b")

    class Flaky(LocalBlobStore):
        def put(self, key, data, content_type):
            if key.endswith("a.txt"):
                raise OSError("disk full")
            super().put(key, data, content_type)

    fx.art.blobs = Flaky(fx.blobs.root)
    new, skipped = fx.capture()
    assert [a.path for a in new] == ["b.txt"] and any("a.txt" in s and "could not be stored" in s for s in skipped)
    assert fx.art.store.latest_artifact("me@example.com", "s1", "a.txt") is None  # no index row for bytes that were never saved


def test_reading_is_limited_to_the_owner_and_to_a_sane_size(fx):
    fx.write("small.txt", "hello")
    fx.write("big.bin", b"z" * 5000)
    new, _ = fx.capture()
    small = next(a for a in new if a.path == "small.txt")
    big = next(a for a in new if a.path == "big.bin")
    rec, data = fx.art.read("me@example.com", small.id)
    assert data == b"hello" and rec.name == "small.txt"
    with pytest.raises(LookupError):
        fx.art.read("someone-else", small.id)  # another user's file looks exactly like a missing one
    with pytest.raises(LookupError):
        fx.art.get("me@example.com", "a_missing")
    with pytest.raises(ValueError, match="inline limit"):
        fx.art.read("me@example.com", big.id, max_bytes=1000)
    assert fx.art.read("me@example.com", big.id)[1] == b"z" * 5000
    assert fx.art.url("me@example.com", small.id) is None  # the local store cannot mint download links


def test_the_local_blob_store_refuses_paths_that_escape_its_folder(tmp_path):
    s = LocalBlobStore(tmp_path / "blobs")
    s.put("users/a/artifacts/s/r/x.txt", b"ok", "text/plain")
    assert s.get("users/a/artifacts/s/r/x.txt") == b"ok"
    for bad in ("../outside.txt", "a/../../outside", "..", ""):
        with pytest.raises(ValueError):
            s.put(bad, b"x", "text/plain")
        with pytest.raises(ValueError):
            s.get(bad)
    s.put("/etc/passwd", b"harmless", "text/plain")  # an absolute-looking key is re-rooted inside the store, never the real /etc
    assert (tmp_path / "blobs" / "etc" / "passwd").read_bytes() == b"harmless"
    assert not (tmp_path / "outside.txt").exists() and not (tmp_path / "outside").exists()


def test_the_blob_store_follows_the_uri_and_gcs_keys_go_under_the_prefix():
    assert isinstance(make_blob_store("./somewhere"), LocalBlobStore) and isinstance(make_blob_store("gs://bkt/pre"), GCSBlobStore)

    class Blob:
        def __init__(self, name, sink):
            self.name, self.sink = name, sink

        def upload_from_string(self, data, content_type=None):
            self.sink[self.name] = (data, content_type)

        def download_as_bytes(self):
            return self.sink[self.name][0]

    class Bucket:
        def __init__(self, sink):
            self.sink = sink

        def blob(self, name):
            return Blob(name, self.sink)

    class Client:
        sink: dict = {}

        def bucket(self, name):
            assert name == "bkt"
            return Bucket(self.sink)

    client = Client()
    g = GCSBlobStore("gs://bkt/pinboard", client, signer=lambda blob, seconds: f"https://signed/{blob.name}?ttl={seconds}")
    g.put("users/u/artifacts/s/r/a.md", b"data", "text/markdown")
    assert client.sink["pinboard/users/u/artifacts/s/r/a.md"] == (b"data", "text/markdown") and g.get("users/u/artifacts/s/r/a.md") == b"data"
    assert g.signed_url("users/u/artifacts/s/r/a.md", 600) == "https://signed/pinboard/users/u/artifacts/s/r/a.md?ttl=600"
