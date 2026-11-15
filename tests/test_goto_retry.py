"""upload_grid_to_x 导航重试与登录失效归类测试。"""

import pytest

from playwright.sync_api import Error as PWError

import upload_grid_to_x as ug


class _FakePage:
    def __init__(self, behaviors):
        # behaviors: list，元素为 None(成功) 或 Exception
        self.behaviors = list(behaviors)
        self.calls = 0
        self.waits = []

    def goto(self, url, wait_until=None, timeout=None):
        self.calls += 1
        b = self.behaviors.pop(0)
        if isinstance(b, Exception):
            raise b

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def test_goto_success_no_retry():
    page = _FakePage([None])
    ug._goto_with_retry(page, "https://x.com/home", retries=2)
    assert page.calls == 1
    assert page.waits == []


def test_goto_transient_error_then_success_retries():
    page = _FakePage([PWError("net::ERR_TIMED_OUT"), None])
    ug._goto_with_retry(page, "https://x.com/home", retries=2, delay=0)
    assert page.calls == 2            # 第一次失败、第二次成功
    assert len(page.waits) == 1       # 重试前等待一次


def test_goto_download_error_raises_login_expired_immediately():
    page = _FakePage([PWError("Page.goto: Download is starting")])
    with pytest.raises(ug.XLoginExpiredError):
        ug._goto_with_retry(page, "https://x.com/home", retries=3, delay=0)
    assert page.calls == 1            # 登录失效不重试，立即抛出
    assert page.waits == []


def test_goto_persistent_error_raises_runtime_error():
    page = _FakePage([PWError("ERR_CONNECTION_RESET"),
                      PWError("ERR_CONNECTION_RESET"),
                      PWError("ERR_CONNECTION_RESET")])
    with pytest.raises(RuntimeError):
        ug._goto_with_retry(page, "https://x.com/home", retries=2, delay=0)
    assert page.calls == 3            # 初始 + 2 次重试
