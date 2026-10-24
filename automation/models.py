"""Enums shared by the state store, workflow, API and UI (04 §1-§2)."""

from enum import Enum


class JobMode(str, Enum):
    SINGLE = "single"
    MULTI_PART = "multi_part"
    SEPARATE_CLIPS = "separate_clips"


class JobStatus(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    RUNNING = "RUNNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    PAUSED = "PAUSED"
    BLOCKED_QUOTA = "BLOCKED_QUOTA"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class PartStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    GENERATING = "GENERATING"
    READY = "READY"
    DOWNLOADED = "DOWNLOADED"
    FAILED = "FAILED"
    BLOCKED_QUOTA = "BLOCKED_QUOTA"


class FinalExportStatus(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    EXPORTING = "EXPORTING"
    DOWNLOADED = "DOWNLOADED"
    PARTIAL_DOWNLOADED = "PARTIAL_DOWNLOADED"
    FAILED = "FAILED"
    NOT_SUPPORTED = "NOT_SUPPORTED"


EDITABLE_JOB_STATUSES = {JobStatus.DRAFT, JobStatus.VALIDATED}
RESUMABLE_JOB_STATUSES = {JobStatus.PAUSED, JobStatus.WAITING_FOR_USER, JobStatus.BLOCKED_QUOTA}
RETRYABLE_JOB_STATUSES = {JobStatus.COMPLETED_WITH_ERRORS, JobStatus.PAUSED, JobStatus.FAILED}
TERMINAL_JOB_STATUSES = {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED}

# Limitation codes shown at review and in reports (DEC-02).
LIMITATION_MESSAGES = {
    "MULTI_SCENE_NOT_SUPPORTED": "Google Vids can't hold several scenes in one project: parts run as separate clips and no combined video is produced.",
    "COMBINED_EXPORT_NOT_SUPPORTED": "Google Vids can't export the whole project as one video: only individual clips are saved.",
    "NO_PER_CLIP_DOWNLOAD": "Google Vids can't download individual clips from a multi-part project: only the combined Final video is saved. Use 'Separate clips' mode for individual files.",
}
