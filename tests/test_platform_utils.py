from pathlib import Path

import pytest

from automation import platform_utils
from automation.errors import ConfigError


def exists_only(*paths):
    """An is_file stand-in that reports only `paths` as existing."""
    present = set(paths)
    return lambda p: p in present


def test_os_family_maps_sys_platform_values():
    assert platform_utils.os_family("win32") == "windows"
    assert platform_utils.os_family("darwin") == "macos"
    assert platform_utils.os_family("linux") == "linux"


def test_configured_chrome_binary_wins(tmp_path):
    chrome = tmp_path / "chrome"
    chrome.write_bytes(b"")
    assert platform_utils.find_chrome_binary(chrome) == chrome


def test_configured_chrome_binary_must_exist(tmp_path):
    with pytest.raises(ConfigError, match="browser.chrome_binary does not exist"):
        platform_utils.find_chrome_binary(tmp_path / "missing-chrome")


def test_a11_windows_machine_wide_install_found(tmp_path):
    """A11: the standard Program Files install on a clean Windows 11 machine is detected."""
    env = {"PROGRAMFILES": str(tmp_path / "pf"), "LOCALAPPDATA": str(tmp_path / "local")}
    expected = tmp_path / "pf" / "Google" / "Chrome" / "Application" / "chrome.exe"
    found = platform_utils.find_chrome_binary(platform="win32", env=env, is_file=exists_only(expected))
    assert found == expected


def test_a11_windows_per_user_install_found(tmp_path):
    env = {"PROGRAMFILES": str(tmp_path / "pf"), "LOCALAPPDATA": str(tmp_path / "local")}
    expected = tmp_path / "local" / "Google" / "Chrome" / "Application" / "chrome.exe"
    found = platform_utils.find_chrome_binary(platform="win32", env=env, is_file=exists_only(expected))
    assert found == expected


def test_windows_machine_wide_install_preferred_over_per_user(tmp_path):
    env = {"PROGRAMFILES": str(tmp_path / "pf"), "LOCALAPPDATA": str(tmp_path / "local")}
    machine = tmp_path / "pf" / "Google" / "Chrome" / "Application" / "chrome.exe"
    user = tmp_path / "local" / "Google" / "Chrome" / "Application" / "chrome.exe"
    found = platform_utils.find_chrome_binary(platform="win32", env=env, is_file=exists_only(machine, user))
    assert found == machine


def test_macos_applications_install_found(tmp_path):
    expected = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    found = platform_utils.find_chrome_binary(platform="darwin", home=tmp_path, is_file=exists_only(expected))
    assert found == expected


def test_macos_user_applications_install_found(tmp_path):
    expected = tmp_path / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    found = platform_utils.find_chrome_binary(platform="darwin", home=tmp_path, is_file=exists_only(expected))
    assert found == expected


def test_linux_chrome_found_on_path(monkeypatch, tmp_path):
    chrome = tmp_path / "google-chrome-stable"
    monkeypatch.setattr(platform_utils.shutil, "which",
                        lambda name: str(chrome) if name == "google-chrome-stable" else None)
    assert platform_utils.find_chrome_binary(platform="linux", is_file=exists_only(chrome)) == chrome


def test_missing_chrome_explains_the_fix(tmp_path):
    with pytest.raises(ConfigError, match="set browser.chrome_binary"):
        platform_utils.find_chrome_binary(platform="win32", env={}, home=tmp_path, is_file=exists_only())


def test_p2_everyday_profile_locations(tmp_path):
    """P2: the everyday Chrome profile folders the automation must refuse, per OS."""
    env = {"LOCALAPPDATA": str(tmp_path / "local")}
    assert platform_utils.default_chrome_user_data_dirs("win32", env, tmp_path) == [
        tmp_path / "local" / "Google" / "Chrome" / "User Data"
    ]
    assert platform_utils.default_chrome_user_data_dirs("darwin", env, tmp_path) == [
        tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
    ]
    assert tmp_path / ".config" / "google-chrome" in platform_utils.default_chrome_user_data_dirs(
        "linux", env, tmp_path
    )
