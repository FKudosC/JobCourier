"""BOSS 浏览器会话：只管 Playwright / Chrome 的启动、复用与关闭。"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from patchright.async_api import (
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    async_playwright,
)
from patchright.async_api import Error as PlaywrightError

from job.api.net import URL as PANEL_URL
from job.paths import USER_DATA_DIR
from job.utils import log

# 后台静默时窗口最小化，恢复（用户点任务栏）后要落在屏幕内。最小化态不允许
# 同时带坐标，所以最小化之前得先把「记忆坐标」摆正，否则恢复出来是个看不见的窗口。
HOME_BOUNDS = {"left": 60, "top": 60, "width": 1280, "height": 800}
# 「还在屏幕外」的判定：Windows 会把 -32000 钳到 -21845，都在这条线以下
OFFSCREEN_LEFT = -1000
# 卡在导航里的页面会让 CDP 调用永不返回（实测 ``CDPSession.detach`` 会永久挂住），
# 所有可能挂住的浏览器调用都得带上限，否则一次「查看浏览器」就能把整个请求钉死。
DETACH_TIMEOUT = 1.5
QUIET_TIMEOUT = 20.0
FRONT_TIMEOUT = 3.0
PLACE_TIMEOUT = 10.0
# 给空白页开首页的导航超时（毫秒）：首页是本地面板，正常远快于此
HOME_GOTO_TIMEOUT = 5000


class BossSession:
    """持久化 Chrome profile 的浏览器会话。"""

    # Chrome 自己报 profile 被占用时的特征（Playwright 启动参数里的 BlockOrigin... 也含 lock，不能只搜 lock）
    IN_USE_SIGNS = ("processsingleton", "singletonlock", "already in use")
    # 没人用过的空白标签页：Chrome 启动时自带的 about:blank 就在这一档
    BLANK_URLS = ("", "about:blank", "chrome://newtab/")
    # 调出浏览器时若只剩一张白纸，先给它一个像样的页面，别让用户对着空页发愣。
    # 用本地面板而不是 BOSS：BOSS 站点在部分网络下压根打不开，一次必然超时的导航
    # 会把页面卡在「正在加载」，后面的窗口操作全被它拖住（实测 detach 挂死）。
    HOME_URL = PANEL_URL

    def __init__(self, user_data_dir: Path | None = None) -> None:
        self.user_data_dir = user_data_dir or self.default_profile_dir()
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        # 用户点了「关闭浏览器」后置位：封锁一切自动重开（后台预查等会偷开浏览器）
        # 直到 resume() 被调用（开始投递/回复或点「查看浏览器」）
        self._closed_by_user = asyncio.Event()
        # 后台静默：窗口最小化在任务栏，任务新开标签页也不许把它弹到前台。
        # 只有用户自己点「查看浏览器」才解开，任务代码永远不许把窗口顶到前面。
        self.background = True
        # 用户此刻是不是正盯着浏览器看（点过「查看浏览器」或把「后台静默运行」关了）。
        # 只有它为 True 时开标签页才走前台方式；其余任何时候任务开页都不许弹窗。
        self.viewing = False

    @staticmethod
    def default_profile_dir() -> Path:
        """默认 Chrome 用户数据目录（项目内 ``user_data/chrome-profile``）。"""
        return USER_DATA_DIR / "chrome-profile"

    @staticmethod
    def chrome_executable() -> Path | None:
        """Windows 上挑一个能用的 Chrome；其他系统或找不到时返回 None，交给 channel 查找。

        Playwright 按 LOCALAPPDATA → Program Files 的顺序取第一个存在的 chrome.exe，
        卸载残留的旧版（只剩 chrome.exe、没有 chrome.dll）也会被选中而启动失败。
        """
        if sys.platform != "win32":
            return None
        drive = os.environ.get("HOMEDRIVE")
        prefixes = [
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            drive and f"{drive}\\Program Files",
            drive and f"{drive}\\Program Files (x86)",
        ]
        for prefix in filter(None, prefixes):
            app = Path(prefix) / "Google" / "Chrome" / "Application"
            exe = app / "chrome.exe"
            if exe.is_file() and any(app.glob("*/chrome.dll")):
                return exe
        return None

    @property
    def is_open(self) -> bool:
        """浏览器是否已打开。"""
        return self._context is not None

    def resume(self) -> None:
        """解除「手动关闭」封锁：开始投递/回复或点「查看浏览器」时调用。"""
        self._closed_by_user.clear()

    async def open(self) -> BrowserContext:
        """启动本机 Chrome；已打开则直接复用。

        用户点过「关闭浏览器」后拒绝重开——否则后台查企业等任务会把
        浏览器一个个偷偷拉起来，关了又弹。
        """
        if self._closed_by_user.is_set():
            raise RuntimeError("浏览器已被手动关闭，请点「查看浏览器」或重新开始任务")
        if self._context is None:
            self._context = await self._launch()
        return self._context

    async def page(self) -> Page:
        """复用一个已有标签页，没有就新建；浏览器被外部关闭/崩溃时自愈重建一次。

        优先挑非空白标签页：Chrome 启动时自带的 ``about:blank`` 只是占位，
        拿它去导航会把真正的页面挤到第二个标签，用户点「查看浏览器」先看到的
        就是一张白纸。
        """
        context = await self.open()
        try:
            return await self._usable_page(context)
        except PlaywrightError:
            # 窗口被用户手动关闭或浏览器崩溃：重建后重试（登录态在 profile 里不丢）
            await self.close()
            context = await self.open()
            return await self._usable_page(context)

    async def _usable_page(self, context: BrowserContext) -> Page:
        """挑一个能用的标签页：非空白优先；一个都没有才新建。"""
        for page in context.pages:
            if not self._is_blank(page):
                return page
        if context.pages:
            return context.pages[0]
        return await self._open_quiet(context)

    async def new_page(self) -> Page:
        """新开一个标签页；浏览器被外部关闭/崩溃时自愈重建一次。

        后台静默时走 :meth:`_open_quiet`：窗口保持最小化，不弹到前台抢焦点。
        """
        context = await self.open()
        try:
            return await self._open_quiet(context)
        except PlaywrightError:
            await self.close()
            context = await self.open()
            return await self._open_quiet(context)

    async def _open_quiet(self, context: BrowserContext) -> Page:
        """后台开一个标签页：窗口保持最小化，绝不弹到前台。

        ``context.new_page()`` 会把最小化的窗口恢复出来并抢焦点，正是打断用户
        前台干活的元凶；改用 CDP ``Target.createTarget(background=True)`` 开页，
        实测窗口状态原样不动。没得选时才退回常规方式（一个标签页都没有，或用户
        正盯着看）。
        """
        if not context.pages or self.viewing:
            return await context.new_page()
        cdp = await context.new_cdp_session(context.pages[0])
        try:
            before = list(context.pages)
            await cdp.send("Target.createTarget", {"url": "about:blank", "background": True})
            for _ in range(60):
                fresh = [page for page in context.pages if page not in before]
                if fresh:
                    return fresh[0]
                await asyncio.sleep(0.05)
        finally:
            await self._detach(cdp)
        log("后台新开标签页超时，退回常规方式")
        page = await context.new_page()
        await self._park_window(context)
        return page

    async def close(self, *, keep_closed: bool = False) -> None:
        """关闭浏览器与 Playwright。

        ``keep_closed`` 是用户主动关闭（工作台「关闭浏览器」）：置位封锁，
        之后任何任务都不能自动把浏览器拉起来，直到 ``resume()``。
        内部自愈重建用的关闭不带这个标记。
        """
        if keep_closed:
            self._closed_by_user.set()
        ctx, self._context = self._context, None
        pw, self._playwright = self._playwright, None
        if ctx is not None:
            await self._quietly(ctx.close(), "关闭浏览器")
        if pw is not None:
            await self._quietly(pw.stop(), "停止 Playwright")

    async def set_background(self, background: bool) -> bool:
        """把浏览器窗口最小化 / 调回屏幕，让后台静默模式运行中即时生效。

        通过 CDP 改窗口状态，不需要重启浏览器；想看一眼时在工作台把
        「后台静默运行」开关切掉即可，看完再打开最小化回任务栏。

        返回窗口是否真的挪到位，好让「查看浏览器」在失败时给用户一句实话，
        而不是点了没反应。
        """
        # 先记下用户的意图：后面任务再开标签页时，_open_quiet 要照这个来
        self.background = background
        # 关掉开关 = 用户现在想看着它跑，别再最小化
        self.viewing = not background
        if not background and not await self._revive():
            return False
        context = self._context
        if context is None or not context.pages:
            return False
        return await self._place_window(context, background=background)

    def watch_off(self) -> None:
        """用户开始一个新任务：把「正看着」的标记收掉，任务期间窗口一律不再上屏。

        用户点「查看浏览器」看过一眼之后又去干别的了，任务再开标签页就不该把
        窗口顶到他面前——每个任务开跑前调一次这里，回到「全程不打扰」的状态。
        """
        self.viewing = False

    async def surface(self) -> bool:
        """把窗口调到屏幕前给用户看一次，但不动「后台静默」这个长期偏好。

        用户点「填表 / 重扫 / 开官网」是他自己要去官网上核对，窗口得摆到眼前；
        这只是「此刻正看着它」，不是把设置里的「后台静默运行」关掉。早先这里
        直接复用 ``set_background(False)``，等于替用户改了设置：这一下之后每个
        查询任务都会把窗口顶到他面前，打断前台干活。想长期看着它请切工作台上
        的开关，走 :meth:`set_background`。
        """
        self.viewing = True
        if not await self._revive():
            return False
        context = self._context
        if context is None or not context.pages:
            return False
        return await self._place_window(context, background=False)

    async def _place_window(self, context: BrowserContext, *, background: bool) -> bool:
        """把窗口最小化到任务栏，或调到前台屏幕内，并回读确认。

        单独拆出来是为了让 ``_launch`` 也能复用：启动时窗口本来就按后台摆好，
        只有「后台静默运行」被关掉时才需要摆到屏幕上。
        """
        if not background:
            # 调出来给用户看之前先收掉多余的空白标签页，
            # 否则「查看浏览器」看到的第一眼就是一张白纸
            await self._tidy_blank_pages(context)
        # 空白标签页可能还留着（只剩它一个时不能关），所以置顶要看的是
        # 真正在干活的页面，而不是数组里的第一张
        front = next(
            (page for page in context.pages if not self._is_blank(page)),
            context.pages[0] if context.pages else None,
        )
        if front is None:
            return False
        if not background and self._is_blank(front):
            # 一个干活的页面都没有：这张白纸先开个首页，别让「查看浏览器」点开是空的
            await self._quietly(
                front.goto(
                    self.HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=HOME_GOTO_TIMEOUT,
                ),
                "打开首页",
            )
        ok, moved = await self._bounded(
            self._apply_bounds(context, front, background=background),
            PLACE_TIMEOUT,
            "摆放浏览器窗口",
        )
        return bool(moved) if ok else False

    async def _apply_bounds(
        self, context: BrowserContext, page: Page, *, background: bool
    ) -> bool:
        """按 ``background`` 摆窗口，回读确认；Chrome 吞请求时最多重试三遍。"""
        if background:
            return await self._minimize(context, page)
        return await self._restore(context, page)

    async def _minimize(self, context: BrowserContext, page: Page) -> bool:
        """把窗口最小化到任务栏；已经在任务栏就不折腾。

        最小化态不允许同时带坐标，所以分两步：先把「记忆坐标」摆回屏幕内
        （否则用户点任务栏恢复出来是个屏幕外的隐形窗口），再最小化。
        """
        state: Any = None
        for _ in range(3):
            try:
                cdp = await context.new_cdp_session(page)
                window_id = (await cdp.send("Browser.getWindowForTarget"))["windowId"]
                current = (await cdp.send("Browser.getWindowBounds", {"windowId": window_id}))[
                    "bounds"
                ]
                if current.get("windowState") == "minimized" and self._on_screen(current):
                    await self._detach(cdp)
                    return True
                if not self._on_screen(current):
                    await cdp.send(
                        "Browser.setWindowBounds",
                        {
                            "windowId": window_id,
                            "bounds": {"windowState": "normal", **HOME_BOUNDS},
                        },
                    )
                await cdp.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "minimized"}},
                )
                # 回读一次：Chrome 偶尔会把请求吞掉，这时得让上层知道
                moved = (await cdp.send("Browser.getWindowBounds", {"windowId": window_id}))[
                    "bounds"
                ]
                await self._detach(cdp)
            except PlaywrightError as exc:
                log(f"最小化浏览器窗口失败: {exc}")
                await asyncio.sleep(0.2)
                continue
            state = moved.get("windowState")
            if state == "minimized":
                return True
            await asyncio.sleep(0.2)
        log(f"浏览器窗口没能最小化（state={state}）")
        return False

    async def _restore(self, context: BrowserContext, page: Page) -> bool:
        """把窗口从任务栏调回屏幕内并提到最前，回读确认。

        「点查看浏览器没反应」的根子是这一步：窗口从最小化恢复时 Chrome 常把
        第一次 ``setWindowBounds`` 吃掉，而坐标和目标态合成一次调用时更容易被忽略。
        这里分两步走（先归位成普通窗口，再摆坐标），并逐次回读验证；位置对了还要
        把窗口提到最前，否则它可能只是挪到了屏幕上、却压在别的程序后面。
        """
        left: Any = None
        for _ in range(3):
            try:
                cdp = await context.new_cdp_session(page)
                window_id = (await cdp.send("Browser.getWindowForTarget"))["windowId"]
                await cdp.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "normal"}},
                )
                # 宽高一起给：只报 left/top 时 Chrome 更容易把整条请求吞掉
                await cdp.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {"windowState": "normal", **HOME_BOUNDS},
                    },
                )
                moved = (await cdp.send("Browser.getWindowBounds", {"windowId": window_id}))[
                    "bounds"
                ]
                await self._detach(cdp)
            except PlaywrightError as exc:
                log(f"调回浏览器窗口失败: {exc}")
                await asyncio.sleep(0.2)
                continue
            left = moved.get("left")
            if self._on_screen(moved):
                # 位置对了还不够：窗口可能压在别的程序后面，顺手提到最前
                await self._quietly(
                    page.bring_to_front(), "置顶浏览器窗口", timeout=FRONT_TIMEOUT
                )
                return True
            await asyncio.sleep(0.2)
        log(f"浏览器窗口没能调回屏幕（left={left}）")
        return False

    @staticmethod
    def _on_screen(bounds: dict[str, Any]) -> bool:
        """窗口的「记忆坐标」是否落在屏幕内（最小化态也能读到它）。"""
        left = bounds.get("left")
        return isinstance(left, int) and left > OFFSCREEN_LEFT

    @classmethod
    def _is_blank(cls, page: Page) -> bool:
        """是不是一张没人用过的空白标签页。"""
        url = (getattr(page, "url", "") or "").strip()
        return url in cls.BLANK_URLS

    async def _tidy_blank_pages(self, context: BrowserContext) -> None:
        """收掉多余的空白标签页，只留最后一个。

        Chrome 启动时自带一个 about:blank，各任务再各开一个标签页，用户点
        「查看浏览器」时排在第一个的就是那张白纸。最后一个不动——标签页全关掉
        的话浏览器窗口也跟着没了。
        """
        for page in list(context.pages):
            if len(context.pages) < 2:
                break
            if self._is_blank(page):
                await self._quietly(page.close(), "关闭空白标签页")

    async def _revive(self) -> bool:
        """调出窗口之前先把浏览器拉回可用状态，返回是否值得继续调窗口。

        用户点了窗口的 × 或 Chrome 进程被杀之后，``_context`` 可能还挂着却
        已经没有标签页；这时只调窗口位置毫无反应，必须先重建（登录态在
        profile 里，不会丢）。失败只记日志，不向外抛。
        """
        for _ in range(2):
            try:
                context = await self.open()
                if not context.pages:
                    await self._open_quiet(context)
                return True
            except RuntimeError as exc:
                log(f"浏览器不可用：{exc}")
                return False
            except PlaywrightError as exc:
                log(f"浏览器已失效，正在重新打开：{exc}")
                await self.close()
        return False

    async def _launch(self) -> BrowserContext:
        """启动 Playwright 并打开持久化 Chrome。

        窗口先按 ``--window-position`` 开在屏幕外（免得启动瞬间在眼前闪一下），
        紧接着最小化到任务栏：跑任务时不该自己弹出来打断用户。想看一眼时用
        「查看浏览器」，走 CDP 把窗口调回屏幕（无需重启浏览器）。
        """
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        exe = self.chrome_executable()
        browser: dict[str, Any] = {"executable_path": str(exe)} if exe else {"channel": "chrome"}
        args = ["--window-position=-32000,-32000", "--window-size=1280,800"]
        try:
            self._playwright = await async_playwright().start()
            context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=False,
                no_viewport=True,
                args=args,
                **browser,
            )
        except PlaywrightError as exc:
            await self.close()
            raise RuntimeError(self._launch_error(exc)) from exc
        # 窗口被用户手动关掉时清掉引用，is_open 才不会一直说谎
        context.on("close", lambda: self._forget(context))
        await self._park_window(context)
        if self.viewing:
            # 用户正主动看着浏览器（点「查看浏览器」带起来的）：启动完就摆到屏幕上，
            # 别让他对着空桌面等；任务自己拉起的浏览器一律留在任务栏。
            await self._place_window(context, background=False)
        return context

    async def _park_window(self, context: BrowserContext) -> None:
        """把窗口最小化到任务栏。

        启动参数 ``--window-position=-32000,-32000`` 只是让窗口别在启动瞬间闪一下；
        真正让窗口安静待在任务栏靠这里的最小化，顺带把「记忆坐标」摆回屏幕内，
        用户点任务栏随时能把它调出来。失败只记日志，不影响跑任务。
        """
        page = next(
            (item for item in context.pages if not self._is_blank(item)),
            context.pages[0] if context.pages else None,
        )
        if page is None:
            page = await context.new_page()
        await self._bounded(
            self._apply_bounds(context, page, background=True),
            PLACE_TIMEOUT,
            "摆放浏览器窗口",
        )

    def _forget(self, context: BrowserContext) -> None:
        """外部关闭事件：只清掉自己那一份引用，不干扰重建后的新会话。"""
        if self._context is context:
            self._context = None

    def _launch_error(self, exc: PlaywrightError) -> str:
        """把启动异常翻译成可读的中文提示。"""
        text = str(exc)
        msg = text.lower()
        if any(k in msg for k in self.IN_USE_SIGNS):
            return (
                f"Chrome profile 被占用：{self.user_data_dir}，"
                "请关闭其它使用该目录的 Chrome 后重试。"
            )
        if "distribution" in msg and "is not found" in msg:
            return "无法启动本机 Chrome（channel=chrome），请确认已安装 Google Chrome。"
        # 只留首行原因（去掉「BrowserType.xxx:」前缀），附上实际启动的 Chrome 路径
        reason = text.splitlines()[0].split(": ", 1)[-1] if text else "未知错误"
        if launched := re.search(r"<launching> (.+?) --", text):
            return f"启动 Chrome 失败：{reason}（{launched.group(1)}）"
        return f"启动 Chrome 失败：{reason}"

    @classmethod
    async def _quietly(
        cls, aw: Awaitable[Any], what: str, *, timeout: float = QUIET_TIMEOUT
    ) -> None:
        """清理阶段的失败只打印，不向外抛；卡住的页面会让清理动作挂住，加超时兜底。"""
        await cls._bounded(aw, timeout, what)

    @classmethod
    async def _detach(cls, cdp: CDPSession) -> None:
        """断开 CDP 会话。

        页面卡在导航里时 ``detach`` 会永久挂住（实测），而它正好挡在
        「调窗口」的必经路上——一次卡死就等于「查看浏览器」点了没反应。
        """
        await cls._bounded(cdp.detach(), DETACH_TIMEOUT, "断开 CDP 会话")

    @classmethod
    async def _bounded(cls, aw: Awaitable[Any], seconds: float, what: str) -> tuple[bool, Any]:
        """跑一个可能永久挂住的浏览器调用，到点就放手继续。

        不取消它：取消一个卡死的协议调用本身也可能挂住。只把结果交给回调收尾，
        调用方照常往下走——失败返回 ``(False, None)``，成功返回 ``(True, 返回值)``。
        """
        task = asyncio.ensure_future(aw)
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if not done:
            task.add_done_callback(cls._swallow)
            log(f"{what}超时（{seconds:g}s），跳过")
            return False, None
        try:
            return True, task.result()
        except PlaywrightError as exc:
            log(f"{what}失败: {exc}")
            return False, None

    @staticmethod
    def _swallow(task: asyncio.Task[Any]) -> None:
        """收尾被放弃的调用：取走异常，免得留下「异常从未被取回」的噪音。"""
        if not task.cancelled():
            task.exception()
