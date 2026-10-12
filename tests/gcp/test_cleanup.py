from types import SimpleNamespace as NS

from pinboard.gcp.runtimes import delete_runtimes, list_runtimes

A = "projects/1/locations/us-central1/reasoningEngines/111"
B = "projects/1/locations/us-central1/reasoningEngines/222"
C = "projects/1/locations/us-central1/reasoningEngines/333"


class FakeRuntimes:
    def __init__(self):
        self.deleted = []
        self.items = [
            NS(api_resource=NS(name=A, display_name="pinboard-sandbox", labels={"created-by": "pinboard-agent"}, create_time="t1")),
            NS(api_resource=NS(name=B, display_name="", labels=None, create_time="t2")),
            NS(api_resource=NS(name=C, display_name="my-real-agent", labels={}, create_time="t3")),
        ]

    def list(self):
        return iter(self.items)

    def delete(self, *, name, force=None):
        assert force is True  # children (sandboxes, templates) must go too
        self.deleted.append(name)


def client():
    return NS(runtimes=FakeRuntimes())


def test_list_marks_ours_and_in_use():
    infos = {r.name: r for r in list_runtimes(client(), in_use=A)}
    assert infos[A].ours and infos[A].in_use
    assert not infos[B].ours and not infos[C].ours and not infos[B].in_use


def test_in_use_runtime_is_never_deleted():
    c = client()
    rep = delete_runtimes(c, [A], in_use=A, confirm=lambda i: True)
    assert "in use" in rep[A] and c.runtimes.deleted == []


def test_unknown_names_are_skipped():
    c = client()
    rep = delete_runtimes(c, ["projects/1/locations/us-central1/reasoningEngines/999"], in_use=None, confirm=lambda i: True)
    assert "not found" in next(iter(rep.values())) and c.runtimes.deleted == []


def test_declined_confirmation_deletes_nothing():
    c = client()
    assert "not confirmed" in delete_runtimes(c, [B], in_use=A, confirm=lambda i: False)[B]
    assert c.runtimes.deleted == []


def test_only_named_runtimes_are_deleted():
    c = client()
    seen = []
    rep = delete_runtimes(c, [B], in_use=A, confirm=lambda i: seen.append(i) or True)
    assert rep[B] == "deleted" and c.runtimes.deleted == [B]  # C (someone's real agent) untouched
    assert seen[0].ours is False  # the confirm callback can warn about non-pinboard runtimes
