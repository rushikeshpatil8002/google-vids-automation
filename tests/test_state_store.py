"""State store: transitions (04 §3), reorder, cascade delete, events (M2.2, ST1-ST4)."""

import pytest

from automation.models import PartStatus as P
from automation.state_store import InvalidTransition, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "jobs.sqlite3")


def new_job(store, slug="J"):
    job_id = store.create_job(project_name=slug, project_slug=slug, mode="separate_clips", output_dir=store.db_path.parent,
                              clip_template="{project}_Part{part:02d}", final_template="{project}_Final",
                              partial_template="{project}_Partial")
    parts = [store.add_part(job_id, title=None, script_path=store.db_path, script_sha12="abc", script_chars=3,
                            image_paths=["x.png"]) for _ in range(3)]
    return job_id, parts


@pytest.mark.parametrize("path", [
    [P.SUBMITTED, P.GENERATING, P.READY, P.DOWNLOADED],
    [P.SUBMITTED, P.READY],
    [P.BLOCKED_QUOTA, P.PENDING],
    [P.SUBMITTED, P.FAILED, P.PENDING],
    [P.SUBMITTED, P.GENERATING, P.READY, P.FAILED, P.READY],
])
def test_st_allowed_transitions(store, path):
    _, parts = new_job(store)
    for status in path:
        store.set_part_status(parts[0], status)
    assert store.get_part(parts[0])["status"] == path[-1].value


@pytest.mark.parametrize("path", [[P.READY], [P.DOWNLOADED], [P.SUBMITTED, P.PENDING],
                                  [P.SUBMITTED, P.READY, P.DOWNLOADED, P.PENDING]])
def test_st_forbidden_transitions(store, path):
    _, parts = new_job(store)
    with pytest.raises(InvalidTransition):
        for status in path:
            store.set_part_status(parts[0], status)


def test_downloaded_only_leaves_via_reconcile(store):
    _, parts = new_job(store)
    for s in (P.SUBMITTED, P.READY, P.DOWNLOADED):
        store.set_part_status(parts[0], s)
    with pytest.raises(InvalidTransition):
        store.set_part_status(parts[0], P.READY)
    store.set_part_status(parts[0], P.READY, reconcile=True)
    assert store.get_part(parts[0])["status"] == "READY"


def test_st4_status_and_event_in_one_transaction(store):
    job_id, parts = new_job(store)
    store.set_part_status(parts[0], P.SUBMITTED, vids_part_ref="ref-1", event=("SUBMITTED", "INFO", "ok"))
    assert store.get_part(parts[0])["vids_part_ref"] == "ref-1"
    assert store.events(job_id, 1)[0]["step"] == "SUBMITTED"
    with pytest.raises(InvalidTransition):
        store.set_part_status(parts[0], P.DOWNLOADED, event=("DOWNLOAD_VERIFIED", "INFO", "never"))
    assert all(e["step"] != "DOWNLOAD_VERIFIED" for e in store.events(job_id))


def test_j3_reorder_and_remove_renumber(store):
    job_id, (a, b, c) = new_job(store)
    store.reorder_parts(job_id, [c, a, b])
    assert [p["part_id"] for p in store.get_parts(job_id)] == [c, a, b]
    assert [p["sequence"] for p in store.get_parts(job_id)] == [1, 2, 3]
    store.remove_part(a)
    assert [(p["part_id"], p["sequence"]) for p in store.get_parts(job_id)] == [(c, 1), (b, 2)]
    with pytest.raises(ValueError):
        store.reorder_parts(job_id, [c])


def test_p3_delete_cascades(store):
    job_id, parts = new_job(store)
    store.delete_job(job_id)
    assert store.get_job(job_id) is None and store.get_part(parts[0]) is None and store.events(job_id) == []


def test_st1_slug_unique_and_listing(store):
    new_job(store, "A")
    new_job(store, "B")
    assert store.slugs() == {"A", "B"}
    assert len(store.list_jobs()) == 2
