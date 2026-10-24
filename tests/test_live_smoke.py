"""Live end-to-end smoke test (08 §6, A1, A10). USES 1 REAL GOOGLE VIDS GENERATION.

Excluded by default (pytest.ini: -m "not live"). Run only on request: pytest -m live
Preconditions: signed in via `python app.py --login`, selectors verified (DEC-25), config.json present.
"""

import io
import time
import warnings
from pathlib import Path

import pytest

from automation.config import PROJECT_ROOT, load_config
from automation.downloads import verify_video

pytestmark = pytest.mark.live
PART = PROJECT_ROOT / "input" / "Sample_Project" / "part_01"


def test_live_single_job_end_to_end():
    warnings.warn("LIVE TEST: this uses 1 Google Vids generation from your quota.")
    from app import create_app

    cfg = load_config()
    client = create_app(cfg).test_client()
    name = f"Smoke {time.strftime('%Y%m%d %H%M%S', time.gmtime())}"
    job_id = client.post("/api/jobs", json={"project_name": name, "mode": "single"}).get_json()["job_id"]
    res = client.post(f"/api/jobs/{job_id}/parts", content_type="multipart/form-data", data={
        "script_text": (PART / "script.txt").read_text(encoding="utf-8"),
        "images": (io.BytesIO((PART / "reference.png").read_bytes()), "reference.png")})
    assert res.status_code == 201
    assert client.post(f"/api/jobs/{job_id}/validate").get_json()["ok"]
    assert client.post(f"/api/jobs/{job_id}/start").status_code == 202

    deadline = time.time() + cfg.timeouts_seconds.generation + cfg.timeouts_seconds.download_complete + 120
    while time.time() < deadline:
        view = client.get(f"/api/jobs/{job_id}").get_json()
        if view["job"]["overall_status"] not in ("VALIDATED", "RUNNING") and not view["busy_job"]:
            break
        time.sleep(3)
    job, part = view["job"], view["parts"][0]
    assert job["overall_status"] == "COMPLETED", job["last_error"]
    out = Path(part["output_path"])
    assert verify_video(out, cfg).ok
    assert out.name == f"{name.replace(' ', '_')}_Part01.mp4"
    assert (out.parent / f"{name.replace(' ', '_')}_report.json").exists()
    assert not [e for e in view["events"] if e["severity"] == "ERROR"]
