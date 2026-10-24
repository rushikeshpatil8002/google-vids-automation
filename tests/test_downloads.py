"""DEC-16 download detection, DL2 verification, DL5 safe moves (M3.2, A3, A4)."""

import threading
import time

import pytest

from automation import downloads
from automation.errors import DownloadError
from automation.fake_controller import tiny_mp4


@pytest.fixture
def cfg(make_config):
    return make_config({"downloads": {"poll_interval_seconds": 0.05}})


def test_a3_valid_mp4_passes(cfg, tmp_path):
    f = tmp_path / "ok.mp4"
    f.write_bytes(tiny_mp4())
    assert downloads.verify_video(f, cfg).ok


@pytest.mark.parametrize("name, data, reason", [
    ("empty.mp4", b"", "too small"),
    ("bad.mp4", b"\0" * 10240, "ftyp"),
    ("clip.mov", tiny_mp4(), "extension"),
    ("clip.mp4.crdownload", tiny_mp4(), "extension"),
])
def test_a3_bad_files_rejected(cfg, tmp_path, name, data, reason):
    f = tmp_path / name
    f.write_bytes(data)
    result = downloads.verify_video(f, cfg)
    assert not result.ok and reason in result.reason


def test_a3_missing_file_rejected(cfg, tmp_path):
    assert not downloads.verify_video(tmp_path / "nope.mp4", cfg).ok


def test_dl1_waits_for_temp_file_to_finish(cfg, tmp_path):
    before = downloads.snapshot(tmp_path)
    temp = tmp_path / "v.mp4.crdownload"
    temp.write_bytes(b"partial")

    def finish():
        time.sleep(0.3)
        temp.write_bytes(tiny_mp4())
        temp.replace(tmp_path / "v.mp4")

    threading.Thread(target=finish).start()
    got = downloads.wait_for_download(tmp_path, before, cfg, start_timeout=2, complete_timeout=3)
    assert got.name == "v.mp4"


def test_er6_never_completing_download_raises(cfg, tmp_path):
    before = downloads.snapshot(tmp_path)
    (tmp_path / "v.mp4.crdownload").write_bytes(b"partial")
    with pytest.raises(DownloadError, match="did not complete"):
        downloads.wait_for_download(tmp_path, before, cfg, start_timeout=1, complete_timeout=0.4)


def test_er6_download_that_never_starts_raises(cfg, tmp_path):
    with pytest.raises(DownloadError, match="did not start"):
        downloads.wait_for_download(tmp_path, downloads.snapshot(tmp_path), cfg, start_timeout=0.2)


def test_a4_duplicate_v2(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "P_Part01.mp4").write_bytes(b"first")
    (out / "p_part01_v2.mp4").write_bytes(b"second")  # case-insensitive clash, like Windows
    src = tmp_path / "new.mp4"
    src.write_bytes(tiny_mp4())
    target = downloads.safe_move(src, out, "P_Part01", ".mp4")
    assert target.name == "P_Part01_v3.mp4"
    assert (out / "P_Part01.mp4").read_bytes() == b"first"


def test_mp4_duration_read(tmp_path):
    f = tmp_path / "noduration.mp4"
    f.write_bytes(tiny_mp4())
    assert downloads.mp4_duration_seconds(f) is None


def test_orphans_moved_never_deleted(tmp_path):
    staging, orphans = tmp_path / "s", tmp_path / "o"
    staging.mkdir()
    (staging / "left.mp4").write_bytes(b"x")
    moved = downloads.move_leftovers(staging, orphans)
    assert [m.name for m in moved] == ["left.mp4"] and (orphans / "left.mp4").exists()
