"""The controller interface shared by the live Selenium controller and the fake (03 §4)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


@dataclass(frozen=True)
class Capabilities:
    multi_scene: bool
    combined_export: bool
    per_clip_download: bool
    max_reference_images: int
    max_prompt_chars: int | None


class GenerationState(Enum):
    GENERATING = "GENERATING"
    READY = "READY"
    FAILED = "FAILED"
    QUOTA = "QUOTA"
    NEEDS_USER = "NEEDS_USER"


@dataclass
class PollResult:
    state: GenerationState
    message: str | None = None  # user-safe, never script text
    error_code: str | None = None  # e.g. UNCERTAIN_SUBMIT when the clip can't be recovered (DEC-29)


class VidsController(ABC):
    """Everything the workflow needs from Google Vids. Implementations raise the typed errors in errors.py."""

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def start(self) -> None:
        """Attach to or launch the browser."""

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def ensure_ready(self) -> None:
        """Signed in and Vids reachable; raises LoginRequiredError."""

    @abstractmethod
    def check_quota(self) -> None:
        """Raises QuotaReachedError if a limit message is visible."""

    @abstractmethod
    def create_project(self, name: str) -> str:
        """Create a new Vids project; returns its ref (editor URL without query)."""

    @abstractmethod
    def open_project(self, project_ref: str) -> None: ...

    @abstractmethod
    def add_scene(self, project_ref: str) -> str:
        """multi_part only: add a scene after the last one; returns its ref."""

    @abstractmethod
    def submit_generation(self, project_ref: str, scene_ref: str | None,
                          script_text: str, image_paths: list[Path]) -> str:
        """Attach images, enter the prompt, click Generate; returns the part ref.

        Raises UploadError/PromptMismatchError (before Generate is clicked) or QuotaReachedError.
        """

    @abstractmethod
    def poll_generation(self, part_ref: str) -> PollResult: ...

    @abstractmethod
    def place_generated_clip(self, part_ref: str) -> None:
        """Insert the generated clip into the project timeline."""

    @abstractmethod
    def download_clip(self, part_ref: str, download_dir: Path) -> None:
        """TRIGGER the clip download into download_dir; downloads.py waits for it."""

    @abstractmethod
    def export_project(self, project_ref: str, download_dir: Path) -> None:
        """TRIGGER the full-project MP4 export into download_dir."""

    @abstractmethod
    def capture_diagnostics(self, label: str) -> Path | None: ...

    def set_context(self, job_id: str | None, part_sequence: int | None) -> None:
        """Which job/part the next calls are for (diagnostic names, fake scenarios)."""
