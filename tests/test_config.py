import logging

import pytest

from automation.config import DEFAULTS_FILE, load_config
from automation.errors import ConfigError


def test_t3_shipped_example_config_loads():
    """T3: config.example.json itself is valid and supplies every default."""
    cfg = load_config(DEFAULTS_FILE)
    assert cfg.browser.mode == "attach"
    assert cfg.timeouts_seconds.generation == 900


def test_t3_missing_config_json_uses_defaults(tmp_path):
    """T3: without config.json the defaults apply (setup copies it, but a missing file must not crash)."""
    cfg = load_config(root=tmp_path)
    assert cfg.browser.debug_port == 9222
    assert cfg.root == tmp_path


def test_t3_explicit_config_path_must_exist(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.json", root=tmp_path)


def test_t3_user_values_deep_merge_over_defaults(make_config):
    """T3: config.json overrides only the keys it names; sibling keys keep their defaults."""
    cfg = make_config({"browser": {"debug_port": 9333}, "retries": {"step_max_attempts": 5}})
    assert cfg.browser.debug_port == 9333
    assert cfg.browser.mode == "attach"
    assert cfg.retries.step_max_attempts == 5
    assert cfg.retries.backoff_seconds == (5, 15, 45)


def test_t3_doc_keys_are_ignored(make_config):
    cfg = make_config({"_doc": "note", "browser": {"_doc": "note", "debug_port": 9333}})
    assert "_doc" not in cfg
    assert "_doc" not in cfg.browser


def test_t3_unknown_key_warns_and_is_ignored(make_config, caplog):
    with caplog.at_level(logging.WARNING, logger="automation.config"):
        cfg = make_config({"browser": {"stealth": True}, "extra_section": {}})
    assert "stealth" not in cfg.browser
    assert "extra_section" not in cfg
    assert "browser.stealth" in caplog.text
    assert "extra_section" in caplog.text


@pytest.mark.parametrize(
    "overrides, key",
    [
        ({"browser": {"debug_port": "9222"}}, "browser.debug_port"),
        ({"browser": {"debug_port": True}}, "browser.debug_port"),
        ({"browser": {"headless": 0}}, "browser.headless"),
        ({"browser": {"window_size": [1440, "900"]}}, "browser.window_size"),
        ({"browser": "attach"}, "browser"),
        ({"limits": {"max_script_chars": "500"}}, "limits.max_script_chars"),
        ({"retries": {"file_move_interval_seconds": "fast"}}, "retries.file_move_interval_seconds"),
    ],
)
def test_t3_wrong_type_fails_fast_naming_the_key(make_config, overrides, key):
    with pytest.raises(ConfigError, match=key.replace(".", r"\.")):
        make_config(overrides)


def test_t3_int_accepted_where_default_is_float(make_config):
    assert make_config({"retries": {"file_move_interval_seconds": 1}}).retries.file_move_interval_seconds == 1


def test_t3_nullable_keys_accept_null_or_their_type(make_config):
    cfg = make_config({"limits": {"max_script_chars": 2000}, "browser": {"chrome_binary": None}})
    assert cfg.limits.max_script_chars == 2000
    assert cfg.browser.chrome_binary is None


def test_t3_invalid_json_reports_file_and_line(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{\n  "browser": {,\n}', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"config\.json: invalid JSON at line 2"):
        load_config(path, root=tmp_path)


def test_dec11_browser_mode_must_be_attach_or_launch(make_config):
    assert make_config({"browser": {"mode": "launch"}}).browser.mode == "launch"
    with pytest.raises(ConfigError, match="browser.mode"):
        make_config({"browser": {"mode": "stealth"}})


def test_se6_headless_is_refused(make_config):
    """05 §1: live runs never use headless Chrome."""
    with pytest.raises(ConfigError, match="headless"):
        make_config({"browser": {"headless": True}})


def test_dec12_timeouts_must_be_positive(make_config):
    with pytest.raises(ConfigError, match="timeouts_seconds.generation"):
        make_config({"timeouts_seconds": {"generation": 0}})


def test_debug_port_range_is_checked(make_config):
    with pytest.raises(ConfigError, match="debug_port"):
        make_config({"browser": {"debug_port": 80}})


def test_relative_paths_resolve_against_project_root(make_config, tmp_path):
    cfg = make_config({"paths": {"logs_dir": "custom/logs"}})
    assert cfg.paths.logs_dir == tmp_path / "custom" / "logs"
    assert cfg.paths.db_file == tmp_path / "data" / "jobs.sqlite3"
    assert cfg.browser.profile_dir == tmp_path / "data" / "chrome-profile"
    assert cfg.fake.scenario == tmp_path / "tests" / "scenarios" / "happy_path.json"


def test_absolute_paths_are_kept(make_config, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "chrome"
    cfg = make_config({"browser": {"chrome_binary": str(elsewhere)}})
    assert cfg.browser.chrome_binary == elsewhere


def test_config_is_read_only(make_config):
    cfg = make_config()
    with pytest.raises(TypeError):
        cfg.browser["debug_port"] = 1
    assert isinstance(cfg.browser.window_size, tuple)
