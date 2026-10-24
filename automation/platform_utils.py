"""OS-specific helpers. Every platform difference lives in this module (CLAUDE.md §5)."""

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from automation.errors import ConfigError

CHROME_LINUX_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def os_family(platform: str | None = None) -> str:
    """'windows', 'macos' or 'linux' for a sys.platform value (default: this machine)."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return "windows"
    if platform == "darwin":
        return "macos"
    return "linux"


def chrome_candidates(
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    """Standard Chrome stable install locations, most likely first."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    family = os_family(platform)
    if family == "windows":
        exe = Path("Google", "Chrome", "Application", "chrome.exe")
        # Machine-wide installs first, then the per-user install under LOCALAPPDATA.
        variables = ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        return [Path(env[var]) / exe for var in variables if env.get(var)]
    if family == "macos":
        app = Path("Google Chrome.app", "Contents", "MacOS", "Google Chrome")
        return [Path("/Applications") / app, home / "Applications" / app]
    return [Path(found) for name in CHROME_LINUX_NAMES if (found := shutil.which(name))]


def find_chrome_binary(
    configured: Path | None = None,
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    is_file: Callable[[Path], bool] = Path.is_file,
) -> Path:
    """Return the Chrome executable: `browser.chrome_binary` if set, else the first standard location."""
    if configured is not None:
        if is_file(configured):
            return configured
        raise ConfigError(f"browser.chrome_binary does not exist: {configured}")
    for candidate in chrome_candidates(platform, env, home):
        if is_file(candidate):
            return candidate
    raise ConfigError(
        "Google Chrome was not found. Install Chrome stable, or set browser.chrome_binary "
        "in config.json to the full path of the Chrome executable."
    )


def default_chrome_user_data_dirs(
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    """The everyday Chrome profile folders, which the automation must never use (P2)."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    family = os_family(platform)
    if family == "windows":
        local = env.get("LOCALAPPDATA")
        return [Path(local, "Google", "Chrome", "User Data")] if local else []
    if family == "macos":
        return [home / "Library" / "Application Support" / "Google" / "Chrome"]
    return [home / ".config" / "google-chrome", home / ".config" / "chromium"]


def retry_on_permission_error(fn: Callable[[], Any], *, attempts: int = 10, interval: float = 0.5) -> Any:
    """Call fn, retrying on PermissionError: Windows locks files that are still being written."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(interval)
    return None


def open_folder(path: Path) -> None:
    """Open a folder in the OS file manager."""
    family = os_family()
    if family == "windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if family == "macos" else "xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def select_all_modifier() -> str:
    """Name of the Selenium Keys modifier for select-all: COMMAND on macOS, CONTROL elsewhere."""
    return "COMMAND" if os_family() == "macos" else "CONTROL"


def detached_popen_kwargs() -> dict[str, Any]:
    """Popen options so Chrome outlives this process and ignores a Ctrl+C sent to it."""
    if os_family() == "windows":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
