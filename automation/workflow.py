"""Orchestration: run loop, state machine, retries, quota stop, exports, reconciliation (03 §5, 04).

The only module that changes part and job statuses, always through state_store.
Never logs script text: only job/part ids, step names, the 12-char hash and char count (P4).
"""

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from automation import __version__ as APP_VERSION, downloads
from automation.config import Config
from automation.controller_base import GenerationState, VidsController
from automation.errors import (
    BrowserStartError,
    ConfigError,
    DiskError,
    DownloadError,
    LoginRequiredError,
    NetworkError,
    QuotaReachedError,
    UIElementMissingError,
    UploadError,
)
from automation.jobs import resolve_mode
from automation.models import FinalExportStatus, JobMode, JobStatus, PartStatus
from automation.state_store import Store, now
from automation.validators import render_name, render_names

log = logging.getLogger(__name__)

QUOTA_MESSAGE = "Usage limit reached. Completed outputs saved. Resume after the limit resets."


class QuotaStop(Exception):
    pass


class NeedsUser(Exception):
    pass


class PauseJob(Exception):
    pass


@dataclass
class PartOutcome:
    ok: bool
    retryable: bool = False  # ER2/ER5: eligible for the automatic part retry (DEC-12)


class Workflow:
    def __init__(self, store: Store, cfg: Config, controller: VidsController,
                 on_step: Callable[[str | None, str | None, str], None] | None = None) -> None:
        self.store = store
        self.cfg = cfg
        self.ctl = controller
        self.on_step = on_step or (lambda job_id, part_id, step: None)
        self.quota_hit = False

    # ----- helpers ---------------------------------------------------------------------------

    def _log(self, job: dict[str, Any], part: dict[str, Any] | None, step: str, msg: str, level: int = logging.INFO) -> None:
        log.log(level, msg, extra={"job": job["job_id"][:8], "part": part["sequence"] if part else "-", "step": step})
        self.on_step(job["job_id"], part["part_id"] if part else None, step)

    def _job(self, job_id: str) -> dict[str, Any]:
        return self.store.get_job(job_id)

    def _caps(self):
        return self.ctl.capabilities()

    def _needs_clip_download(self, job: dict[str, Any]) -> bool:
        return job["effective_mode"] != JobMode.MULTI_PART.value or self._caps().per_clip_download

    def _part_succeeded(self, job: dict[str, Any], part: dict[str, Any]) -> bool:
        if part["status"] == PartStatus.DOWNLOADED.value:
            return True
        return part["status"] == PartStatus.READY.value and not self._needs_clip_download(job)  # DEC-23

    def _staging(self, job: dict[str, Any]) -> Path:
        return self.cfg.paths.download_staging_dir / job["job_id"]

    def _backoff(self, attempt: int) -> float:
        seq = self.cfg.retries.backoff_seconds
        return float(seq[min(attempt - 1, len(seq) - 1)]) if seq else 0.0

    # ----- public entry points ----------------------------------------------------------------

    def run_job(self, job_id: str, stop_event: threading.Event, resume: bool = False) -> str:
        """Run or resume a job to a stopping point. Returns the final job status."""
        self.quota_hit = False
        job = self._job(job_id)
        self.reconcile(job_id, startup=False)
        if resume:
            for part in self.store.get_parts(job_id):
                if part["status"] == PartStatus.BLOCKED_QUOTA.value:
                    self.store.set_part_status(part["part_id"], PartStatus.PENDING, error_code=None, error_message=None)
        self.store.set_job_status(job_id, JobStatus.RUNNING, last_error=None,
                                  event=("RESUMED" if resume else "RUN_STARTED", "INFO",
                                         f"{'Run resumed' if resume else 'Run started'} (app {APP_VERSION})"))
        self._log(job, None, "RUN_STARTED", "run started")
        status = self._run(job_id, stop_event)
        self._finish(job_id)
        return status

    def _run(self, job_id: str, stop_event: threading.Event) -> str:
        job = self._job(job_id)
        try:
            self.ctl.start()
            self.store.add_event(job_id, None, "BROWSER_STARTED", "INFO", "Browser ready")
            self.store.add_event(job_id, None, "LOGIN_CHECK", "INFO", "Checking Google sign-in")
            self.ctl.ensure_ready()
        except LoginRequiredError:
            return self._wait_for_user(job_id, "Sign in to Google in the automation Chrome window, then click Resume.",
                                       "LOGIN_REQUIRED")
        except (BrowserStartError, ConfigError) as exc:
            self.store.set_job_status(job_id, JobStatus.FAILED, last_error=str(exc),
                                      event=("RUN_FINISHED", "ERROR", f"Could not start: {exc}"))
            return JobStatus.FAILED.value
        except NetworkError as exc:
            return self._pause(job_id, f"Google Vids did not load: {exc}. Check the connection, then Resume.")

        caps = self._caps()
        effective, limitations = resolve_mode(job["mode"], caps)
        fields: dict[str, Any] = {"effective_mode": effective, "limitations": limitations}
        if effective == JobMode.MULTI_PART.value and caps.combined_export:
            if job["final_export_status"] == FinalExportStatus.NOT_APPLICABLE.value:
                fields["final_export_status"] = FinalExportStatus.PENDING
        elif job["final_export_status"] == FinalExportStatus.NOT_APPLICABLE.value and job["mode"] == JobMode.MULTI_PART.value:
            fields["final_export_status"] = FinalExportStatus.NOT_SUPPORTED
        self.store.update_job(job_id, **fields)
        job = self._job(job_id)

        try:
            while True:
                if stop_event.is_set():
                    return self._pause(job_id, "Stopped safely.", step="PAUSED")
                part = self._next_part(job_id)
                if part is None:
                    break
                outcome = self._process_with_retries(job, part)
                job = self._job(job_id)
                failures = 0 if outcome.ok else job["consecutive_failures"] + 1
                self.store.update_job(job_id, consecutive_failures=failures)
                if failures >= self.cfg.retries.consecutive_part_failures_to_pause:
                    return self._pause(job_id, f"{failures} parts failed in a row. Check the errors, then Resume or Retry.")
                if self.quota_hit:
                    raise QuotaStop()
            if self._final_export_due(job_id):
                self._export(job_id, partial=False)
        except QuotaStop:
            return self._quota_stop(job_id)
        except NeedsUser as exc:
            return self._wait_for_user(job_id, str(exc), None)
        except PauseJob as exc:
            return self._pause(job_id, str(exc))
        return self._final_status(job_id)

    def _next_part(self, job_id: str) -> dict[str, Any] | None:
        job = self._job(job_id)
        for part in self.store.get_parts(job_id):
            status = PartStatus(part["status"])
            if status in (PartStatus.PENDING, PartStatus.SUBMITTED, PartStatus.GENERATING):
                return part
            if status is PartStatus.READY and self._needs_clip_download(job):
                return part
        return None

    # ----- one part ------------------------------------------------------------------------------

    def _process_with_retries(self, job: dict[str, Any], part: dict[str, Any]) -> PartOutcome:
        auto_retries = self.cfg.retries.part_auto_retries
        while True:
            outcome = self._process(job, part)
            if outcome.ok or not outcome.retryable or auto_retries <= 0 or self.quota_hit:
                return outcome
            auto_retries -= 1
            part = self.store.get_part(part["part_id"])
            target = PartStatus.READY if part["generated_at"] and part["vids_part_ref"] else PartStatus.PENDING
            self.store.set_part_status(part["part_id"], target, submit_attempted_at=None,
                                       event=("STEP_RETRY", "WARNING", f"Automatic retry of part {part['sequence']}"))
            part = self.store.get_part(part["part_id"])

    def _process(self, job: dict[str, Any], part: dict[str, Any]) -> PartOutcome:
        self.ctl.set_context(job["job_id"], part["sequence"])
        status = PartStatus(part["status"])
        if status is PartStatus.PENDING:
            result = self._submit(job, part)
            if result is not None:
                return result
            part = self.store.get_part(part["part_id"])
            status = PartStatus(part["status"])
        if status in (PartStatus.SUBMITTED, PartStatus.GENERATING):
            result = self._await_generation(job, part)
            if result is not None:
                return result
            part = self.store.get_part(part["part_id"])
        if part["status"] == PartStatus.READY.value and self._needs_clip_download(job):
            return self._download_part(job, part)
        return PartOutcome(ok=True)

    def _project_for(self, job: dict[str, Any], part: dict[str, Any]) -> tuple[str, str | None]:
        """Create or reuse the Vids project (DEC-03) and, in multi_part, a scene for this part."""
        if job["effective_mode"] == JobMode.MULTI_PART.value:
            ref = job["vids_project_ref"]
            placed = [p for p in self.store.get_parts(job["job_id"])
                      if p["status"] in (PartStatus.READY.value, PartStatus.DOWNLOADED.value)]
            if not ref:
                ref = self.ctl.create_project(job["project_name"])
                self.store.update_job(job["job_id"], vids_project_ref=ref,
                                      event=("PROJECT_CREATED", "INFO", "Vids project created"))
            else:
                self.ctl.open_project(ref)
            scene = None
            if placed:
                scene = self.ctl.add_scene(ref)
                self.store.add_event(job["job_id"], part["part_id"], "SCENE_ADDED", "INFO", f"Scene added for part {part['sequence']}")
            return ref, scene
        ref = part["vids_project_ref"]
        if not ref:
            name = job["project_name"] if job["effective_mode"] == JobMode.SINGLE.value else f"{job['project_name']} - Part {part['sequence']:02d}"
            ref = self.ctl.create_project(name)
            self.store.update_part(part["part_id"], vids_project_ref=ref,
                                   event=("PROJECT_CREATED", "INFO", f"Vids project created for part {part['sequence']}"))
        else:
            self.ctl.open_project(ref)
        return ref, None

    def _submit(self, job: dict[str, Any], part: dict[str, Any]) -> PartOutcome | None:
        """PENDING -> SUBMITTED. Returns an outcome on failure, None on success."""
        pid, seq = part["part_id"], part["sequence"]
        script = Path(part["script_path"]).read_text(encoding="utf-8")
        images = [Path(p) for p in part["image_paths"]]
        attempts = self.cfg.retries.step_max_attempts
        for attempt in range(1, attempts + 1):
            try:
                self.ctl.check_quota()
                project_ref, scene_ref = self._project_for(job, part)
                self.store.update_part(pid, submit_attempted_at=now(), vids_scene_ref=scene_ref,
                                       event=("SUBMIT_STARTED", "INFO", f"Submitting part {seq} (script {part['script_sha12']}, {part['script_chars']} chars)"))
                self._log(job, part, "SUBMIT_STARTED", f"submit sha12={part['script_sha12']} chars={part['script_chars']}")
                ref = self.ctl.submit_generation(project_ref, scene_ref, script, images)
                self.store.set_part_status(pid, PartStatus.SUBMITTED, vids_part_ref=ref, error_code=None,
                                           error_message=None, event=("SUBMITTED", "INFO", f"Part {seq} submitted"))
                return None
            except UploadError as exc:  # DEC-26 / DEC-28: nothing was submitted
                self.store.update_part(pid, submit_attempted_at=None, error_code=exc.error_code, error_message=str(exc),
                                       event=("UPLOAD_FAILED", "ERROR", f"Part {seq}: {exc}"))
                if exc.error_code == "PROMPT_MISMATCH":
                    raise PauseJob(f"The prompt shown in Vids didn't match the script for part {seq}. Resume to try again.")
                raise PauseJob(f"Image upload failed for part {seq}. Check the image, then Resume.")
            except QuotaReachedError:
                self.store.set_part_status(pid, PartStatus.BLOCKED_QUOTA, submit_attempted_at=None,
                                           error_code="QUOTA", error_message="Usage limit reached")
                raise QuotaStop()
            except LoginRequiredError:
                self.store.update_part(pid, submit_attempted_at=None)
                raise NeedsUser("Sign in to Google in the automation Chrome window, then click Resume.")
            except UIElementMissingError as exc:  # ER4: likely systemic
                self.store.set_part_status(pid, PartStatus.FAILED, submit_attempted_at=None, error_code="UI_MISSING",
                                           error_message=str(exc),
                                           event=("UI_ELEMENT_MISSING", "ERROR", f"{exc}. Google may have changed the Vids page.", exc.screenshot))
                raise PauseJob(f"Part {seq}: {exc}. Google may have changed the Vids page; the job is paused.")
            except NetworkError as exc:  # ER2
                self.store.update_part(pid, submit_attempted_at=None)
                if attempt < attempts:
                    self.store.add_event(job["job_id"], pid, "STEP_RETRY", "WARNING",
                                         f"Network problem on part {seq} (attempt {attempt}/{attempts}): {exc}. Retrying.")
                    time.sleep(self._backoff(attempt))
                    continue
                shot = self.ctl.capture_diagnostics("network")
                self.store.set_part_status(pid, PartStatus.FAILED, error_code="NETWORK", error_message=str(exc),
                                           event=("STEP_RETRY", "ERROR", f"Part {seq} failed after {attempts} network attempts: {exc}", shot))
                return PartOutcome(ok=False, retryable=True)
        return PartOutcome(ok=False, retryable=True)

    def _await_generation(self, job: dict[str, Any], part: dict[str, Any]) -> PartOutcome | None:
        """SUBMITTED/GENERATING -> READY (placed in the project). Returns an outcome on failure."""
        pid, seq, ref = part["part_id"], part["sequence"], part["vids_part_ref"]
        deadline = time.monotonic() + self.cfg.timeouts_seconds.generation
        interval = self.cfg.timeouts_seconds.generation_poll_interval
        quota_seen = False
        while time.monotonic() < deadline:
            result = self.ctl.poll_generation(ref)
            state = result.state
            if state is GenerationState.GENERATING:
                if self.store.get_part(pid)["status"] == PartStatus.SUBMITTED.value:
                    self.store.set_part_status(pid, PartStatus.GENERATING,
                                               event=("GENERATION_STARTED", "INFO", f"Part {seq} generating"))
            elif state is GenerationState.READY:
                try:
                    self.ctl.place_generated_clip(ref)
                except UIElementMissingError as exc:
                    self.store.set_part_status(pid, PartStatus.FAILED, error_code="UI_MISSING", error_message=str(exc),
                                               event=("UI_ELEMENT_MISSING", "ERROR", str(exc), exc.screenshot))
                    raise PauseJob(f"Part {seq}: {exc}. The job is paused.")
                self.store.set_part_status(pid, PartStatus.READY, generated_at=now(),
                                           event=("GENERATION_READY", "INFO", f"Part {seq} generated and placed"))
                if quota_seen:
                    self.quota_hit = True
                return None
            elif state is GenerationState.FAILED:
                code = result.error_code or "GEN_FAILED"
                self.store.set_part_status(pid, PartStatus.FAILED, error_code=code,
                                           error_message=result.message or "Generation failed",
                                           event=("GENERATION_FAILED", "ERROR", f"Part {seq}: {result.message or 'generation failed'}"))
                return PartOutcome(ok=False, retryable=code == "GEN_FAILED")
            elif state is GenerationState.QUOTA:
                if not quota_seen:
                    self.store.add_event(job["job_id"], pid, "QUOTA_DETECTED", "WARNING",
                                         "Usage limit message shown; waiting for the current generation to finish")
                quota_seen = True
            elif state is GenerationState.NEEDS_USER:
                raise NeedsUser("Sign in to Google in the automation Chrome window, then click Resume.")
            time.sleep(interval)
        shot = self.ctl.capture_diagnostics("generation_timeout")
        if quota_seen:
            self.store.set_part_status(pid, PartStatus.BLOCKED_QUOTA, error_code="QUOTA", error_message="Usage limit reached")
            raise QuotaStop()
        self.store.set_part_status(pid, PartStatus.FAILED, error_code="TIMEOUT",
                                   error_message=f"No result after {self.cfg.timeouts_seconds.generation} s",
                                   event=("GENERATION_TIMEOUT", "ERROR",
                                          f"Part {seq}: no result after {self.cfg.timeouts_seconds.generation} s", shot))
        return PartOutcome(ok=False, retryable=True)

    # ----- downloads and exports -------------------------------------------------------------

    def _fetch(self, job: dict[str, Any], part: dict[str, Any] | None, trigger: Callable[[Path], None],
               base_name: str, start_timeout: float) -> tuple[Path, str]:
        """Trigger a download into staging, wait, verify (DL2), move without overwrite (DL5).

        Returns (saved path, Chrome's original filename) (DL4).
        """
        staging = self._staging(job)
        staging.mkdir(parents=True, exist_ok=True)
        orphans = self.cfg.paths.download_staging_dir / "_orphans"
        for moved in downloads.move_leftovers(staging, orphans):
            self.store.add_event(job["job_id"], part["part_id"] if part else None, "ORPHAN_DOWNLOAD", "WARNING",
                                 f"Leftover download moved to {moved.parent.name}/{moved.name}")
        attempts = self.cfg.retries.download_max_attempts
        last_error = "download failed"
        for attempt in range(1, attempts + 1):
            before = downloads.snapshot(staging)
            trigger(staging)
            try:
                src = downloads.wait_for_download(staging, before, self.cfg, start_timeout=start_timeout)
            except DownloadError as exc:
                last_error = str(exc)
                self.store.add_event(job["job_id"], part["part_id"] if part else None, "DOWNLOAD_RETRY", "WARNING",
                                     f"{exc} (attempt {attempt}/{attempts})")
                continue
            check = downloads.verify_video(src, self.cfg)
            if not check.ok:
                last_error = f"Downloaded file rejected: {check.reason}"
                self.store.add_event(job["job_id"], part["part_id"] if part else None, "DOWNLOAD_RETRY", "WARNING",
                                     f"{last_error} (attempt {attempt}/{attempts})")
                downloads.move_leftovers(staging, orphans)
                continue
            fault = getattr(self.ctl, "fault_before_save", None)
            try:
                if fault:
                    fault(part["sequence"] if part else None)
                saved = downloads.safe_move(src, Path(job["output_dir"]), base_name, src.suffix.lower(),
                                            attempts=self.cfg.retries.file_move_attempts,
                                            interval=self.cfg.retries.file_move_interval_seconds)
                return saved, src.name
            except OSError as exc:
                raise DiskError(Path(job["output_dir"]), exc) from exc
        raise DownloadError(last_error)

    def _record_rename(self, job: dict[str, Any], part_id: str | None, final: Path, base_name: str) -> None:
        if final.stem != base_name:
            self.store.add_event(job["job_id"], part_id, "DUPLICATE_RENAMED", "WARNING",
                                 f"{base_name}{final.suffix} already existed; saved as {final.name}")

    def _download_part(self, job: dict[str, Any], part: dict[str, Any]) -> PartOutcome:
        pid, seq = part["part_id"], part["sequence"]
        base = render_names(job, self.store.get_parts(job["job_id"]))[pid]
        self.store.update_part(pid, download_started_at=now(), event=("DOWNLOAD_STARTED", "INFO", f"Downloading part {seq}"))
        project_ref = part["vids_project_ref"] or job["vids_project_ref"]

        def trigger(staging: Path) -> None:
            if project_ref:
                self.ctl.open_project(project_ref)
            self.ctl.download_clip(part["vids_part_ref"] or "", staging)

        try:
            final, original = self._fetch(job, part, trigger, base, self.cfg.timeouts_seconds.export_render)
        except DiskError as exc:
            self.store.add_event(job["job_id"], pid, "DISK_ERROR", "ERROR", f"Can't save to {exc.path}: {exc}")
            raise NeedsUser(f"Can't save part {seq} to {exc.path}: {exc.os_error.strerror or exc}. Free space or fix permissions, then Resume.")
        except DownloadError as exc:
            shot = self.ctl.capture_diagnostics("download_failed")
            self.store.set_part_status(pid, PartStatus.FAILED, error_code="DOWNLOAD", error_message=str(exc),
                                       event=("DOWNLOAD_FAILED", "ERROR", f"Part {seq}: {exc}. Retry re-downloads without regenerating.", shot))
            return PartOutcome(ok=False)
        except UIElementMissingError as exc:
            self.store.set_part_status(pid, PartStatus.FAILED, error_code="UI_MISSING", error_message=str(exc),
                                       event=("UI_ELEMENT_MISSING", "ERROR", str(exc), exc.screenshot))
            raise PauseJob(f"Part {seq}: {exc}. The job is paused.")
        self.store.set_part_status(pid, PartStatus.DOWNLOADED, output_path=str(final), original_filename=original,
                                   output_bytes=final.stat().st_size, error_code=None, error_message=None,
                                   event=("DOWNLOAD_VERIFIED", "INFO", f"Part {seq} saved as {final.name}"))
        self._record_rename(job, pid, final, base)
        return PartOutcome(ok=True)

    def _final_export_due(self, job_id: str) -> bool:
        job = self._job(job_id)
        if job["final_export_status"] not in (FinalExportStatus.PENDING.value, FinalExportStatus.FAILED.value,
                                             FinalExportStatus.PARTIAL_DOWNLOADED.value):
            return False
        parts = self.store.get_parts(job_id)
        return bool(parts) and all(self._part_succeeded(job, p) for p in parts)

    def _export(self, job_id: str, partial: bool) -> bool:
        job = self._job(job_id)
        base = render_name(job["partial_template" if partial else "final_template"], job["project_slug"])
        if not partial:
            self.store.update_job(job_id, final_export_status=FinalExportStatus.EXPORTING,
                                  event=("EXPORT_STARTED", "INFO", "Exporting the combined video"))
        else:
            self.store.add_event(job_id, None, "EXPORT_STARTED", "INFO", "Exporting the partial video (usage limit)")
        self.ctl.set_context(job_id, None)

        def trigger(staging: Path) -> None:
            self.ctl.export_project(job["vids_project_ref"] or "", staging)

        try:
            final, original = self._fetch(job, None, trigger, base, self.cfg.timeouts_seconds.export_render)
        except (DownloadError, UIElementMissingError) as exc:
            shot = self.ctl.capture_diagnostics("export_failed")
            if partial:
                self.store.add_event(job_id, None, "EXPORT_FAILED", "ERROR", f"Partial export failed: {exc}", shot)
            else:
                self.store.update_job(job_id, final_export_status=FinalExportStatus.FAILED,
                                      event=("EXPORT_FAILED", "ERROR", f"Combined export failed: {exc}", shot))
            return False
        except DiskError as exc:
            self.store.add_event(job_id, None, "DISK_ERROR", "ERROR", f"Can't save to {exc.path}: {exc}")
            if not partial:
                self.store.update_job(job_id, final_export_status=FinalExportStatus.PENDING)
            raise NeedsUser(f"Can't save the export to {exc.path}: {exc.os_error.strerror or exc}. Free space or fix permissions, then Resume.")
        if partial:
            self.store.update_job(job_id, final_export_status=FinalExportStatus.PARTIAL_DOWNLOADED,
                                  partial_output_path=str(final),
                                  event=("EXPORT_VERIFIED", "INFO", f"Partial video saved as {final.name}"))
        else:
            self.store.update_job(job_id, final_export_status=FinalExportStatus.DOWNLOADED, final_output_path=str(final),
                                  final_original_filename=original,
                                  event=("EXPORT_VERIFIED", "INFO", f"Combined video saved as {final.name}"))
        self._record_rename(job, None, final, base)
        return True

    # ----- stopping points ---------------------------------------------------------------------

    def _quota_stop(self, job_id: str) -> str:
        """04 §7: submit nothing more, save what's done, block the rest."""
        self.store.add_event(job_id, None, "QUOTA_DETECTED", "WARNING", "Usage limit reached; no new generations will start")
        job = self._job(job_id)
        for part in self.store.get_parts(job_id):
            if part["status"] == PartStatus.PENDING.value:
                self.store.set_part_status(part["part_id"], PartStatus.BLOCKED_QUOTA, error_code="QUOTA",
                                           error_message="Waiting for the usage limit to reset")
        try:
            if self._needs_clip_download(job):
                for part in self.store.get_parts(job_id):
                    if part["status"] == PartStatus.READY.value:
                        self._download_part(job, part)
            elif any(p["status"] == PartStatus.READY.value for p in self.store.get_parts(job_id)) and job["vids_project_ref"]:
                self._export(job_id, partial=True)  # DEC-24
        except NeedsUser as exc:
            return self._wait_for_user(job_id, str(exc), None)
        self.store.set_job_status(job_id, JobStatus.BLOCKED_QUOTA, last_error=QUOTA_MESSAGE)
        return JobStatus.BLOCKED_QUOTA.value

    def _wait_for_user(self, job_id: str, message: str, step: str | None) -> str:
        self.store.set_job_status(job_id, JobStatus.WAITING_FOR_USER, last_error=message,
                                  event=(step or "PAUSED", "WARNING", message))
        return JobStatus.WAITING_FOR_USER.value

    def _pause(self, job_id: str, message: str, step: str = "PAUSED") -> str:
        self.store.set_job_status(job_id, JobStatus.PAUSED, last_error=message, event=(step, "WARNING", message))
        return JobStatus.PAUSED.value

    def _final_status(self, job_id: str) -> str:
        job = self._job(job_id)
        parts = self.store.get_parts(job_id)
        final_ok = job["final_export_status"] in (FinalExportStatus.NOT_APPLICABLE.value,
                                                  FinalExportStatus.NOT_SUPPORTED.value,
                                                  FinalExportStatus.DOWNLOADED.value)
        if parts and all(self._part_succeeded(job, p) for p in parts) and final_ok:
            status = JobStatus.COMPLETED
        else:
            status = JobStatus.COMPLETED_WITH_ERRORS
        errors = [f"Part {p['sequence']}: {p['error_message']}" for p in parts if p["status"] == PartStatus.FAILED.value]
        self.store.set_job_status(job_id, status, last_error="; ".join(errors)[:500] or None)
        return status.value

    def _finish(self, job_id: str) -> None:
        from automation.report import write_reports
        try:
            json_path, _ = write_reports(self.store, job_id)
            self.store.add_event(job_id, None, "REPORT_WRITTEN", "INFO", f"Report written: {json_path.name}")
        except OSError as exc:
            self.store.add_event(job_id, None, "REPORT_WRITTEN", "WARNING", f"Report could not be written: {exc}")
        job = self._job(job_id)
        self.store.add_event(job_id, None, "RUN_FINISHED", "INFO", f"Run finished: {job['overall_status']}")

    # ----- recovery ------------------------------------------------------------------------------

    def reconcile(self, job_id: str, startup: bool) -> None:
        """04 §6: check the DB against the files before acting. Never regenerates verified work."""
        job = self._job(job_id)
        if startup and job["overall_status"] == JobStatus.RUNNING.value:
            self.store.set_job_status(job_id, JobStatus.PAUSED, last_error="Recovered after an unexpected stop. Click Resume.",
                                      event=("RECONCILED", "WARNING", "Recovered after unexpected stop"))
        for part in self.store.get_parts(job_id):
            pid, status = part["part_id"], PartStatus(part["status"])
            if status is PartStatus.DOWNLOADED:
                path = Path(part["output_path"]) if part["output_path"] else None
                if path is None or not downloads.verify_video(path, self.cfg).ok:
                    if part["generated_at"] and (part["vids_project_ref"] or job["vids_project_ref"]):
                        self.store.set_part_status(pid, PartStatus.READY, reconcile=True, output_path=None,
                                                   event=("RECONCILED", "WARNING", f"Part {part['sequence']} output file is missing; it will be re-downloaded, not regenerated"))
                    else:
                        self.store.set_part_status(pid, PartStatus.FAILED, reconcile=True, error_code="OUTPUT_MISSING",
                                                   error_message="The saved file is missing",
                                                   event=("RECONCILED", "WARNING", f"Part {part['sequence']} output file is missing"))
            elif status is PartStatus.PENDING and part["submit_attempted_at"] and not part["vids_part_ref"]:
                self.store.set_part_status(pid, PartStatus.FAILED, error_code="UNCERTAIN_SUBMIT",
                                           error_message="A generation may have started before the app stopped. Check the Vids project, then use Retry.",
                                           event=("RECONCILED", "WARNING", f"Part {part['sequence']}: submit was interrupted; not resubmitting automatically"))
        staging = self._staging(job)
        orphans = self.cfg.paths.download_staging_dir / "_orphans"
        for moved in downloads.move_leftovers(staging, orphans):
            self.store.add_event(job_id, None, "ORPHAN_DOWNLOAD", "WARNING", f"Leftover download moved to _orphans/{moved.name}")
        job = self._job(job_id)
        if job["final_export_status"] == FinalExportStatus.EXPORTING.value:
            self.store.update_job(job_id, final_export_status=FinalExportStatus.PENDING,
                                  event=("RECONCILED", "WARNING", "Interrupted export will run again (no generation used)"))
        elif job["final_export_status"] == FinalExportStatus.DOWNLOADED.value:
            final = Path(job["final_output_path"]) if job["final_output_path"] else None
            if final is None or not downloads.verify_video(final, self.cfg).ok:
                self.store.update_job(job_id, final_export_status=FinalExportStatus.PENDING, final_output_path=None,
                                      event=("RECONCILED", "WARNING", "Combined video is missing; it will be exported again"))

    def retry_failed(self, job_id: str) -> int:
        """DEC-22: FAILED parts go back to READY (re-download) or PENDING; DOWNLOADED parts are untouched."""
        count = 0
        for part in self.store.get_parts(job_id):
            if part["status"] != PartStatus.FAILED.value:
                continue
            target = PartStatus.READY if part["generated_at"] and (part["vids_part_ref"] or part["vids_project_ref"]) else PartStatus.PENDING
            self.store.set_part_status(part["part_id"], target, retry_count=part["retry_count"] + 1,
                                       submit_attempted_at=None, error_code=None, error_message=None,
                                       event=("RETRY_REQUESTED", "INFO", f"Retry part {part['sequence']} ({'re-download' if target is PartStatus.READY else 'regenerate'})"))
            count += 1
        job = self._job(job_id)
        if job["final_export_status"] == FinalExportStatus.FAILED.value:
            self.store.update_job(job_id, final_export_status=FinalExportStatus.PENDING)
            count += 1
        self.store.update_job(job_id, consecutive_failures=0)
        return count
