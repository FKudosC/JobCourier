"""BossSession 配置单元测试。"""

import asyncio
from pathlib import Path

import pytest
from patchright.async_api import Error as PlaywrightError

from job.boss.session import BossSession
from job.paths import USER_DATA_DIR

_INSTALL_HINT = "无法启动本机 Chrome（channel=chrome），请确认已安装 Google Chrome。"


class _FakePage:
    """占位标签页：能塞进 pages 列表、能被置顶、能关掉、能报自己的网址。"""

    def __init__(self, url: str = "about:blank", owner: "_FakeContext | None" = None) -> None:
        self.url = url
        self.owner = owner
        self.fronted = False
        self.closed = False

    async def bring_to_front(self) -> None:
        self.fronted = True

    async def goto(self, url: str, **_kwargs) -> None:
        # 「查看浏览器」前若只剩白纸，会话会给它开个首页；假页面照记网址
        self.url = url

    async def close(self) -> None:
        self.closed = True
        # 真 Playwright 里关掉的标签页会从 context.pages 消失，假页面也照做
        if self.owner is not None and self in self.owner.pages:
            self.owner.pages.remove(self)


class _FakeCdp:
    """假 CDP 会话：记下发过的命令，回读窗口状态。

    ``swallow`` 为真时假装 Chrome 把 ``setWindowBounds`` 吞掉（原样回读旧坐标），
    用来复现「点了按钮没反应」。
    """

    def __init__(self, context: "_FakeContext") -> None:
        self.context = context
        self.sent: list[tuple[str, dict | None]] = []
        self.detached = False

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7}
        if method == "Browser.setWindowBounds":
            if not self.context.swallow:
                self.context.apply_bounds((params or {}).get("bounds") or {})
            return {}
        if method == "Browser.getWindowBounds":
            return {
                "bounds": {
                    "left": self.context.left,
                    "top": self.context.top,
                    "windowState": self.context.window_state,
                }
            }
        if method == "Target.createTarget":
            # 后台开页：真 Chrome 会新开一张标签页，但窗口状态原样不动
            self.context.pages.append(_FakePage(owner=self.context))
            return {"targetId": "bg-1"}
        return {}

    async def detach(self) -> None:
        """真 CDP 会话用完要摘掉；假会话照做一个空动作。

        ``hang_detach`` 为真时模拟「页面卡在导航里」：真 Chrome 这时会让 detach
        永不返回，用来验证会话不会被它拖死。
        """
        if self.context.hang_detach:
            await asyncio.Event().wait()
        self.detached = True


class _FakeContext:
    """最小假 context：pages / new_page / new_cdp_session / close / on。"""

    def __init__(
        self,
        pages: list | None = None,
        *,
        new_page_error: Exception | None = None,
        left: int = 60,
        top: int = 60,
        swallow: bool = True,
        window_state: str = "normal",
        hang_detach: bool = False,
    ) -> None:
        self.pages = list(pages or [])
        for page in self.pages:
            page.owner = self
        self._new_page_error = new_page_error
        self.left = left
        self.top = top
        self.swallow = swallow
        self.window_state = window_state
        self.hang_detach = hang_detach
        self.closed = False
        self.cdp = _FakeCdp(self)

    def apply_bounds(self, bounds: dict) -> None:
        """把 CDP 请求的窗口状态记下来（真 Chrome 会照做）。"""
        for key in ("left", "top"):
            if key in bounds:
                setattr(self, key, bounds[key])
        if state := bounds.get("windowState"):
            self.window_state = state

    async def new_page(self):
        if self._new_page_error is not None:
            raise self._new_page_error
        # 真 Chrome 里常规开页会把最小化的窗口恢复出来
        self.window_state = "normal"
        page = _FakePage(owner=self)
        self.pages.append(page)
        return page

    async def new_cdp_session(self, _page) -> _FakeCdp:
        return self.cdp

    async def close(self) -> None:
        self.closed = True

    def on(self, *_args, **_kwargs) -> None:
        return None


def test_set_background_rebuilds_dead_browser(tmp_path: Path, monkeypatch):
    """窗口被手动关掉后 _context 还挂着却没标签页：调窗口前必须重建。"""
    session = BossSession(user_data_dir=tmp_path)
    dead = _FakeContext(
        new_page_error=PlaywrightError("Target page, context or browser has been closed")
    )
    fresh = _FakeContext()
    session._context = dead
    launched: list[int] = []

    async def fake_launch():
        launched.append(1)
        return fresh

    monkeypatch.setattr(session, "_launch", fake_launch)

    assert asyncio.run(session.set_background(False)) is True
    assert launched == [1]
    assert dead.closed is True
    assert session._context is fresh
    assert [m for m, _ in fresh.cdp.sent] == [
        "Browser.getWindowForTarget",
        "Browser.setWindowBounds",
        "Browser.setWindowBounds",
        "Browser.getWindowBounds",
    ]
    assert fresh.pages[0].fronted is True


