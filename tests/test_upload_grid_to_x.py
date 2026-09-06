import os
import socket
import sys
import tempfile

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import upload_grid_to_x as ux


def test_is_port_open_false_on_unused_port():
    assert ux.is_port_open("127.0.0.1", 59999, timeout=0.2) is False


def test_is_port_open_true_on_listening_socket():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert ux.is_port_open("127.0.0.1", port, timeout=0.5) is True
    finally:
        srv.close()


def test_build_chrome_args_contains_flags():
    args = ux.build_chrome_args("/usr/bin/chrome", 9222, "/tmp/data", "http://proxy:7890")
    assert args[0] == "/usr/bin/chrome"
    assert "--remote-debugging-port=9222" in args
    assert "--user-data-dir=/tmp/data" in args
    assert "--proxy-server=http://proxy:7890" in args


def test_build_chrome_args_no_proxy():
    args = ux.build_chrome_args("/usr/bin/chrome", 9222, "/tmp/data", "")
    assert "--proxy-server=" not in " ".join(args)


def test_config_constants_exist():
    assert isinstance(ux.DEBUG_PORT, int)
    assert isinstance(ux.POST_TEXT, str)
    assert isinstance(ux.PROXY, str)
    assert isinstance(ux.CHROME_PATH, str)


def test_launch_chrome_missing_executable():
    with pytest.raises(FileNotFoundError):
        ux.launch_chrome(chrome_path="/no/such/chrome", port=59998, wait=0.1)


def test_ensure_browser_reuses_open_port(monkeypatch):
    monkeypatch.setattr(ux, "is_port_open", lambda *a, **k: True)
    proc, started = ux.ensure_browser(port=9222)
    assert proc is None
    assert started is False


def test_ensure_browser_launches_when_closed(monkeypatch):
    calls = {}

    def fake_launch(**kwargs):
        calls["launched"] = True
        return "FAKE_PROC"

    monkeypatch.setattr(ux, "is_port_open", lambda *a, **k: False)
    monkeypatch.setattr(ux, "launch_chrome", lambda **k: fake_launch(**k))
    proc, started = ux.ensure_browser(port=9222)
    assert proc == "FAKE_PROC"
    assert started is True
    assert calls["launched"] is True


def test_generate_grid_creates_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    data = {"blocks": [{"models": [{"id": 1, "username": "Alice"}]}]}
    monkeypatch.setattr(ux, "fetch_data", lambda: data)
    monkeypatch.setattr(ux, "pick_9_covers", lambda d, n: [{"id": 1, "url": "u"}])
    fake_img = Image.new("RGB", (320, 240), "red")
    monkeypatch.setattr(ux, "download_images", lambda covers: [fake_img])
    path, username = ux.generate_grid()
    assert os.path.exists(path)
    assert path.endswith(".jpg")
    assert username == "Alice"


def test_build_username_map():
    data = {
        "blocks": [
            {"models": [{"id": 1, "username": "Alice"}, {"id": 2, "username": "Bob"}]},
            {"models": [{"id": 3, "username": "Cara"}]},
        ]
    }
    umap = ux.build_username_map(data)
    assert umap == {1: "Alice", 2: "Bob", 3: "Cara"}


def test_render_post_text_replaces_placeholder():
    assert ux.render_post_text("hi {username}!", "Alice") == "hi Alice!"


def test_render_post_text_no_placeholder_unchanged():
    assert ux.render_post_text("no placeholder", "Alice") == "no placeholder"


def test_render_post_text_empty_username():
    assert ux.render_post_text("hi {username}", "") == "hi "


def test_upload_media_to_x_exists_and_old_name_gone():
    assert hasattr(ux, "upload_media_to_x")
    assert not hasattr(ux, "upload_image_to_x")
