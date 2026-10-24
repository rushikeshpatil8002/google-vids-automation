"""ER1-ER9 through fake scenarios (08 §3, M4.2-4.3)."""

import threading
from pathlib import Path


def test_er1_a5_quota_after_2(env):
    """ER1/A5: no 3rd submit, completed clips saved, the rest BLOCKED_QUOTA and resumable."""
    e = env("quota_after_2")
    job_id = e.sample_job("separate_clips", parts=3)
    assert e.run(job_id) == "BLOCKED_QUOTA"
    assert e.statuses(job_id) == ["DOWNLOADED", "DOWNLOADED", "BLOCKED_QUOTA"]
    assert e.gen_count() == 2 and len(e.fake.state()["submissions"]) == 2
    assert "Resume after the limit resets" in e.job(job_id)["last_error"]
    # Limit resets: Resume finishes part 3 only.
    e.scenario["quota_after_generations"] = None
    first = [Path(p["output_path"]).stat().st_mtime for p in e.parts(job_id)[:2]]
    assert e.restart().run(job_id, resume=True) == "COMPLETED"
    assert e.gen_count() == 3
    assert [Path(p["output_path"]).stat().st_mtime for p in e.parts(job_id)[:2]] == first


def test_dec24_quota_without_clip_downloads_exports_partial(env):
    e = env("quota_after_2_no_clip_dl")
    job_id = e.sample_job("multi_part", parts=3)
    assert e.run(job_id) == "BLOCKED_QUOTA"
    job = e.job(job_id)
    assert e.statuses(job_id) == ["READY", "READY", "BLOCKED_QUOTA"]
    assert job["final_export_status"] == "PARTIAL_DOWNLOADED"
    assert Path(job["partial_output_path"]).name == "Sample_Project_Partial.mp4"
    e.scenario["quota_after_generations"] = None
    assert e.restart().run(job_id, resume=True) == "COMPLETED"
    job = e.job(job_id)
    assert job["final_export_status"] == "DOWNLOADED" and Path(job["final_output_path"]).exists()
    assert Path(job["partial_output_path"]).exists()  # never deleted


def test_er5_a6_gen_fail_part2_then_retry(env):
    """ER5/A6: only part 2 fails (after 1 auto-retry); Retry reruns it alone, others untouched."""
    e = env("gen_fail_part2")
    job_id = e.sample_job("separate_clips", parts=3)
    assert e.run(job_id) == "COMPLETED_WITH_ERRORS"
    assert e.statuses(job_id) == ["DOWNLOADED", "FAILED", "DOWNLOADED"]
    assert e.fake.state()["generations_per_part"]["2"] == 2
    others = {p["sequence"]: Path(p["output_path"]).stat().st_mtime for p in e.parts(job_id) if p["sequence"] != 2}
    assert e.workflow.retry_failed(job_id) == 1
    assert e.run(job_id, resume=True) == "COMPLETED"
    parts = e.parts(job_id)
    assert parts[1]["status"] == "DOWNLOADED" and parts[1]["retry_count"] == 1
    assert {p["sequence"]: Path(p["output_path"]).stat().st_mtime for p in parts if p["sequence"] != 2} == others
    assert e.fake.state()["generations_per_part"] == {"1": 1, "2": 3, "3": 1}


def test_er2_a8_timeout_part1(env):
    """ER2/A8: generation timeout -> FAILED with a screenshot path in the event."""
    e = env("timeout_part1", overrides={"timeouts_seconds": {"generation": 0.3}, "retries": {"part_auto_retries": 0}})
    job_id = e.sample_job("single", parts=1)
    assert e.run(job_id) == "COMPLETED_WITH_ERRORS"
    part = e.parts(job_id)[0]
    assert part["status"] == "FAILED" and part["error_code"] == "TIMEOUT"
    event = next(ev for ev in e.store.all_events(job_id) if ev["step"] == "GENERATION_TIMEOUT")
    assert event["screenshot_path"] and Path(event["screenshot_path"]).exists()


def test_er2_a8_network_error_retries_then_succeeds(env):
    e = env("network_error_part1")
    job_id = e.sample_job("single", parts=1)
    assert e.run(job_id) == "COMPLETED"
    retries = [ev for ev in e.store.all_events(job_id) if ev["step"] == "STEP_RETRY"]
    assert len(retries) == 2 and all("Network" in ev["message"] for ev in retries)