def test_set_background_off_screen_does_not_launch(tmp_path: Path, monkeypatch):
    """只是把窗口移出屏幕时不该顺手把浏览器拉起来。"""
    session = BossSession(user_data_dir=tmp_path)
    launched: list[int] = []

    async def fake_launch():
        launched.append(1)
        return _FakeContext()

    monkeypatch.setattr(session, "_launch", fake_launch)
    assert asyncio.run(session.set_background(True)) is False
    assert launched == []


def test_set_background_reports_window_stuck_offscreen(tmp_path: Path):
    """Chrome 把 setWindowBounds 吞掉时要如实返回失败，别让按钮静默无反应。"""
    session = BossSession(user_data_dir=tmp_path)
    session._context = _FakeContext(pages=[_FakePage()], left=-32000, top=-32000)
    assert asyncio.run(session.set_background(False)) is False


def test_set_background_off_when_closed_by_user_stays_quiet(tmp_path: Path):
    """用户点过「关闭浏览器」：调窗口不能抛错，也不能偷偷重开。"""
    session = BossSession(user_data_dir=tmp_path)
    asyncio.run(session.close(keep_closed=True))
    assert asyncio.run(session.set_background(False)) is False
    assert not session.is_open


def test_surface_shows_window_without_touching_the_setting(tmp_path: Path):
    """用户点「填表 / 重扫」只是看一眼：窗口上屏，但「后台静默运行」这个设置不许被改。

    早先这一步直接复用 ``set_background(False)``，等于替用户关了后台静默：之后
    每个查询任务都会把窗口顶到他面前，打断前台干活。
    """
    session = BossSession(user_data_dir=tmp_path)
    assert session.background is True
    session._context = _FakeContext(pages=[_FakePage(url="https://acme.cn/apply")])

    assert asyncio.run(session.surface()) is True
    assert session.background is True  # 设置原样留着
    assert session.viewing is True  # 此刻用户正看着它
    assert session._context.pages[0].fronted is True
    # 下一个后台任务开跑时收回「正看着」，窗口又回到屏幕外
    session.watch_off()
    assert session.viewing is False


def test_set_background_true_minimizes_window(tmp_path: Path):
    """切回后台静默：窗口最小化到任务栏，用户还能点任务栏调出来。"""
    session = BossSession(user_data_dir=tmp_path)
    session._context = _FakeContext(
        pages=[_FakePage(url="https://acme.cn/apply")], swallow=False
    )
    assert asyncio.run(session.set_background(True)) is True
    assert session._context.window_state == "minimized"
    assert session.background is True
    assert session.viewing is False


def test_minimize_puts_offscreen_window_back_on_screen(tmp_path: Path):
    """最小化前先把「记忆坐标」摆回屏幕内，否则用户点任务栏恢复出来是个隐形窗口。"""
    session = BossSession(user_data_dir=tmp_path)
    session._context = _FakeContext(
        pages=[_FakePage(url="https://acme.cn/apply")],
        left=-21845,
        top=-21845,
        swallow=False,
    )
    assert asyncio.run(session.set_background(True)) is True
    assert session._context.window_state == "minimized"
    assert session._context.left > -1000 and session._context.top > -1000


def test_minimize_skips_calls_when_already_parked(tmp_path: Path):
    """窗口已经乖乖待在任务栏、坐标也在屏幕内：不必再折腾它。"""
    session = BossSession(user_data_dir=tmp_path)
    session._context = _FakeContext(
        pages=[_FakePage(url="https://acme.cn/apply")],
        swallow=False,
        window_state="minimized",
    )
    assert asyncio.run(session.set_background(True)) is True
    assert [m for m, _ in session._context.cdp.sent] == [
        "Browser.getWindowForTarget",
        "Browser.getWindowBounds",
    ]


def test_new_page_keeps_window_minimized(tmp_path: Path):
    """后台开标签页不能把最小化的窗口弹出来：走 CDP background，窗口状态原样不动。"""
    session = BossSession(user_data_dir=tmp_path)
    session._context = _FakeContext(
        pages=[_FakePage(url="https://acme.cn/apply")],
        swallow=False,
        window_state="minimized",
    )
    asyncio.run(session.new_page())
    assert session._context.window_state == "minimized"
    assert len(session._context.pages) == 2
    created = [p for m, p in session._context.cdp.sent if m == "Target.createTarget"]
    assert created == [{"url": "about:blank", "background": True}]


