"""Typed exceptions. The workflow maps each one to an ER row in 01_REQUIREMENTS.md (03 §4)."""

from pathlib import Path


class ConfigError(Exception):
    """config.json or vids_selectors.json is invalid, or a required setting is missing."""


class BrowserStartError(Exception):
    """The automation Chrome could not be started, or Selenium could not attach to it."""


class ValidationError(Exception):
    """Job or part input failed V1-V5."""


class LoginRequiredError(Exception):
    """Sign-in page, expired session, CAPTCHA or 2FA (ER3)."""


class QuotaReachedError(Exception):
    """A usage or limit message is visible (ER1)."""


class UIElementMissingError(Exception):
    """A selector key matched nothing visible in time (ER4)."""

    def __init__(self, selector_key: str, step: str, screenshot: Path | None = None, detail: str | None = None) -> None:
        super().__init__(f"UI element '{selector_key}' not found during step {step}" + (f". {detail}" if detail else ""))
        self.selector_key = selector_key
        self.step = step
        self.screenshot = screenshot


class UploadError(Exception):
    """A reference image could not be attached. Raised only before Generate is clicked (DEC-26)."""

    error_code = "UPLOAD"


class PromptMismatchError(UploadError):
    """The prompt read back from Vids differs from the script (05 §7). Handled like UploadError (DEC-26)."""

    error_code = "PROMPT_MISMATCH"


class GenerationFailedError(Exception):
    """Vids showed a generation error (ER5)."""


class GenerationTimeoutError(Exception):
    """Generation did not finish within timeouts_seconds.generation (ER5)."""


class NetworkError(Exception):
    """Page load or network timeout (ER2)."""


class DownloadError(Exception):
    """Download did not start, never completed, or failed verification (ER6)."""


class DiskError(Exception):
    """Disk full or permission denied while saving output (ER9)."""

    def __init__(self, path: Path, os_error: OSError) -> None:
        super().__init__(f"{os_error.strerror or os_error} at {path}")
        self.path = path
        self.os_error = os_error
