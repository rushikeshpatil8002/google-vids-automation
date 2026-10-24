"""One background thread runs one job at a time; commands arrive through a queue (DEC-10, 03 §7)."""

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from automation.controller_base import VidsController
from automation.workflow import Workflow

log = logging.getLogger(__name__)


@dataclass
class Current:
    job_id: str | None = None
    part_id: str | None = None
    step: str | None = None
    started_at: float | None = None
    part_started_at: float | None = None


class Worker:
    def __init__(self, workflow_factory: Callable[[Callable], Workflow]) -> None:
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._current = Current()
        self._busy_job: str | None = None
        self.workflow = workflow_factory(self._on_step)
        self._thread = threading.Thread(target=self._loop, name="vids-worker", daemon=True)
        self._thread.start()

    @property
    def controller(self) -> VidsController:
        return self.workflow.ctl

    def _on_step(self, job_id: str | None, part_id: str | None, step: str) -> None:
        with self._lock:
            if part_id != self._current.part_id:
                self._current.part_started_at = time.time() if part_id else None
            self._current.job_id, self._current.part_id, self._current.step = job_id, part_id, step

    def submit(self, command: str, job_id: str) -> None:
        """START, RESUME or RETRY_FAILED; STOP sets the safe-stop flag immediately (DEC-15)."""
        if command == "STOP":
            self._stop.set()
            return
        with self._lock:
            self._busy_job = job_id
        self._queue.put((command, job_id))

    def busy_job(self) -> str | None:
        with self._lock:
            return self._busy_job

    def current(self) -> Current:
        with self._lock:
            return Current(**vars(self._current))

    def _loop(self) -> None:
        while True:
            command, job_id = self._queue.get()
            self._stop.clear()
            with self._lock:
                self._current = Current(job_id=job_id, step=command, started_at=time.time())
            try:
                if command == "RETRY_FAILED":
                    self.workflow.retry_failed(job_id)
                self.workflow.run_job(job_id, self._stop, resume=command != "START")
            except Exception:  # keep the worker alive; the job is reconciled on next start/resume
                log.exception("Worker crashed on %s", command, extra={"job": job_id[:8], "step": command})
                try:
                    self.workflow.store.set_job_status(job_id, "PAUSED", last_error="Unexpected error; see logs, then Resume.",
                                                       event=("PAUSED", "ERROR", "Unexpected error in the worker"))
                except Exception:
                    log.exception("Could not record the worker error")
            finally:
                with self._lock:
                    self._busy_job = None
                    self._current = Current()
                self._queue.task_done()

    def wait_idle(self, timeout: float = 60) -> bool:
        """Tests: block until the queue is drained."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.05)
        return False