def test_new_page_when_viewing_opens_foreground(tmp_path: Path):
    """用户正看着浏览器时开页不必躲着走，走常规方式即可。"""
    session = BossSession(user_data_dir=tmp_path)
    session._context = _FakeContext(pages=[_FakePage(url="https://acme.cn/apply")])
    session.viewing = True
    asyncio.run(session.new_page())
    assert [m for m, _ in session._context.cdp.sent] == []
    assert len(session._context.pages) == 2


def test_forget_ignores_newer_context(tmp_path: Path):
    """外部关闭事件只清自己那一份引用，别把重建后的新会话一起清掉。"""
    session = BossSession(user_data_dir=tmp_path)
    old, new = _FakeContext(), _FakeContext()
    session._context = new
    session._forget(old)
    assert session.is_open
    session._forget(new)
    assert not session.is_open


def _run(coro):
    """跑一个可能故意留下「挂住的任务」的协程，收尾时把残留任务取消干净。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


def test_minimize_survives_hanging_detach(tmp_path: Path):
    """页面卡在导航里时 detach 会永久挂住：最小化必须照常返回，不能把请求钉死。"""
    session = BossSession(user_data_dir=tmp_path)
    session._context = _FakeContext(
        pages=[_FakePage(url="https://acme.cn/apply")],
        swallow=False,
        hang_detach=True,
    )
    assert _run(session.set_background(True)) is True
    assert session._context.window_state == "minimized"


def test_restore_survives_hanging_detach(tmp_path: Path):
    """同上：调出窗口时 detach 挂住也不能拖死「查看浏览器」。"""
    session = BossSession(user_data_dir=tmp_path)
    session._context = _FakeContext(
        pages=[_FakePage(url="https://acme.cn/apply")],
        swallow=False,
        window_state="minimized",
        hang_detach=True,
    )
    assert _run(session.set_background(False)) is True
    assert session._context.window_state == "normal"
    assert session._context.pages[0].fronted is True


def test_new_page_survives_hanging_detach(tmp_path: Path):
    """后台开页收尾时 detach 挂住，也不能把开页本身拖死。"""
    session = BossSession(user_data_dir=tmp_path)
    session._context = _FakeContext(
        pages=[_FakePage(url="https://acme.cn/apply")],
        swallow=False,
        window_state="minimized",
        hang_detach=True,
    )
    page = _run(session.new_page())
    assert page.url == "about:blank"
    assert len(session._context.pages) == 2
    assert session._context.window_state == "minimized"


def test_bounded_reports_timeout_as_failure(tmp_path: Path):
    """到点没返回就如实报失败，让上层能给用户一句实话而不是干等。"""
    session = BossSession(user_data_dir=tmp_path)

    async def never():
        await asyncio.Event().wait()

    assert _run(session._bounded(never(), 0.05, "测试调用")) == (False, None)


def test_page_prefers_real_tab_over_blank(tmp_path: Path):
    """复用时先挑真页面：别拿 Chrome 自带的 about:blank 去导航。"""
    session = BossSession(user_data_dir=tmp_path)
    blank, site = _FakePage("about:blank"), _FakePage("https://acme.cn/apply")
    session._context = _FakeContext(pages=[blank, site])
    assert asyncio.run(session.page()) is site


def test_page_uses_lone_blank_tab(tmp_path: Path):
    """只剩一张空白页时也得用它，不然每次都要多开一个标签。"""
    session = BossSession(user_data_dir=tmp_path)
    blank = _FakePage("about:blank")
    session._context = _FakeContext(pages=[blank])
    assert asyncio.run(session.page()) is blank


def test_tidy_blank_pages_spares_the_last_one(tmp_path: Path):
    """收空白页时最后一个必须留着：标签页全关掉窗口也跟着没了。"""
    session = BossSession(user_data_dir=tmp_path)
    first, last = _FakePage("about:blank"), _FakePage("chrome://newtab/")
    context = _FakeContext(pages=[first, last])
    asyncio.run(session._tidy_blank_pages(context))
    assert first.closed is True and last.closed is False
    assert context.pages == [last]


def test_tidy_blank_pages_keeps_real_page(tmp_path: Path):
    """有真页面时把占位的空白页收掉，别让「查看浏览器」第一眼是白纸。"""
    session = BossSession(user_data_dir=tmp_path)
    blank, site = _FakePage("about:blank"), _FakePage("https://acme.cn/apply")
    context = _FakeContext(pages=[blank, site])
    asyncio.run(session._tidy_blank_pages(context))
    assert blank.closed is True and site.closed is False
    assert context.pages == [site]


def test_set_background_off_closes_blank_tab(tmp_path: Path):
    """点「查看浏览器」时顺手收掉空白标签页，再置顶真页面。"""
    session = BossSession(user_data_dir=tmp_path)
    blank, site = _FakePage("about:blank"), _FakePage("https://acme.cn/apply")
    session._context = _FakeContext(pages=[blank, site])
    assert asyncio.run(session.set_background(False)) is True
    assert blank.closed is True and site.closed is False
    assert site.fronted is True


def test_close_keep_closed_blocks_reopen(tmp_path: Path):
    """点「关闭浏览器」后封锁自动重开：后台预查等任务不能再把浏览器拉起来。"""
    session = BossSession(user_data_dir=tmp_path)
    asyncio.run(session.close(keep_closed=True))
    with pytest.raises(RuntimeError):
        asyncio.run(session.open())
    session.resume()
    assert not session._closed_by_user.is_set()


def test_default_profile_dir_under_user_data():
    """默认 profile 落在项目内的 user_data/。"""
    assert BossSession.default_profile_dir() == USER_DATA_DIR / "chrome-profile"


def test_launch_error_profile_in_use_despite_chrome_path(tmp_path: Path):
    session = BossSession(user_data_dir=tmp_path)
    exc = PlaywrightError(
        "BrowserType.launch_persistent_context: Failed to create a ProcessSingleton "
        "for your profile directory. This usually means that the profile is already "
        "in use by another instance of Chromium.\n"
        "Call log:\n"
        "  - <launching> /opt/google/chrome/chrome --user-data-dir="
        f"{tmp_path} --remote-debugging-pipe about:blank\n"
        f"  - [err] Failed to create {tmp_path}/SingletonLock: File exists (17)\n"
    )
    msg = session._launch_error(exc)
    assert msg == (
        f"Chrome profile 被占用：{tmp_path}，请关闭其它使用该目录的 Chrome 后重试。"
    )
    assert _INSTALL_HINT not in msg


def test_launch_error_chrome_distribution_not_found(tmp_path: Path):
    session = BossSession(user_data_dir=tmp_path)
    exc = PlaywrightError(
        "BrowserType.launch_persistent_context: Chromium distribution 'chrome' "
        "is not found at /opt/google/chrome/chrome\n"
        'Run "npx playwright install chrome"'
    )
    assert session._launch_error(exc) == _INSTALL_HINT


def test_launch_error_spawn_failure_is_not_profile_in_use(tmp_path: Path):
    """Playwright 启动参数里的 BlockOriginHeader... 含「lock」，不能因此误报占用。"""
    session = BossSession(user_data_dir=tmp_path)
    chrome = r"C:\Users\Admin\AppData\Local\Google\Chrome\Application\chrome.exe"
    exc = PlaywrightError(
        "BrowserType.launch_persistent_context: spawn UNKNOWN\n"
        "Call log:\n"
        f"  - <launching> {chrome} --disable-features=BlockOriginHeaderModificationOnRedirect "
        f"--user-data-dir={tmp_path} --remote-debugging-pipe about:blank\n"
    )
    assert session._launch_error(exc) == f"启动 Chrome 失败：spawn UNKNOWN（{chrome}）"


def _install_chrome(prefix: Path, *, complete: bool) -> Path:
    app = prefix / "Google" / "Chrome" / "Application"
    version = app / "154.0.8037.58"
    version.mkdir(parents=True)
    (app / "chrome.exe").write_bytes(b"")
    if complete:
        (version / "chrome.dll").write_bytes(b"")
    return app / "chrome.exe"


def test_windows_skips_leftover_chrome(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("sys.platform", "win32")
    _install_chrome(tmp_path / "local", complete=False)
    system = _install_chrome(tmp_path / "program", complete=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "program"))
    for name in ("PROGRAMFILES(X86)", "HOMEDRIVE"):
        monkeypatch.delenv(name, raising=False)
    assert BossSession.chrome_executable() == system


def test_windows_without_usable_chrome_falls_back_to_channel(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr("sys.platform", "win32")
    _install_chrome(tmp_path / "local", complete=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "HOMEDRIVE"):
        monkeypatch.delenv(name, raising=False)
    assert BossSession.chrome_executable() is None


def test_non_windows_uses_channel(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    assert BossSession.chrome_executable() is None
