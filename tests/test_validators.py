"""V1-V5, sanitizing and templates (M2.1). Fixture images are generated per test (08 §2)."""

import io
from pathlib import Path

import pytest
from PIL import Image

from automation.errors import ValidationError
from automation.validators import (
    check_image,
    check_output_dir,
    check_template,
    read_script,
    render_names,
    sanitize_filename,
    slugify_project,
)


def make_image(path: Path, fmt: str, size=(64, 48)) -> Path:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 100, 50)).save(buf, format=fmt)
    path.write_bytes(buf.getvalue())
    return path


@pytest.mark.parametrize("fmt, name", [("PNG", "ok.png"), ("JPEG", "ok.jpg"), ("WEBP", "ok.webp")])
def test_v2_accepts_png_jpeg_webp(make_config, tmp_path, fmt, name):
    detected, errors = check_image(make_image(tmp_path / name, fmt), make_config())
    assert errors == [] and detected == fmt


def test_v2_fake_extension_detected_by_content(make_config, tmp_path):
    """A .png that is really a JPEG is accepted as JPEG (saved later as .jpg)."""
    detected, errors = check_image(make_image(tmp_path / "fake_ext.png", "JPEG"), make_config())
    assert errors == [] and detected == "JPEG"


def test_v2_text_file_named_png_rejected(make_config, tmp_path):
    bad = tmp_path / "not_image.png"
    bad.write_text("hello", encoding="utf-8")
    assert check_image(bad, make_config())[1]


def test_v2_truncated_image_rejected(make_config, tmp_path):
    ok = make_image(tmp_path / "full.png", "PNG", (400, 400))
    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(ok.read_bytes()[:200])
    assert check_image(truncated, make_config())[1]


def test_v2_oversized_image_rejected(make_config, tmp_path):
    big = make_image(tmp_path / "huge.png", "PNG")
    cfg = make_config({"limits": {"max_image_mb": 0}})
    assert "limit" in check_image(big, cfg)[1][0]


def test_v1_script_rules_never_alter_text():
    text, errors = read_script("  keep  spacing \n".encode("utf-8"), 100)
    assert errors == [] and text == "  keep  spacing \n"
    assert read_script(b"   \n", None)[1] == ["Script is empty."]
    assert read_script(b"\xff\xfe\x00bad", None)[1] == ["Script is not valid UTF-8 text."]
    assert "never shortened" in read_script(b"x" * 11, 10)[1][0]


@pytest.mark.parametrize("raw, expected", [
    ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
    ("CON", "_CON"), ("lpt1.mp4", "_lpt1.mp4"), ("name. . ", "name"), ("../../etc", "_etc"),
])
def test_v3_sanitize_windows_rules(raw, expected):
    result = sanitize_filename(raw)
    assert result == expected
    assert "/" not in result and "\\" not in result and ".." not in result


def test_v3_sanitize_empty_rejected():
    with pytest.raises(ValidationError):
        sanitize_filename('<>:"')


def test_v3_slug_and_name_rules():
    assert slugify_project("My Project 1") == "My_Project_1"
    assert slugify_project("Proj", {"proj", "Proj_2"}) == "Proj_3"
    with pytest.raises(ValidationError):
        slugify_project("bad/name")


def test_v4_output_dir_rules(make_config, tmp_path):
    cfg = make_config()
    out, err = check_output_dir("output/X", cfg)
    assert err is None and out == tmp_path / "output" / "X" and out.is_dir()
    for forbidden in ("input/x", "data/x", "logs"):
        assert check_output_dir(forbidden, cfg)[1]


def test_dec05_templates_need_part_and_no_collisions():
    assert check_template("{project}_Clip", needs_part=True)
    assert check_template("{project}_{bogus}", needs_part=False)
    assert check_template("{project}_Part{part:02d}", needs_part=True) is None
    job = {"clip_template": "{project}_{title}_{part}", "project_slug": "P"}
    parts = [{"part_id": "a", "sequence": 1, "title": "x"}, {"part_id": "b", "sequence": 2, "title": "y"}]
    assert render_names(job, parts) == {"a": "P_x_1", "b": "P_y_2"}
    with pytest.raises(ValidationError, match="Parts 1 and 11"):
        render_names({"clip_template": "{project}_{title}{part}", "project_slug": "P"},
                     [{"part_id": "a", "sequence": 1, "title": "A1"}, {"part_id": "b", "sequence": 11, "title": "a"}])


def test_v5_single_mode_needs_exactly_one_part_and_image_limit(env, tmp_path):
    e = env()
    job_id = e.jobs.create_job("Solo", "single")
    img = make_image(tmp_path / "i.png", "PNG").read_bytes()
    for _ in range(2):
        e.jobs.add_part(job_id, b"prompt", [("i.png", img)])
    result = e.jobs.validate(job_id)
    assert any("exactly 1 part" in m for m in result["job_errors"])
    job2 = e.jobs.create_job("Many", "separate_clips")
    pid = e.jobs.add_part(job2, b"prompt", [("a.png", img), ("b.png", img), ("c.png", img)])
    assert any("at most 2" in m for m in e.jobs.validate(job2)["part_errors"][pid])


def test_dec27_webp_converted_losslessly_to_png(env, tmp_path):
    e = env()
    job_id = e.jobs.create_job("Webp", "single")
    src = make_image(tmp_path / "w.webp", "WEBP")
    pid = e.jobs.add_part(job_id, b"prompt", [("w.webp", src.read_bytes())])
    stored = Path(e.store.get_part(pid)["image_paths"][0])
    assert stored.suffix == ".png"
    with Image.open(stored) as a, Image.open(src) as b:
        assert a.format == "PNG" and a.convert("RGB").tobytes() == b.convert("RGB").tobytes()
    assert src.exists()  # user file untouched


def test_dec14_inputs_copied_byte_for_byte(env):
    e = env()
    job_id = e.sample_job("single", parts=1)
    part = e.parts(job_id)[0]
    from conftest import SAMPLE
    assert Path(part["script_path"]).read_bytes() == (SAMPLE / "part_01" / "script.txt").read_bytes()
    assert Path(part["image_paths"][0]).read_bytes() == (SAMPLE / "part_01" / "reference.png").read_bytes()
    assert Path(part["script_path"]).is_relative_to(e.cfg.paths.input_dir / "jobs")
