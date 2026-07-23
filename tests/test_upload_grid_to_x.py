import socket
import upload_grid_to_x as ux


def test_is_port_open_false_on_unused_port():
    # 选一个几乎不可能被占用的高位端口
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
    args = ux.build_chrome_args(r"C:\chrome.exe", 9222, r"C:\data")
    assert args[0] == r"C:\chrome.exe"
    assert "--remote-debugging-port=9222" in args
    assert r"--user-data-dir=C:\data" in args


def test_config_constants_exist():
    assert isinstance(ux.DEBUG_PORT, int)
    assert ux.X_COMPOSE_URL.startswith("https://")
    assert isinstance(ux.POST_TEXT, str)
