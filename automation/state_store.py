"""All SQLite access: schema, CRUD, and atomic status transitions with their event (04 §3-§5).

A new connection per operation (WAL, foreign keys, synchronous=FULL) and one process-wide
write lock, so the worker thread and Flask threads never share a connection (03 §7).
"""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from automation.models import JobStatus, PartStatus

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  job_id                  TEXT PRIMARY KEY,
  project_name            TEXT NOT NULL,
  project_slug            TEXT NOT NULL UNIQUE,
  mode                    TEXT NOT NULL CHECK (mode IN ('single','multi_part','separate_clips')),
  effective_mode          TEXT,
  output_dir              TEXT NOT NULL,
  clip_template           TEXT NOT NULL,
  final_template          TEXT NOT NULL,
  partial_template        TEXT NOT NULL,
  overall_status          TEXT NOT NULL,
  vids_project_ref        TEXT,
  final_export_status     TEXT NOT NULL DEFAULT 'NOT_APPLICABLE'
                          CHECK (final_export_status IN ('NOT_APPLICABLE','PENDING','EXPORTING',
                                 'DOWNLOADED','PARTIAL_DOWNLOADED','FAILED','NOT_SUPPORTED')),
  final_output_path       TEXT,
  final_original_filename TEXT,
  partial_output_path     TEXT,
  limitations             TEXT NOT NULL DEFAULT '[]',
  consecutive_failures    INTEGER NOT NULL DEFAULT 0,
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL,
  last_error              TEXT
);
CREATE TABLE IF NOT EXISTS parts (
  part_id              TEXT PRIMARY KEY,
  job_id               TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  sequence             INTEGER NOT NULL,
  title                TEXT,
  script_path          TEXT NOT NULL,
  script_sha12         TEXT NOT NULL,
  script_chars         INTEGER NOT NULL,
  image_paths          TEXT NOT NULL,
  status               TEXT NOT NULL,
  validation_errors    TEXT NOT NULL DEFAULT '[]',
  retry_count          INTEGER NOT NULL DEFAULT 0,
  vids_project_ref     TEXT,
  vids_scene_ref       TEXT,
  vids_part_ref        TEXT,
  submit_attempted_at  TEXT,
  generated_at         TEXT,
  download_started_at  TEXT,
  output_path          TEXT,
  original_filename    TEXT,
  output_bytes         INTEGER,
  error_code           TEXT,
  error_message        TEXT,
  updated_at           TEXT NOT NULL,
  UNIQUE (job_id, sequence)
);
CREATE TABLE IF NOT EXISTS events (
  event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  job_id          TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  part_id         TEXT,
  step            TEXT NOT NULL,
  severity        TEXT NOT NULL CHECK (severity IN ('DEBUG','INFO','WARNING','ERROR')),
  message         TEXT NOT NULL,
  screenshot_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_parts_job ON parts(job_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, event_id);
"""

P = PartStatus
# 04 §3. Anything else raises InvalidTransition.
ALLOWED: dict[PartStatus, set[PartStatus]] = {
    P.PENDING: {P.SUBMITTED, P.FAILED, P.BLOCKED_QUOTA},
    P.SUBMITTED: {P.GENERATING, P.READY, P.FAILED, P.BLOCKED_QUOTA},
    P.GENERATING: {P.READY, P.FAILED, P.BLOCKED_QUOTA},
    P.READY: {P.DOWNLOADED, P.FAILED},
    P.FAILED: {P.READY, P.PENDING},
    P.BLOCKED_QUOTA: {P.PENDING},
    P.DOWNLOADED: set(),
}
JOB_JSON = {"limitations"}
PART_JSON = {"image_paths", "validation_errors"}


class InvalidTransition(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


Event = tuple[str, str, str] | tuple[str, str, str, str | None]  # (step, severity, message[, screenshot])


class Store:
    _write_lock = threading.RLock()

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as con:
            con.executescript(SCHEMA)
            if con.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                con.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))

    # ----- connections -------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=FULL")
        return con

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.close()

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        con = self._connect()
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    @staticmethod
    def _decode(row: sqlite3.Row | None, json_cols: set[str]) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for col in json_cols & data.keys():
            data[col] = json.loads(data[col]) if data[col] else []
        return data

    @staticmethod
    def _encode(fields: dict[str, Any], json_cols: set[str]) -> dict[str, Any]:
        out = {}
        for key, value in fields.items():
            if key in json_cols:
                value = json.dumps(value)
            elif hasattr(value, "value"):
                value = value.value
            elif isinstance(value, Path):
                value = str(value)
            out[key] = value
        return out

    @staticmethod
    def _insert_event(con: sqlite3.Connection, job_id: str, part_id: str | None, event: Event) -> None:
        step, severity, message, *rest = event
        con.execute("INSERT INTO events (ts, job_id, part_id, step, severity, message, screenshot_path) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (now(), job_id, part_id, step, severity, message, str(rest[0]) if rest and rest[0] else None))

    # ----- jobs ----------------------------------------------------------------------------

    def create_job(self, *, project_name: str, project_slug: str, mode: str, output_dir: Path,
                   clip_template: str, final_template: str, partial_template: str) -> str:
        job_id = uuid.uuid4().hex
        ts = now()
        with self._tx() as con:
            con.execute(
                "INSERT INTO jobs (job_id, project_name, project_slug, mode, output_dir, clip_template, "
                "final_template, partial_template, overall_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, project_name, project_slug, mode, str(output_dir), clip_template, final_template,
                 partial_template, JobStatus.DRAFT.value, ts, ts))
            self._insert_event(con, job_id, None, ("JOB_CREATED", "INFO", f"Job created ({mode})"))
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        rows = self._read("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        return self._decode(rows[0] if rows else None, JOB_JSON)

    def list_jobs(self) -> list[dict[str, Any]]:
        rows = self._read("SELECT * FROM jobs ORDER BY created_at DESC")
        return [self._decode(r, JOB_JSON) for r in rows]

    def slugs(self) -> set[str]:
        return {r[0] for r in self._read("SELECT project_slug FROM jobs")}

    def update_job(self, job_id: str, event: Event | None = None, **fields: Any) -> None:
        fields = self._encode(fields, JOB_JSON)
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as con:
            con.execute(f"UPDATE jobs SET {cols} WHERE job_id=?", (*fields.values(), job_id))
            if event:
                self._insert_event(con, job_id, None, event)

    def set_job_status(self, job_id: str, status: JobStatus, event: Event | None = None, **fields: Any) -> None:
        self.update_job(job_id, event=event, overall_status=status, **fields)

    def delete_job(self, job_id: str) -> None:
        with self._tx() as con:
            con.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))

    # ----- parts ---------------------------------------------------------------------------

    def add_part(self, job_id: str, *, title: str | None, script_path: Path, script_sha12: str,
                 script_chars: int, image_paths: list[str], sequence: int | None = None) -> str:
        part_id = uuid.uuid4().hex
        with self._tx() as con:
            if sequence is None:
                sequence = con.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM parts WHERE job_id=?",
                                       (job_id,)).fetchone()[0]
            con.execute(
                "INSERT INTO parts (part_id, job_id, sequence, title, script_path, script_sha12, script_chars, "
                "image_paths, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (part_id, job_id, sequence, title, str(script_path), script_sha12, script_chars,
                 json.dumps([str(p) for p in image_paths]), PartStatus.PENDING.value, now()))
            self._insert_event(con, job_id, part_id, ("PART_ADDED", "INFO", f"Part {sequence} added"))
        return part_id

    def get_part(self, part_id: str) -> dict[str, Any] | None:
        rows = self._read("SELECT * FROM parts WHERE part_id=?", (part_id,))
        return self._decode(rows[0] if rows else None, PART_JSON)

    def get_parts(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._read("SELECT * FROM parts WHERE job_id=? ORDER BY sequence", (job_id,))
        return [self._decode(r, PART_JSON) for r in rows]

    def update_part(self, part_id: str, event: Event | None = None, **fields: Any) -> None:
        """Change part fields without a status change."""
        if "status" in fields:
            raise InvalidTransition("Use set_part_status to change status")
        self._write_part(part_id, None, event, fields)

    def set_part_status(self, part_id: str, new: PartStatus, event: Event | None = None,
                        reconcile: bool = False, **fields: Any) -> None:
        """Atomic status change + fields + event (04 §5). reconcile=True also allows DOWNLOADED -> READY/FAILED (04 §6)."""
        self._write_part(part_id, PartStatus(new), event, fields, reconcile)

    def _write_part(self, part_id: str, new: PartStatus | None, event: Event | None,
                    fields: dict[str, Any], reconcile: bool = False) -> None:
        fields = self._encode(fields, PART_JSON)
        with self._tx() as con:
            row = con.execute("SELECT job_id, status FROM parts WHERE part_id=?", (part_id,)).fetchone()
            if row is None:
                raise KeyError(part_id)
            if new is not None:
                old = PartStatus(row["status"])
                allowed = new in ALLOWED[old] or (reconcile and old is P.DOWNLOADED and new in (P.READY, P.FAILED))
                if not allowed and old is not new:
                    raise InvalidTransition(f"{old.value} -> {new.value} is not allowed")
                fields["status"] = new.value
            fields["updated_at"] = now()
            cols = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE parts SET {cols} WHERE part_id=?", (*fields.values(), part_id))
            if event:
                self._insert_event(con, row["job_id"], part_id, event)
            con.execute("UPDATE jobs SET updated_at=? WHERE job_id=?", (now(), row["job_id"]))

    def remove_part(self, part_id: str) -> None:
        part = self.get_part(part_id)
        if part is None:
            raise KeyError(part_id)
        with self._tx() as con:
            con.execute("DELETE FROM parts WHERE part_id=?", (part_id,))
            self._renumber(con, part["job_id"])
            self._insert_event(con, part["job_id"], None, ("PART_REMOVED", "INFO", f"Part {part['sequence']} removed"))

    def reorder_parts(self, job_id: str, order: list[str]) -> None:
        current = {p["part_id"] for p in self.get_parts(job_id)}
        if set(order) != current or len(order) != len(current):
            raise ValueError("Reorder list must contain every part of the job exactly once")
        with self._tx() as con:
            # Negative first, so UNIQUE(job_id, sequence) never collides mid-update (04 §4).
            con.execute("UPDATE parts SET sequence = -sequence WHERE job_id=?", (job_id,))
            for index, part_id in enumerate(order, start=1):
                con.execute("UPDATE parts SET sequence=?, updated_at=? WHERE part_id=?", (index, now(), part_id))
            self._insert_event(con, job_id, None, ("PARTS_REORDERED", "INFO", "Parts reordered"))

    @staticmethod
    def _renumber(con: sqlite3.Connection, job_id: str) -> None:
        ids = [r[0] for r in con.execute("SELECT part_id FROM parts WHERE job_id=? ORDER BY sequence", (job_id,))]
        con.execute("UPDATE parts SET sequence = -sequence WHERE job_id=?", (job_id,))
        for index, part_id in enumerate(ids, start=1):
            con.execute("UPDATE parts SET sequence=? WHERE part_id=?", (index, part_id))

    # ----- events --------------------------------------------------------------------------

    def add_event(self, job_id: str, part_id: str | None, step: str, severity: str, message: str,
                  screenshot_path: Path | str | None = None) -> None:
        with self._tx() as con:
            self._insert_event(con, job_id, part_id, (step, severity, message, screenshot_path))

    def events(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._read("SELECT * FROM events WHERE job_id=? ORDER BY event_id DESC LIMIT ?", (job_id, limit))
        return [dict(r) for r in rows]

    def all_events(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._read("SELECT * FROM events WHERE job_id=? ORDER BY event_id", (job_id,))]
