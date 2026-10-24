"""Download detection, verification and safe moves (DEC-16, DL1-DL5, ER6)."""

import logging
import shutil
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from automation.config import Config
from automation.errors import DiskError, DownloadError
from automation.platform_utils import retry_on_permission_error

log = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    ok: bool
    reason: str
    size: int = 0


def _is_temp(path: Path, cfg: Config) -> bool:
    return path.suffix.lower() in cfg.downloads.temp_suffixes


def _visible_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")]


def snapshot(folder: Path) -> set[str]:
    """Names currently in the staging folder."""
    return {p.name for p in _visible_files(folder)}


def move_leftovers(staging: Path, orphans: Path) -> list[Path]:
    """Move files left in staging into _orphans/ before a new download (DEC-16 step 2). Never deletes."""
    moved = []
    for src in _visible_files(staging):
        orphans.mkdir(parents=True, exist_ok=True)
        moved.append(safe_move(src, orphans, src.stem, src.suffix))
    return moved


def wait_for_download(folder: Path, before: set[str], cfg: Config,
                      start_timeout: float | None = None, complete_timeout: float | None = None) -> Path:
    """Wait for one new, complete, size-stable file in `folder` (DEC-16 step 4). Raises DownloadError."""
    poll = cfg.downloads.poll_interval_seconds
    start_deadline = time.monotonic() + (start_timeout or cfg.timeouts_seconds.download_start)
    while not (snapshot(folder) - before):
        if time.monotonic() > start_deadline:
            raise DownloadError("The download did not start")
        time.sleep(poll)

    complete_deadline = time.monotonic() + (complete_timeout or cfg.timeouts_seconds.download_complete)
    last_size, seen = -1, 0
    while time.monotonic() < complete_deadline:
        new = [p for p in _visible_files(folder) if p.name not in before]
        temps = [p for p in new if _is_temp(p, cfg)]
        done = [p for p in new if not _is_temp(p, cfg)]
        if done and not temps:
            size = done[0].stat().st_size
            # Complete when the same non-zero size is seen on N consecutive polls.
            seen = seen + 1 if size == last_size and size > 0 else 1
            last_size = size
            if seen >= cfg.downloads.stable_polls_required:
                return done[0]
        else:
            last_size, seen = -1, 0
        time.sleep(poll)
    raise DownloadError("The download did not complete (temporary file still present)")


def verify_video(path: Path, cfg: Config) -> VerifyResult:
    """DL2: exists, accepted extension, at least min_video_bytes, MP4 'ftyp' signature."""
    if not path.is_file():
        return VerifyResult(False, "file missing")
    size = path.stat().st_size
    if path.suffix.lower() not in cfg.downloads.accepted_extensions:
        return VerifyResult(False, f"unexpected extension {path.suffix}", size)
    if size < cfg.limits.min_video_bytes:
        return VerifyResult(False, f"too small ({size} bytes)", size)
    with path.open("rb") as f:
        head = f.read(12)
    if head[4:8] != b"ftyp":
        return VerifyResult(False, "not an MP4 container (no ftyp box)", size)
    return VerifyResult(True, "ok", size)


def mp4_duration_seconds(path: Path) -> float | None:
    """Movie duration from the moov/mvhd box, or None if it can't be read."""
    data = path.read_bytes()

    def boxes(start: int, end: int):
        i = start
        while i + 8 <= end:
            size, kind = struct.unpack(">I4s", data[i:i + 8])
            header = 8
            if size == 1:
                size, header = struct.unpack(">Q", data[i + 8:i + 16])[0], 16
            elif size == 0:
                size = end - i
            if size < header:
                return
            yield kind, i + header, i + size
            i += size

    for kind, body, end in boxes(0, len(data)):
        if kind == b"moov":
            for kind2, body2, _ in boxes(body, end):
                if kind2 == b"mvhd":
                    if data[body2] == 1:
                        scale, duration = struct.unpack(">IQ", data[body2 + 20:body2 + 32])
                    else:
                        scale, duration = struct.unpack(">II", data[body2 + 12:body2 + 20])
                    return duration / scale if scale else None
    return None


def free_target(out_dir: Path, base_name: str, ext: str) -> Path:
    """First name among base.ext, base_v2.ext, base_v3.ext, ... that doesn't exist (DL5). Case-insensitive."""
    existing = {p.name.lower() for p in out_dir.iterdir()} if out_dir.exists() else set()
    candidate, n = f"{base_name}{ext}", 1
    while candidate.lower() in existing:
        n += 1
        candidate = f"{base_name}_v{n}{ext}"
    return out_dir / candidate


def safe_move(src: Path, out_dir: Path, base_name: str, ext: str | None = None,
              attempts: int = 10, interval: float = 0.5) -> Path:
    """Move src into out_dir under a free name; never overwrites (DL5). Raises DiskError on OS errors."""
    ext = ext if ext is not None else src.suffix
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        size = src.stat().st_size
        target = free_target(out_dir, base_name, ext)
        retry_on_permission_error(lambda: shutil.move(str(src), str(target)), attempts=attempts, interval=interval)
    except OSError as exc:
        raise DiskError(out_dir, exc) from exc
    if target.stat().st_size != size:
        raise DownloadError(f"Size changed while moving {target.name}")
    return target
