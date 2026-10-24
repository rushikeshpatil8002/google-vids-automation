"""P4 / A12: script text never reaches logs or events; emails are masked (M2.3)."""

import logging

from automation.logging_setup import setup_logging

MARKER = "ZEBRA-SECRET-PROMPT-7731"


def test_p4_script_marker_absent_from_log_and_events(env):
    e = env("happy_path")
    setup_logging(e.cfg)
    job_id = e.jobs.create_job("Private", "single")
    from conftest import SAMPLE
    e.jobs.add_part(job_id, f"A confidential prompt {MARKER}".encode("utf-8"),
                    [("r.png", (SAMPLE / "part_01" / "reference.png").read_bytes())])
    assert e.jobs.validate(job_id, e.fake.capabilities())["ok"]
    assert e.run(job_id) == "COMPLETED"
    for handler in logging.getLogger().handlers:
        handler.flush()
    log_text = (e.cfg.paths.logs_dir / "automation.log").read_text(encoding="utf-8")
    assert "SUBMIT_STARTED" in log_text and MARKER not in log_text
    assert all(MARKER not in ev["message"] for ev in e.store.all_events(job_id))


def test_a12_redaction_masks_emails_and_drops_script_records(make_config):
    cfg = make_config()
    setup_logging(cfg)
    log = logging.getLogger("t")
    log.warning("account someone.name+x@gmail.com signed in")
    log.warning("should vanish", extra={"script_text": MARKER})
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (cfg.paths.logs_dir / "automation.log").read_text(encoding="utf-8")
    assert "***@***" in text and "gmail.com" not in text
    assert "should vanish" not in text
