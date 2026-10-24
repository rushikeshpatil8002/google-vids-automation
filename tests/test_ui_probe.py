"""Pure helpers of tools/ui_probe.py (05 §9). The live capture itself runs in M1 step 1.4."""

from tools import ui_probe


def test_rule4_inline_scripts_and_hidden_values_removed_from_dom_dump():
    """CLAUDE.md rule 4: tokens Google embeds in inline scripts and hidden inputs never reach dom.html."""
    html = (
        '<html><head><script nonce="n1">WIZ_global_data={"SNlM0e":"SECRET1"};</script>'
        '<script src="https://www.gstatic.com/app.js"></script></head>'
        '<body><form><input type="hidden" name="at" value="SECRET2">'
        "<input value='SECRET3' type=hidden>"
        '<input type="text" placeholder="Describe your video" value="keep"></form>'
        "<SCRIPT>\nvar t = 'SECRET4';\n</SCRIPT></body></html>"
    )
    cleaned = ui_probe.clean_html(html)
    for secret in ("SECRET1", "SECRET2", "SECRET3", "SECRET4", "gstatic"):
        assert secret not in cleaned
    assert 'placeholder="Describe your video"' in cleaned
    assert 'value="keep"' in cleaned
    assert 'name="at"' in cleaned


def test_dec18_url_saved_without_query_fragment_or_userinfo():
    url = "https://user:pw@docs.google.com/videos/d/abc123/edit?authuser=0&usp=x#scene=2"
    assert ui_probe.url_without_query(url) == "https://docs.google.com/videos/d/abc123/edit"


def test_p4_emails_masked_in_nested_snapshot():
    data = {"title": "Vids", "elements": [{"aria_label": "Google Account: Jane (jane.doe+x@gmail.com)"}]}
    masked = ui_probe.mask_emails(data)
    assert masked["elements"][0]["aria_label"] == "Google Account: Jane (***@***)"
    assert masked["title"] == "Vids"


def test_dl5_snapshot_folders_never_reuse_a_number(tmp_path):
    """Nothing is overwritten: numbering continues past existing folders, even after a rerun."""
    (tmp_path / "01_home").mkdir()
    (tmp_path / "07_editor").mkdir()
    first = ui_probe.next_snapshot_dir(tmp_path, "upload dialog")
    second = ui_probe.next_snapshot_dir(tmp_path, "upload dialog")
    assert first.name == "08_upload_dialog"
    assert second.name == "09_upload_dialog"


def test_labels_become_safe_folder_names():
    assert ui_probe.sanitize_label("../../etc/passwd") == "etc_passwd"
    assert ui_probe.sanitize_label("  ") == "snap"
    assert len(ui_probe.sanitize_label("x" * 100)) == 40


def test_rule4_url_query_strings_removed_from_dom_dump():
    html = '<a href="https://docs.google.com/download?c=SIGNED&x=1">d</a><img src=\'/v.png#frag\'>'
    cleaned = ui_probe.clean_html(html)
    assert "SIGNED" not in cleaned and "frag" not in cleaned
    assert 'href="https://docs.google.com/download"' in cleaned
    assert "src='/v.png'" in cleaned


def test_delayed_label_parsing():
    assert ui_probe.parse_label("file_menu +5") == ("file_menu", 5)
    assert ui_probe.parse_label("+3") == ("snap", 3)
    assert ui_probe.parse_label("editor") == ("editor", 0)
    assert ui_probe.parse_label("") == ("snap", 0)
