"""BOSS 浏览器会话：只管 Playwright / Chrome 的启动、复用与关闭。"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from patchright.async_api import BrowserContext, Page, Playwright, async_playwright
from patchright.async_api import Error as PlaywrightError

from job.paths import USER_DATA_DIR
from job.utils import log


class BossSession:
    """持久化 Chrome profile 的浏览器会话。"""

    # Chrome 自己报 profile 被占用时的特征（Playwright 启动参数里的 BlockOrigin... 也含 lock，不能只搜 lock）
    IN_USE_SIGNS = ("processsingleton", "singletonlock", "already in use")

    def __init__(self, user_data_dir: Path | None = None) -> None:
        self.user_data_dir = user_data_dir or self.default_profile_dir()
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        # 用户点了「关闭浏览器」后置位：封锁一切自动重开（后台预查等会偷开浏览器）
        # 直到 resume() 被调用（开始投递/回复或点「查看浏览器」）
        self._closed_by_user = asyncio.Event()

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
        """复用第一个标签页，没有就新建；浏览器被外部关闭/崩溃时自愈重建一次。"""
        context = await self.open()
        try:
            return context.pages[0] if context.pages else await context.new_page()
        except PlaywrightError:
            # 窗口被用户手动关闭或浏览器崩溃：重建后重试（登录态在 profile 里不丢）
            await self.close()
            context = await self.open()
            return context.pages[0] if context.pages else await context.new_page()

    async def new_page(self) -> Page:
        """新开一个标签页；浏览器被外部关闭/崩溃时自愈重建一次。"""
        context = await self.open()
        try:
            return await context.new_page()
        except PlaywrightError:
            await self.close()
            context = await self.open()
            return await context.new_page()

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

    async def set_background(self, background: bool) -> None:
        """把浏览器窗口移出 / 移回屏幕，让后台静默模式运行中即时生效。

        通过 CDP 改窗口位置，不需要重启浏览器；想看一眼时在工作台把
        「后台静默运行」开关切掉即可，看完再打开移回后台。
        """
        if self._context is None or not self._context.pages:
            return
        bounds: dict[str, Any] = {"windowState": "normal"}
        if background:
            bounds.update({"left": -32000, "top": -32000})
        else:
            bounds.update({"left": 60, "top": 60})
        try:
            cdp = await self._context.new_cdp_session(self._context.pages[0])
            window = await cdp.send("Browser.getWindowForTarget")
            await cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": bounds},
            )
        except PlaywrightError as exc:
            log(f"移动浏览器窗口失败: {exc}")

    async def _launch(self) -> BrowserContext:
        """启动 Playwright 并打开持久化 Chrome。

        「后台静默模式」开启时把窗口移出屏幕：任务照常运行但不弹出打扰；
        想看时在工作台切掉开关，窗口立即移回屏幕（无需重启浏览器）。
        """
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        exe = self.chrome_executable()
        browser: dict[str, Any] = {"executable_path": str(exe)} if exe else {"channel": "chrome"}
        args: list[str] = []
        try:
            from job.models.setting import BrowserSettings

            if BrowserSettings.load().background:
                args = ["--window-position=-32000,-32000", "--window-size=1280,800"]
        except Exception:  # noqa: BLE001 设置读不到就按可见模式启动
            pass
        try:
            self._playwright = await async_playwright().start()
            return await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=False,
                no_viewport=True,
                args=args or None,
                **browser,
            )
        except PlaywrightError as exc:
            await self.close()
            raise RuntimeError(self._launch_error(exc)) from exc

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

    @staticmethod
    async def _quietly(aw: Awaitable[None], what: str) -> None:
        """清理阶段的失败只打印，不向外抛。"""
        try:
            await aw
        except PlaywrightError as exc:
            log(f"{what}失败: {exc}")
