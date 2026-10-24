"""Happy paths for all modes through the fake controller (M3.3, A1, A2, A3, DEC-02, DEC-23)."""

from pathlib import Path

from automation.downloads import verify_video
from automation.validators import script_hash12

from conftest import SAMPLE


def test_a1_single_happy(env):
    """A1: single job created, validated, generated, downloaded, renamed, marked complete."""
    e = env("happy_path")
    job_id = e.sample_job("single", parts=1, name="Solo")
    assert e.run(job_id) == "COMPLETED"
    part = e.parts(job_id)[0]
    out = Path(part["output_path"])
    assert part["status"] == "DOWNLOADED" and out.name == "Solo_Part01.mp4"
    assert verify_video(out, e.cfg).ok and part["output_bytes"] == out.stat().st_size
    assert part["original_filename"].startswith("Fake clip 1")  # DL4
    assert e.job(job_id)["final_export_status"] == "NOT_APPLICABLE"
    assert (out.parent / "Solo_report.json").exists() and (out.parent / "Solo_report.md").exists()  # DEC-20


def test_a2_mapping_order(env):
    """A2: each submit carries its own part's script hash and image, in sequence order."""
    e = env("happy_path")
    job_id = e.sample_job("separate_clips", parts=3)
    assert e.run(job_id) == "COMPLETED"
    subs = e.fake.state()["submissions"]
    expected = [script_hash12((SAMPLE / f"part_{n:02d}" / "script.txt").read_text(encoding="utf-8")) for n in (1, 2, 3)]
    assert [s["seq"] for s in subs] == [1, 2, 3]
    assert [s["sha12"] for s in subs] == expected
    parts = e.parts(job_id)
    assert [s["image_parent"] for s in subs] == [Path(p["image_paths"][0]).parent.name for p in parts]
    assert [Path(p["output_path"]).name for p in parts] == [f"Sample_Project_Part0{n}.mp4" for n in (1, 2, 3)]


def test_mp5_multi_part_with_clips_and_final(env):
    """Branch A with per-clip downloads: every clip plus a verified Final."""
    e = env("happy_path")
    job_id = e.sample_job("multi_part", parts=3)
    assert e.run(job_id) == "COMPLETED"
    job = e.job(job_id)
    assert e.statuses(job_id) == ["DOWNLOADED"] * 3
    assert job["final_export_status"] == "DOWNLOADED"
    assert Path(job["final_output_path"]).name == "Sample_Project_Final.mp4"
    assert verify_video(Path(job["final_output_path"]), e.cfg).ok


def test_dec23_no_per_clip_download_parts_end_ready_with_final(env):
    """DEC-23 (the verified Vids behavior): parts end READY, the job completes on the Final file."""
    e = env("no_per_clip_download")
    job_id = e.sample_job("multi_part", parts=3)
    assert e.run(job_id) == "COMPLETED"
    job = e.job(job_id)
    assert e.statuses(job_id) == ["READY"] * 3
    assert job["final_export_status"] == "DOWNLOADED"
    assert job["limitations"] == ["NO_PER_CLIP_DOWNLOAD"]
    report = (Path(job["output_dir"]) / "Sample_Project_report.json").read_text(encoding="utf-8")
    assert "included in final export" in report


def test_mp2_no_multi_scene_runs_as_separate_clips(env):
    """MP2 / DEC-02: multi_part falls back to separate clips with the limitation recorded, no Final."""
    e = env("no_multi_scene")
    job_id = e.sample_job("multi_part", parts=2)
    assert e.run(job_id) == "COMPLETED"
    job = e.job(job_id)
    assert job["effective_mode"] == "separate_clips"
    assert job["limitations"] == ["MULTI_SCENE_NOT_SUPPORTED"]
    assert job["final_export_status"] == "NOT_SUPPORTED"
    assert e.statuses(job_id) == ["DOWNLOADED"] * 2


def test_st4_commit_sequence_events(env):
    """ST4: every milestone is committed as an event, in order, for each part."""
    e = env("happy_path")
    job_id = e.sample_job("single", parts=1)
    e.run(job_id)
    steps = e.steps(job_id)
    order = ["VALIDATED", "RUN_STARTED", "SUBMIT_STARTED", "SUBMITTED", "GENERATION_READY", "DOWNLOAD_STARTED",
             "DOWNLOAD_VERIFIED", "REPORT_WRITTEN", "RUN_FINISHED"]
    positions = [steps.index(s) for s in order]
    assert positions == sorted(positions)


def test_run_started_event_shows_app_version(env):
    """Remote troubleshooting: events tell which code version ran."""
    from automation import __version__

    e = env("happy_path")
    job_id = e.sample_job("single", parts=1)
    e.run(job_id)
    started = next(ev for ev in e.store.all_events(job_id) if ev["step"] == "RUN_STARTED")
    assert f"(app {__version__})" in started["message"]
