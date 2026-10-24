import json
import threading
from pathlib import Path

import pytest

from automation.config import load_config
from automation.fake_controller import FakeVidsController
from automation.jobs import JobService
from automation.state_store import Store
from automation.workflow import Workflow

REPO = Path(__file__).resolve().parent.parent
SCENARIOS = REPO / "tests" / "scenarios"
SAMPLE = REPO / "input" / "Sample_Project"

FAST = {
    "timeouts_seconds": {"page_load": 1, "element": 1, "upload": 1, "generation": 2, "generation_poll_interval": 0.05,
                         "download_start": 3, "download_complete": 3, "export_render": 3},
    "downloads": {"poll_interval_seconds": 0.05},
    "retries": {"backoff_seconds": [0.0], "file_move_interval_seconds": 0.01},
}


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@pytest.fixture
def make_config(tmp_path):
    """Load the config with `overrides` written to a temp config.json, rooted at tmp_path."""

    def _make(overrides=None):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(overrides or {}), encoding="utf-8")
        return load_config(path, root=tmp_path)

    return _make


def scenario(name: str, **overrides) -> dict:
    data = json.loads((SCENARIOS / f"{name}.json").read_text(encoding="utf-8"))
    data.update({"generation_seconds": 0, "download_delay_seconds": 0.05})
    data.update(overrides)
    return data


class Env:
    """An isolated project root with store, job service, fake controller and workflow."""

    def __init__(self, tmp_path: Path, make_config, scenario_name: str = "happy_path", overrides=None, **scenario_overrides):
        self.root = tmp_path
        self.cfg = make_config(deep_merge(FAST, overrides or {}))
        self.store = Store(self.cfg.paths.db_file)
        self.jobs = JobService(self.store, self.cfg)
        self.scenario = scenario(scenario_name, **scenario_overrides)
        self.fake = FakeVidsController(self.scenario, self.cfg.paths.data_dir, self.cfg.paths.screenshots_dir)
        self.workflow = Workflow(self.store, self.cfg, self.fake)

    def restart(self) -> "Env":
        """Simulate an app restart: fresh store, controller and workflow on the same files."""
        self.store = Store(self.cfg.paths.db_file)
        self.jobs = JobService(self.store, self.cfg)
        self.fake = FakeVidsController(self.scenario, self.cfg.paths.data_dir, self.cfg.paths.screenshots_dir)
        self.workflow = Workflow(self.store, self.cfg, self.fake)
        return self

    def sample_job(self, mode: str = "multi_part", parts: int = 3, name: str = "Sample Project") -> str:
        job_id = self.jobs.create_job(name, mode)
        for n in range(1, parts + 1):
            folder = SAMPLE / f"part_{n:02d}"
            self.jobs.add_part(job_id, (folder / "script.txt").read_bytes(),
                               [("reference.png", (folder / "reference.png").read_bytes())], title=f"P{n}")
        result = self.jobs.validate(job_id, self.fake.capabilities())
        assert result["ok"], result
        return job_id

    def run(self, job_id: str, resume: bool = False) -> str:
        return self.workflow.run_job(job_id, threading.Event(), resume=resume)

    def parts(self, job_id: str) -> list[dict]:
        return self.store.get_parts(job_id)

    def statuses(self, job_id: str) -> list[str]:
        return [p["status"] for p in self.parts(job_id)]

    def job(self, job_id: str) -> dict:
        return self.store.get_job(job_id)

    def steps(self, job_id: str) -> list[str]:
        return [e["step"] for e in self.store.all_events(job_id)]

    def gen_count(self) -> int:
        return self.fake.state()["total_generations"]


@pytest.fixture
def env(tmp_path, make_config):
    def _env(scenario_name: str = "happy_path", overrides=None, **scenario_overrides) -> Env:
        return Env(tmp_path, make_config, scenario_name, overrides, **scenario_overrides)

    return _env
