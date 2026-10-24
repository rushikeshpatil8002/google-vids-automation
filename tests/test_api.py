"""HTTP API through the Flask test client and the fake controller (M2.4, 03 §8, A9)."""

import io
import json
import time

import pytest

from app import create_app
from conftest import FAST, REPO, SAMPLE, scenario
from automation.fake_controller import FakeVidsController


@pytest.fixture
def client(make_config):
    cfg = make_config(FAST)
    fake = FakeVidsController(scenario("happy_path"), cfg.paths.data_dir, cfg.paths.screenshots_dir)
    app = create_app(cfg, fake=True, controller=fake)
    app.testing = True
    c = app.test_client()
    c.app_ext = app.extensions["gva"]
    return c


def part_form(n=1, script=None):
    folder = SAMPLE / f"part_{n:02d}"
    return {"script_text": script if script is not None else (folder / "script.txt").read_text(encoding="utf-8"),
            "title": f"P{n}", "images": (io.BytesIO((folder / "reference.png").read_bytes()), "reference.png")}


def sample_manifest(name="Sample_Project"):
    """The sample manifest with paths made absolute (tests run in a temp project root)."""
    m = json.loads((SAMPLE / "job.json").read_text(encoding="utf-8"))
    m.update(project_name=name, output_dir=None)
    for part in m["parts"]:
        part["script_file"] = str(REPO / part["script_file"])
        part["reference_images"] = [str(REPO / r) for r in part["reference_images"]]
    return m


def wait_status(client, job_id, statuses, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get(f"/api/jobs/{job_id}").get_json()
        if view["job"]["overall_status"] in statuses and not view["busy_job"]:
            return view
        time.sleep(0.05)
    raise AssertionError(f"job did not reach {statuses}: {view['job']['overall_status']}")


def test_health_and_index(client):
    assert client.get("/api/health").get_json()["fake_mode"] is True
    assert b"Google Vids Automation" in client.get("/").data


def test_j1_j3_create_add_reorder_remove_validate(client):
    job_id = client.post("/api/jobs", json={"project_name": "Api Job", "mode": "separate_clips"}).get_json()["job_id"]
    ids = [client.post(f"/api/jobs/{job_id}/parts", data=part_form(n), content_type="multipart/form-data").get_json()["part_id"]
           for n in (1, 2, 3)]
    assert client.post(f"/api/jobs/{job_id}/parts/reorder", json={"order": ids[::-1]}).status_code == 200
    view = client.get(f"/api/jobs/{job_id}").get_json()
    assert [p["part_id"] for p in view["parts"]] == ids[::-1]
    assert client.delete(f"/api/jobs/{job_id}/parts/{ids[0]}").status_code == 200
    res = client.post(f"/api/jobs/{job_id}/validate").get_json()
    assert res["ok"] and client.get(f"/api/jobs/{job_id}").get_json()["job"]["overall_status"] == "VALIDATED"
    plan = client.get(f"/api/jobs/{job_id}/plan").get_json()
    assert [p["output"] for p in plan["parts"]] == ["Api_Job_Part01.mp4", "Api_Job_Part02.mp4"]
    assert client.get(f"/api/parts/{ids[1]}/thumb/0").mimetype == "image/png"


def test_validation_errors_reported(client):
    job_id = client.post("/api/jobs", json={"project_name": "Bad", "mode": "single"}).get_json()["job_id"]
    res = client.post(f"/api/jobs/{job_id}/parts", data=part_form(1, script="   "), content_type="multipart/form-data")
    assert res.status_code == 400 and "empty" in res.get_json()["error"]["message"]
    assert client.post("/api/jobs", json={"project_name": "bad/name", "mode": "single"}).status_code == 400
    res = client.post(f"/api/jobs/{job_id}/validate").get_json()
    assert not res["ok"] and any("Add at least one part" in m or "exactly 1" in m for m in res["job_errors"])


def test_j4_import_manifest_and_run_a9_counts(client):
    res = client.post("/api/jobs/import", json=sample_manifest())
    assert res.status_code == 201
    job_id = res.get_json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/start").status_code == 409  # not validated yet
    assert client.post(f"/api/jobs/{job_id}/validate").get_json()["ok"]
    assert client.post(f"/api/jobs/{job_id}/start").status_code == 202
    view = wait_status(client, job_id, {"COMPLETED"})
    assert view["counts"]["done"] == 3 and view["counts"]["DOWNLOADED"] == 3
    assert client.get(f"/api/jobs/{job_id}/report?format=md").status_code == 200
    assert client.post(f"/api/jobs/{job_id}/resume").status_code == 409
    listing = client.get("/api/jobs").get_json()
    assert listing[0]["counts"]["done"] == 3


def test_p3_delete_job_data_keeps_outputs_unless_asked(client):
    job_id = client.post("/api/jobs/import", json=sample_manifest("Del")).get_json()["job_id"]
    client.post(f"/api/jobs/{job_id}/validate")
    client.post(f"/api/jobs/{job_id}/start")
    view = wait_status(client, job_id, {"COMPLETED"})
    outputs = [p["output_path"] for p in view["parts"]]
    cfg = client.app_ext["cfg"]
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert not (cfg.paths.input_dir / "jobs" / "Del").exists()
    from pathlib import Path
    assert all(Path(o).exists() for o in outputs)


def test_dec21_open_output_loopback_only(client):
    job_id = client.post("/api/jobs", json={"project_name": "Lan", "mode": "single"}).get_json()["job_id"]
    res = client.post(f"/api/jobs/{job_id}/open-output", environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert res.status_code == 409


def test_404_and_editing_locked_after_start(client):
    assert client.get("/api/jobs/nope").status_code == 404
    job_id = client.post("/api/jobs/import", json=sample_manifest("Lock")).get_json()["job_id"]
    client.post(f"/api/jobs/{job_id}/validate")
    client.post(f"/api/jobs/{job_id}/start")
    wait_status(client, job_id, {"COMPLETED"})
    res = client.post(f"/api/jobs/{job_id}/parts", data=part_form(1), content_type="multipart/form-data")
    assert res.status_code == 409
