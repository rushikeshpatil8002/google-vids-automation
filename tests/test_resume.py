"""Crash/restart recovery (08 §4, A7, ER8). "Generation count" is the fake's persisted counter."""

from pathlib import Path

import pytest


class Crash(SystemExit):
    pass


def crash_run(e, job_id):
    with pytest.raises(SystemExit):
        e.run(job_id)
    assert e.job(job_id)["overall_status"] == "RUNNING"  # left as a crash would leave it


def restart(e, job_id):
    e.restart()
    e.workflow.reconcile(job_id, startup=True)  # what create_app does for non-terminal jobs
    return e


def test_a7_crash_after_submit_started_marks_uncertain_no_auto_resubmit(env):
    e = env("happy_path")
    job_id = e.sample_job("single", parts=1)
    e.fake.submit_generation = lambda *a, **k: (_ for _ in ()).throw(Crash())
    crash_run(e, job_id)
    restart(e, job_id)
    job, part = e.job(job_id), e.parts(job_id)[0]
    assert job["overall_status"] == "PAUSED" and "RECONCILED" in e.steps(job_id)
    assert part["status"] == "FAILED" and part["error_code"] == "UNCERTAIN_SUBMIT"
    assert e.run(job_id, resume=True) == "COMPLETED_WITH_ERRORS"
    assert e.gen_count() == 0  # never resubmitted automatically
    e.workflow.retry_failed(job_id)
    assert e.run(job_id, resume=True) == "COMPLETED" and e.gen_count() == 1


def test_a7_crash_after_submitted_repolls_same_ref(env):
    e = env("happy_path")
    job_id = e.sample_job("single", parts=1)
    e.fake.poll_generation = lambda ref: (_ for _ in ()).throw(Crash())
    crash_run(e, job_id)
    ref = e.parts(job_id)[0]["vids_part_ref"]
    restart(e, job_id)
    assert e.parts(job_id)[0]["status"] == "SUBMITTED"
    assert e.run(job_id, resume=True) == "COMPLETED"
    assert e.gen_count() == 1 and e.parts(job_id)[0]["vids_part_ref"] == ref


def test_a7_crash_after_generation_ready_redownloads(env):
    e = env("happy_path")
    job_id = e.sample_job("single", parts=1)
    e.fake.download_clip = lambda *a, **k: (_ for _ in ()).throw(Crash())
    crash_run(e, job_id)
    restart(e, job_id)
    assert e.parts(job_id)[0]["status"] == "READY"
    assert e.run(job_id, resume=True) == "COMPLETED" and e.gen_count() == 1


def test_a7_crash_during_download_moves_orphan_and_redownloads(env):
    e = env("happy_path")
    job_id = e.sample_job("single", parts=1)
    original = e.fake._deliver

    def partial_then_crash(part_ref, download_dir):
        original(Path(download_dir), "half.mp4", complete=False)
        raise Crash()

    e.fake.download_clip = partial_then_crash
    crash_run(e, job_id)
    restart(e, job_id)
    assert "ORPHAN_DOWNLOAD" in e.steps(job_id)
    assert (e.cfg.paths.download_staging_dir / "_orphans" / "half.mp4.crdownload").exists()
    assert e.run(job_id, resume=True) == "COMPLETED" and e.gen_count() == 1


def test_a7_crash_between_parts_keeps_downloaded(env):
    e = env("happy_path")
    job_id = e.sample_job("separate_clips", parts=2)
    original = e.fake.set_context

    def crash_on_part2(job, seq):
        if seq == 2:
            raise Crash()
        original(job, seq)

    e.fake.set_context = crash_on_part2
    crash_run(e, job_id)
    restart(e, job_id)
    assert e.statuses(job_id) == ["DOWNLOADED", "PENDING"]
    assert e.run(job_id, resume=True) == "COMPLETED"
    assert e.fake.state()["generations_per_part"] == {"1": 1, "2": 1}


def test_a7_deleted_output_is_redownloaded_not_regenerated(env):
    e = env("happy_path")
    job_id = e.sample_job("separate_clips", parts=2)
    original = e.fake.set_context

    def crash_on_part2(job, seq):
        if seq == 2:
            raise Crash()
        original(job, seq)

    e.fake.set_context = crash_on_part2
    crash_run(e, job_id)
    Path(e.parts(job_id)[0]["output_path"]).unlink()
    restart(e, job_id)
    assert e.statuses(job_id) == ["READY", "PENDING"]
    assert e.run(job_id, resume=True) == "COMPLETED"
    assert e.fake.state()["generations_per_part"] == {"1": 1, "2": 1}
    assert Path(e.parts(job_id)[0]["output_path"]).exists()
