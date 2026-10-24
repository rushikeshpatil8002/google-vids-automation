"""Deterministic, scenario-driven stand-in for Google Vids (03 §5.5). Uses no network and no quota.

Counters persist in <data_dir>/fake_state.json so restart tests can prove nothing was
regenerated. Downloads write a valid tiny MP4 into the staging folder via a .crdownload
temp file, like Chrome.
"""

import errno
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from automation.controller_base import Capabilities, GenerationState, PollResult, VidsController
from automation.errors import LoginRequiredError, NetworkError, QuotaReachedError, UIElementMissingError, UploadError
from automation.validators import script_hash12

TINY_MP4_HEADER = bytes.fromhex("000000186674797069736f6d0000020069736f6d69736f32")
SUBMIT_KEYS = {"image_upload_input", "prompt_textbox", "generate_button", "generate_video_entry"}


def tiny_mp4(size: int = 10240) -> bytes:
    """A valid 'ftyp' box padded to `size` bytes (08 §2)."""
    return TINY_MP4_HEADER + b"\0" * (size - len(TINY_MP4_HEADER))


def load_scenario(source: dict[str, Any] | Path | str) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    return json.loads(Path(source).read_text(encoding="utf-8"))


class FakeVidsController(VidsController):
    def __init__(self, scenario: dict[str, Any] | Path | str, data_dir: Path, screenshots_dir: Path | None = None) -> None:
        self.scenario = load_scenario(scenario)
        self.state_file = Path(data_dir) / "fake_state.json"
        self.screenshots_dir = screenshots_dir or Path(data_dir) / "fake_screenshots"
        self.job_id: str | None = None
        self.seq: int | None = None
        self._lock = threading.Lock()
        self.started = False

    # ----- persisted counters ---------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        return {"total_generations": 0, "generations_per_part": {}, "upload_attempts": {}, "submit_calls": {},
                "download_triggers": {}, "login_checks": 0, "disk_errors": {}, "submissions": [], "refs": {}}

    def _save(self, state: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")

    def _bump(self, state: dict[str, Any], key: str) -> int:
        counts = state[key]
        counts[str(self.seq)] = counts.get(str(self.seq), 0) + 1
        return counts[str(self.seq)]

    def _limit(self, key: str) -> int:
        return int(self.scenario.get(key, {}).get(str(self.seq), 0))

    def state(self) -> dict[str, Any]:
        return self._load()

    # ----- interface ---------------------------------------------------------------------------

    def set_context(self, job_id: str | None, part_sequence: int | None) -> None:
        self.job_id, self.seq = job_id, part_sequence

    def capabilities(self) -> Capabilities:
        c = self.scenario["capabilities"]
        return Capabilities(c["multi_scene"], c["combined_export"], c["per_clip_download"],
                            c["max_reference_images"], c.get("max_prompt_chars"))

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.started = False

    def ensure_ready(self) -> None:
        with self._lock:
            state = self._load()
            state["login_checks"] += 1
            self._save(state)
        if state["login_checks"] <= int(self.scenario.get("login_required_checks", 0)):
            raise LoginRequiredError("Google sign-in is needed (simulated)")

    def _quota_reached(self, state: dict[str, Any]) -> bool:
        limit = self.scenario.get("quota_after_generations")
        return limit is not None and state["total_generations"] >= limit

    def check_quota(self) -> None:
        if self._quota_reached(self._load()):
            raise QuotaReachedError("Usage limit reached (simulated)")

    def create_project(self, name: str) -> str:
        return f"fake://project/{uuid.uuid4().hex[:10]}"

    def open_project(self, project_ref: str) -> None:
        pass

    def add_scene(self, project_ref: str) -> str:
        return f"{project_ref}#scene-{uuid.uuid4().hex[:6]}"

    def _ui_missing(self, keys: set[str]) -> None:
        missing = self.scenario.get("ui_missing")
        if missing and int(missing.get("part", -1)) == self.seq and missing.get("key") in keys:
            raise UIElementMissingError(missing["key"], "GENERATE", self.capture_diagnostics(f"missing_{missing['key']}"))

    def submit_generation(self, project_ref: str, scene_ref: str | None,
                          script_text: str, image_paths: list[Path]) -> str:
        with self._lock:
            state = self._load()
            self._ui_missing(SUBMIT_KEYS)
            if self._bump(state, "upload_attempts") <= self._limit("upload_fail_parts"):
                self._save(state)
                raise UploadError("Image upload failed (simulated)")
            if self._bump(state, "submit_calls") <= self._limit("network_error_parts"):
                self._save(state)
                raise NetworkError("Network timeout (simulated)")
            if self._quota_reached(state):
                self._save(state)
                raise QuotaReachedError("Usage limit reached (simulated)")
            attempt = self._bump(state, "generations_per_part")
            state["total_generations"] += 1
            ref = f"fake-gen-{self.seq}-{attempt}-{uuid.uuid4().hex[:6]}"
            state["refs"][ref] = {"seq": self.seq, "attempt": attempt, "started": time.time()}
            state["submissions"].append({"seq": self.seq, "sha12": script_hash12(script_text),
                                         "images": [Path(p).name for p in image_paths],
                                         "image_parent": Path(image_paths[0]).parent.name if image_paths else None})
            self._save(state)
            return ref

    def poll_generation(self, part_ref: str) -> PollResult:
        info = self._load()["refs"].get(part_ref)
        if info is None:
            return PollResult(GenerationState.FAILED, "Unknown generation", error_code="UNCERTAIN_SUBMIT")
        seq, attempt = str(info["seq"]), info["attempt"]
        if attempt <= int(self.scenario.get("timeout_generation_parts", {}).get(seq, 0)):
            return PollResult(GenerationState.GENERATING)
        if time.time() - info["started"] < float(self.scenario.get("generation_seconds", 1)):
            return PollResult(GenerationState.GENERATING)
        if attempt <= int(self.scenario.get("fail_generation_parts", {}).get(seq, 0)):
            return PollResult(GenerationState.FAILED, "Generation failed (simulated)")
        return PollResult(GenerationState.READY)

    def place_generated_clip(self, part_ref: str) -> None:
        self._ui_missing({"insert_generated_clip"})

    def _deliver(self, download_dir: Path, name: str, complete: bool) -> None:
        download_dir.mkdir(parents=True, exist_ok=True)
        temp = download_dir / f"{name}.crdownload"
        temp.write_bytes(tiny_mp4()[:4096])
        if not complete:
            return

        def finish() -> None:
            time.sleep(float(self.scenario.get("download_delay_seconds", 0.3)))
            temp.write_bytes(tiny_mp4())
            temp.replace(download_dir / name)

        threading.Thread(target=finish, daemon=True).start()

    def download_clip(self, part_ref: str, download_dir: Path) -> None:
        with self._lock:
            state = self._load()
            trigger = self._bump(state, "download_triggers")
            self._save(state)
        complete = trigger > self._limit("download_interrupt_parts")
        self._deliver(Path(download_dir), f"Fake clip {self.seq} ({trigger}).mp4", complete)

    def export_project(self, project_ref: str, download_dir: Path) -> None:
        self._deliver(Path(download_dir), f"Fake export {uuid.uuid4().hex[:6]}.mp4", True)

    def fault_before_save(self, part_sequence: int | None) -> None:
        """Test seam for ER9: raise ENOSPC on safe_move for the scenario's part, `times` times."""
        disk = self.scenario.get("disk_error")
        if not disk or part_sequence is None or int(disk.get("part", -1)) != part_sequence:
            return
        with self._lock:
            state = self._load()
            done = state["disk_errors"].get(str(part_sequence), 0)
            if done >= int(disk.get("times", 1)):
                return
            state["disk_errors"][str(part_sequence)] = done + 1
            self._save(state)
        raise OSError(errno.ENOSPC, "No space left on device (simulated)")

    def capture_diagnostics(self, label: str) -> Path | None:
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshots_dir / f"{(self.job_id or 'run')[:8]}_{self.seq or 0:02d}_{label}_{uuid.uuid4().hex[:6]}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return path