def test_im3_dec26_upload_fail_keeps_part_pending_and_pauses(env):
    """IM3/DEC-26: nothing submitted, part stays PENDING, job PAUSED, later parts untouched; Resume retries."""
    e = env("upload_fail_part1")
    job_id = e.sample_job("separate_clips", parts=3)
    assert e.run(job_id) == "PAUSED"
    part = e.parts(job_id)[0]
    assert part["status"] == "PENDING" and part["error_code"] == "UPLOAD" and part["submit_attempted_at"] is None
    assert e.statuses(job_id)[1:] == ["PENDING", "PENDING"]
    assert e.gen_count() == 0
    assert e.job(job_id)["last_error"] == "Image upload failed for part 1. Check the image, then Resume."
    # Reconciliation must not turn it into UNCERTAIN_SUBMIT; the 4th upload attempt succeeds.
    assert e.restart().run(job_id, resume=True) == "PAUSED"  # attempts 2-3 still fail (one per run)
    e.restart().run(job_id, resume=True)
    assert e.restart().run(job_id, resume=True) == "COMPLETED"
    assert e.gen_count() == 3


def test_er6_download_interrupt_retries_download_not_generation(env):
    e = env("download_interrupt_part1")
    job_id = e.sample_job("single", parts=1)
    assert e.run(job_id) == "COMPLETED"
    assert "DOWNLOAD_RETRY" in e.steps(job_id)
    assert e.gen_count() == 1


def test_er3_a8_login_required_then_resume(env):
    e = env("login_required")
    job_id = e.sample_job("single", parts=1)
    assert e.run(job_id) == "WAITING_FOR_USER"
    assert "Sign in" in e.job(job_id)["last_error"] and e.gen_count() == 0
    assert "LOGIN_REQUIRED" in e.steps(job_id)
    assert e.restart().run(job_id, resume=True) == "COMPLETED"


def test_er4_a8_ui_missing_generate_pauses(env):
    e = env("ui_missing_generate")
    job_id = e.sample_job("separate_clips", parts=3)
    assert e.run(job_id) == "PAUSED"
    assert e.statuses(job_id) == ["FAILED", "PENDING", "PENDING"]
    part = e.parts(job_id)[0]
    assert part["error_code"] == "UI_MISSING" and "generate_button" in part["error_message"]
    event = next(ev for ev in e.store.all_events(job_id) if ev["step"] == "UI_ELEMENT_MISSING")
    assert event["screenshot_path"]


def test_er9_a8_disk_error_waits_then_resumes_without_regenerating(env):
    e = env("disk_error_part2")
    job_id = e.sample_job("separate_clips", parts=3)
    assert e.run(job_id) == "WAITING_FOR_USER"
    job = e.job(job_id)
    assert job["output_dir"] in job["last_error"] and "space" in job["last_error"].lower()
    assert e.statuses(job_id) == ["DOWNLOADED", "READY", "PENDING"]
    assert "DISK_ERROR" in e.steps(job_id)
    assert e.restart().run(job_id, resume=True) == "COMPLETED"
    assert e.fake.state()["generations_per_part"]["2"] == 1


def test_dec12_two_consecutive_failures_pause(env):
    e = env("two_consecutive_fail")
    job_id = e.sample_job("separate_clips", parts=3)
    assert e.run(job_id) == "PAUSED"
    assert e.statuses(job_id) == ["FAILED", "FAILED", "PENDING"]


def test_dec15_stop_safely_finishes_current_part(env):
    e = env("happy_path", generation_seconds=0.3)
    job_id = e.sample_job("separate_clips", parts=3)
    stop = threading.Event()
    original = e.fake.submit_generation

    def submit_then_stop(*args, **kwargs):
        ref = original(*args, **kwargs)
        stop.set()  # stop requested while part 1 is generating
        return ref

    e.fake.submit_generation = submit_then_stop
    assert e.workflow.run_job(job_id, stop) == "PAUSED"
    assert e.statuses(job_id) == ["DOWNLOADED", "PENDING", "PENDING"]


def test_dl5_a4_duplicate_gets_v2_suffix(env):
    """DL5/A4: an existing file is never overwritten; the new one gets _v2 and an event."""
    e = env("happy_path")
    job_id = e.sample_job("single", parts=1, name="Dup")
    out_dir = Path(e.job(job_id)["output_dir"])
    existing = out_dir / "Dup_Part01.mp4"
    existing.write_bytes(b"original bytes")
    assert e.run(job_id) == "COMPLETED"
    assert existing.read_bytes() == b"original bytes"
    assert Path(e.parts(job_id)[0]["output_path"]).name == "Dup_Part01_v2.mp4"
    assert "DUPLICATE_RENAMED" in e.steps(job_id)
