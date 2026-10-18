"""网申助手：资料合成、表单扫描、字段匹配、填表、任务与 API。"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from patchright.async_api import Error as PlaywrightError
from patchright.async_api import async_playwright

from job import models as models_pkg
from job.api.app import app
from job.apply import filler, knowledge, pipeline
from job.apply.assistant import ApplyAssistant, ApplyError, _alike
from job.apply.captcha import CaptchaSolver
from job.apply.filler import FillOutcome, FormFiller
from job.apply.locator import FieldLocator
from job.apply.matcher import (
    FieldMatcher,
    align_option,
    parse_drop_indexes,
    parse_merge_keys,
    parse_plan,
)
from job.apply.profile import ApplyProfile, ProfileEntry, resume_facts
from job.apply.safety import (
    DECLARATION,
    DERIVED,
    FACT,
    OPTIONAL,
    SENSITIVE,
    end_date_of,
    kind,
    order_by_end_date,
    public_entries,
    split_entries,
    unfilled,
    vague,
)
from job.apply.scanner import (
    FormField,
    FormScanner,
    guess_company,
    guess_title,
    normalize,
    read_page_meta,
)
from job.apply.vision import VisionChecker, VisionResult
from job.boss.jobs import Job
from job.boss.session import BossSession
from job.models import db_session, init_db, reset_engine
from job.models.application import (
    ApplicationRow,
    ApplyLearnedRow,
    ApplyMappingRow,
    ApplyRejectRow,
    domain_of,
)
from job.models.job import JobRow
from job.models.search import SearchConfigRow
from job.models.setting import (
    ApplyAutoSettings,
    ApplyLocateSettings,
    ApplyProfileSettings,
    ApplyVisionSettings,
    CaptchaSettings,
    LlmSettings,
)

# --- 公共脚手架 -------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield tmp_path
    reset_engine()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    with TestClient(app) as c:
        yield c
    reset_engine()


def _seed_job(job_id: str = "j1", **kw: str) -> str:
    """插一条岗位，返回它的 uid（BOSS 岗位用裸职位 ID 作主键）。"""
    data: dict[str, str] = {
        "job_id": job_id,
        "title": "AI 工程师",
        "company": "某公司",
        "salary": "20-30K",
        "location": "上海",
    }
    data.update(kw)
    JobRow.record(Job(**data))
    return job_id


def _profile(**kw: str) -> ApplyProfile:
    """直接拼一份资料，不落库。"""
    return ApplyProfile.from_sources(ApplyProfileSettings(**kw))


def _matcher(profile: ApplyProfile, domain: str = "") -> FieldMatcher:
    """未配置大模型的匹配器：AI 兜底会静默跳过，测试不触网。"""
    return FieldMatcher(profile, domain=domain)


def _field(**kw: Any) -> FormField:
    """直接拼一个字段；默认带上与序号一致的定位选择器。"""
    kw.setdefault("index", 0)
    kw.setdefault("selector", f'[data-gj-idx="{kw["index"]}"]')
    return FormField(**kw)


class _FakeLocator:
    """假定位器：命中集合里的选择器才算找得到，调用记到所属 frame 上。"""

    def __init__(self, frame: "_FakeFrame", selector: str, text: str = "") -> None:
        self.frame = frame
        self.selector = selector
        self.text = text

    async def count(self) -> int:
        return 1 if self.selector in self.frame.selectors else 0

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def all(self) -> list["_FakeLocator"]:
        """浮层选项：按 ``choices`` 里登记的文字各造一个可选定位器。"""
        return [_FakeLocator(self.frame, self.selector, text) for text in self.frame.choices(self.selector)]

    def locator(self, selector: str) -> "_FakeLocator":
        """在一个定位器里再找子节点（地区格子里那个小勾选框走的就是这条路）。"""
        return _FakeLocator(self.frame, selector)

    async def fill(self, value: str, **kw: Any) -> None:
        self.frame.calls.append(("fill", self.selector, value))
        if self.selector in self.frame.refuse_fill:
            raise PlaywrightError("Locator.fill: Timeout 3000ms exceeded.")
        self.frame.written[self.selector] = value

    async def focus(self, **kw: Any) -> None:
        self.frame.calls.append(("focus", self.selector, ""))

    async def press_sequentially(self, value: str, **kw: Any) -> None:
        self.frame.calls.append(("type", self.selector, value))
        self.frame.written[self.selector] = value

    async def select_option(self, value: str = "", **kw: Any) -> None:
        value = value or str(kw.get("value") or "")
        self.frame.calls.append(("select", self.selector, value))
        # 记下「这一级选中的显示文字」：级联的下一级要靠它才知道该刷新成什么
        text = next(
            (label for val, label in self.frame.native_options(self.selector) if val == value),
            "",
        )
        self.frame.selected[self.selector] = text or value

    async def check(self, **kw: Any) -> None:
        self.frame.calls.append(("check", self.selector, ""))

    async def click(self, **kw: Any) -> None:
        self.frame.calls.append(("click", self.selector, self.text))
        if self.text:
            self.frame.picked.append(self.text)
        if self.selector in self.frame.refuse_click:
            raise PlaywrightError("控件不支持直接上传")

    async def set_input_files(self, value: Any, **kw: Any) -> None:
        self.frame.calls.append(("file", self.selector, value))
        if self.selector in self.frame.refuse_upload:
            raise PlaywrightError("set_input_files: input[type=file] is hidden")
        if self.selector in self.frame.drop_upload:
            # 控件把文件吞了：调用没报错，但 input.files 一直是空的
            return
        self.frame.files[self.selector] = len(value) if isinstance(value, list) else 1
        # 模拟「官网拿简历解析结果覆盖已填栏」：上传时把登记过的栏改写掉
        self.frame.written.update(self.frame.overwrite_on_upload)
        # 模拟 webuploader：官网自己校验 / 异步上传，失败弹 alert，成功写回服务器路径
        if self.frame.upload_alert:
            self.frame.page.emit_dialog(self.frame.upload_alert)
        elif self.frame.upload_path.get(self.selector) is not None:
            self.frame.upload_path[self.selector] = "/upload/2026/demo-file.pdf"

    async def input_value(self, **kw: Any) -> str:
        if self.selector in self.frame.readback:
            return self.frame.readback[self.selector]
        if self.frame.picked:
            return " / ".join(self.frame.picked)
        return self.frame.written.get(self.selector, "")

    async def press(self, key: str, **kw: Any) -> None:
        self.frame.calls.append(("press", self.selector, key))

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self, **kw: Any) -> str:
        return self.text

    async def screenshot(self) -> bytes:
        self.frame.calls.append(("shot", self.selector, ""))
        if self.selector in self.frame.refuse_shot:
            raise PlaywrightError("截图失败：元素不可见")
        return b"png"

    async def evaluate(self, source: str, *args: Any) -> Any:
        if "gj-shot" in source:
            return self.frame.shot
        if "gj-drop" in source:
            if not self.frame.drop_ok:
                return False
            # 模拟「拖拽区真收了文件」：控件那边挂上了文件，调用方回读得到
            self.frame.calls.append(("drop", self.selector, args[0]["name"] if args else ""))
            self.frame.files[self.selector] = 1
            return True
        if "gj-options" in source:
            return self.frame.native_options(self.selector)
        if "gj-upload-path" in source:
            return self.frame.upload_path.get(self.selector)
        if "gj-files" in source:
            return self.frame.files.get(self.selector, self.frame.chooser_files)
        if "gj-accept" in source:
            return self.frame.accept.get(self.selector, "")
        if "gj-shown" in source:
            return self.frame.shown.get(self.selector, self.text)
        if "gj-selected" in source:
            # 原生下拉当前选中项的**文字**（``_SELECTED_JS``）
            return self.frame.selected.get(self.selector, "")
        if "gj-box" in source:
            return self.frame.box_height(self.selector)
        if "gj-probe" in source:
            return self.frame.probe.get(self.selector)
        if ".click()" in source:
            # DOM 点击（``_click_node``）：和 ``click`` 一样记一笔、一样算选中
            self.frame.calls.append(("click", self.selector, self.text))
            if self.text:
                self.frame.picked.append(self.text)
            return None
        return None


class _FakeDialog:
    """假 alert：官网校验不过时弹出来的那种。"""

    def __init__(self, message: str) -> None:
        self.message = message

    async def dismiss(self) -> None:
        return None


class _FakeChooser:
    """假文件选择框：``await chooser.value`` 拿到它，``set_files`` 记一笔。"""

    def __init__(self, page: "_FakePage") -> None:
        self.page = page

    def __await__(self) -> Any:
        async def _self() -> "_FakeChooser":
            return self

        return _self().__await__()

    async def set_files(self, path: str) -> None:
        self.page.frame.calls.append(("chooser", "", path))
        # 走系统文件选择框这条路：控件那边也会挂上文件
        self.page.frame.chooser_files += 1


class _FakeChooserContext:
    """``page.expect_file_chooser()`` 的假上下文管理器。"""

    def __init__(self, page: "_FakePage") -> None:
        self.value = _FakeChooser(page)

    async def __aenter__(self) -> "_FakeChooserContext":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeFrame:
    """假 frame：``evaluate`` 按脚本内容区分「取控件」还是「取正文」。"""

    def __init__(
        self,
        raw: list[dict[str, Any]] | None = None,
        text: str = "",
        selectors: Any = (),
        meta: dict[str, str] | None = None,
        captcha: Any = None,
        choices: dict[str, list[str]] | None = None,
        native: dict[str, list[list[str]]] | None = None,
        readback: dict[str, str] | None = None,
        refuse_upload: Any = (),
        refuse_click: Any = (),
        shot: str = '[data-gj-shot="1"]',
        refuse_shot: Any = (),
        overwrite_on_upload: dict[str, str] | None = None,
        upload_path: dict[str, str] | None = None,
        upload_alert: str = "",
        tall: Any = (),
        shown: dict[str, str] | None = None,
        accept: dict[str, str] | None = None,
        drop_upload: Any = (),
        lazy_native: dict[str, tuple[str, list[list[str]]]] | None = None,
        drop_ok: bool = False,
        size: dict[str, Any] | None = None,
        found: dict[str, dict[str, Any]] | None = None,
        errors: list[dict[str, str]] | None = None,
        late_choices: dict[str, list[str]] | None = None,
        probe: dict[str, dict[str, Any]] | None = None,
        refuse_fill: Any = (),
    ) -> None:
        self.raw = raw or []
        self.text = text
        self.selectors = set(selectors)
        self.meta = meta or {}
        self.captcha = captcha
        self._choices = choices or {}
        self._native = native or {}
        self.readback = readback or {}
        self.refuse_upload = set(refuse_upload)
        self.refuse_click = set(refuse_click)
        # 登记成「直接 fill 写不进去」的控件：组件库把真正的 input 压在浮层下面，
        # Playwright 的 fill 一直等到超时（``Locator.fill: Timeout 3000ms exceeded``）
        self.refuse_fill = set(refuse_fill)
        self.shot = shot
        self.refuse_shot = set(refuse_shot)
        self.overwrite_on_upload = dict(overwrite_on_upload or {})
        # 上传控件旁边那个「已传文件路径」的只读栏：登记了才回读，否则当官网没法回读
        self.upload_path = dict(upload_path or {})
        # 官网自己校验不过时弹的 alert 文案
        self.upload_alert = upload_alert
        # 控件声明的接收类型（``accept``）：喂文件前会先拿它自查一遍
        self.accept = dict(accept or {})
        # 登记成「把文件吞了」的控件：调用不报错，但 input.files 一直空着
        self.drop_upload = set(drop_upload)
        # 登记成「拖拽区真会收文件」：派 drop 事件后控件那边挂上文件
        self.drop_ok = drop_ok
        # 控件自己挂上的文件数；走系统文件选择框那条路记在 chooser_files 上
        self.files: dict[str, int] = {}
        self.chooser_files = 0
        # 级联的下一级：``selector -> (触发文字, 刷新后的选项表)``。上一级选中那句
        # 文字之前只给占位项，之后才「由接口刷出来」，模拟真实官网的异步级联。
        self.lazy_native = dict(lazy_native or {})
        # 每一级当前选中的显示文字（键是选择器）
        self.selected: dict[str, str] = {}
        # 登记成「真展开」的浮层容器（其余同名容器按 0 高的占位壳处理）
        self.tall = set(tall)
        # 控件外层展示出来的文字（``_SHOWN_JS`` 读的那一处）：组件库把选中值挂在这里
        self.shown = dict(shown or {})
        self.page: "_FakePage | None" = None
        self.picked: list[str] = []
        self.written: dict[str, str] = {}
        self.calls: list[tuple[str, str, str]] = []
        # 视觉核对用：页面总高 / 视口高，以及「按名字找回控件」的登记表
        self.size = dict(size or {})
        self.found = dict(found or {})
        # 官网自己弹的校验报错：``[{"idx": "0", "text": "请填写正确的手机号"}]``
        self.errors = [dict(row) for row in (errors or [])]
        # 要滚动一屏才渲染出来的选项：``selector -> 选项文字``（第一次滚动后才出现在 DOM 里）
        self._late_choices = {key: list(val) for key, val in (late_choices or {}).items()}
        # 浮层滚过几次（``gj-scroll`` 记的账）：滚动是「渲染更多选项」的触发条件
        self.scrolls = 0
        # 控件是不是「点一下才弹浮层」的自研下拉（``gj-probe`` 读的那份）
        self.probe = dict(probe or {})
        self.marks: list[str] = []

    async def evaluate(self, source: str, *args: Any) -> Any:
        if "gj-vision-find" in source:
            return self.found.get(args[0]["text"]) if args else None
        if "gj-vision-size" in source:
            return dict(self.size)
        if "gj-vision" in source:
            self.marks.append(source)
            return 1
        # 校验报错脚本里也含 ``data-gj-idx`` 这个串，必须先于「取控件」那条分支判掉
        if "gj-errors" in source:
            return list(self.errors)
        if "data-gj-idx" in source:
            return self.raw
        if "gj-captcha" in source:
            return self.captcha
        if "og:site_name" in source:
            return self.meta
        if "gj-scroll" in source:
            # 滚一屏：登记过「滚动后才出来的选项」就算滚得动
            self.scrolls += 1
            return 1 if self._late_choices else 0
        return self.text

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    def choices(self, selector: str) -> list[str]:
        """某个浮层选择器下的选项文字。"""
        texts = list(self._choices.get(selector, []))
        if self.scrolls:
            texts.extend(self._late_choices.get(selector, []))
        return texts

    def native_options(self, selector: str) -> list[list[str]]:
        """原生 select 的 ``[value, text]`` 选项表。"""
        lazy = self.lazy_native.get(selector)
        if lazy is not None:
            trigger, options = lazy
            if trigger in self.selected.values():
                return list(options)
        return list(self._native.get(selector, []))

    def box_height(self, selector: str) -> float:
        """浮层容器的高度：登记在 ``tall`` 里的才算「真展开」，其余是 0 高的占位壳。"""
        return 120.0 if selector in self.tall else 0.0


class _FakeMouse:
    """假鼠标：只记「点了空白处」这一件事（收浮层用）。"""

    def __init__(self, page: "_FakePage") -> None:
        self.page = page

    async def click(self, x: float, y: float) -> None:
        self.page.calls.append(("mouse", f"{x},{y}", ""))


class _FakeKeyboard:
    """假键盘：只记按了什么键。"""

    def __init__(self, page: "_FakePage") -> None:
        self.page = page

    async def press(self, key: str) -> None:
        self.page.calls.append(("key", key, ""))


class _FakePage:
    """假标签页：一个 frame 承载全部控件。"""

    def __init__(
        self,
        raw: list[dict[str, Any]] | None = None,
        text: str = "",
        selectors: Any = (),
        frame_count: int = 1,
        meta: dict[str, str] | None = None,
        captcha: Any = None,
        choices: dict[str, list[str]] | None = None,
        native: dict[str, list[list[str]]] | None = None,
        readback: dict[str, str] | None = None,
        refuse_upload: Any = (),
        refuse_click: Any = (),
        shot: str = '[data-gj-shot="1"]',
        refuse_shot: Any = (),
        overwrite_on_upload: dict[str, str] | None = None,
        upload_path: dict[str, str] | None = None,
        upload_alert: str = "",
        tall: Any = (),
        shown: dict[str, str] | None = None,
        accept: dict[str, str] | None = None,
        drop_upload: Any = (),
        lazy_native: dict[str, tuple[str, list[list[str]]]] | None = None,
        drop_ok: bool = False,
        size: dict[str, Any] | None = None,
        found: dict[str, dict[str, Any]] | None = None,
        errors: list[dict[str, str]] | None = None,
        late_choices: dict[str, list[str]] | None = None,
        probe: dict[str, dict[str, Any]] | None = None,
        refuse_fill: Any = (),
    ) -> None:
        self.frame = _FakeFrame(
            raw,
            text,
            selectors,
            meta,
            captcha,
            choices,
            native,
            readback,
            refuse_upload,
            refuse_click,
            shot,
            refuse_shot,
            overwrite_on_upload,
            upload_path,
            upload_alert,
            tall,
            shown,
            accept,
            drop_upload,
            lazy_native,
            drop_ok,
            size,
            found,
            errors,
            late_choices=late_choices,
            probe=probe,
            refuse_fill=refuse_fill,
        )
        self.frame.page = self
        self.main_frame = self.frame
        self._frames = [self.frame] * frame_count
        self.goto_calls: list[str] = []
        # 真实标签页有 url；重开面板时靠它判断「已经在这个地址上了」
        self.url = "about:blank"
        # 视觉核对在页面上截了几张图
        self.shots = 0
        self._closed = False
        self._listeners: dict[str, list[Any]] = {}
        self.mouse = _FakeMouse(self)
        self.keyboard = _FakeKeyboard(self)

    @property
    def frames(self) -> list[_FakeFrame]:
        return self._frames

    @property
    def calls(self) -> list[tuple[str, str, str]]:
        return self.frame.calls

    async def goto(self, url: str, **kw: Any) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def evaluate(self, source: str, *args: Any) -> Any:
        return await self.frame.evaluate(source, *args)

    async def screenshot(self, **kw: Any) -> bytes:
        self.shots += 1
        return b"png"

    async def wait_for_timeout(self, ms: float) -> None:
        """等表单渲染用的：假页面里直接返回，不真的睡。"""
        return None

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True

    def expect_file_chooser(self, **kw: Any) -> "_FakeChooserContext":
        """拖拽上传兜底：模拟「点一下控件会弹出系统文件选择框」。"""
        return _FakeChooserContext(self)

    def on(self, event: str, handler: Any) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        handlers = self._listeners.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def emit_dialog(self, message: str) -> None:
        """模拟官网弹 alert：交给登记过的 dialog 处理器。"""
        for handler in list(self._listeners.get("dialog", [])):
            handler(_FakeDialog(message))


class _FakeSession:
    """假会话：只记 new_page / surface / set_background 的调用。"""

    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.pages: list[_FakePage] = []
        self.background: list[bool] = []
        self.surfaced = 0
        self.resumed = 0

    async def new_page(self) -> _FakePage:
        self.pages.append(self.page)
        return self.page

    async def set_background(self, value: bool) -> None:
        self.background.append(value)

    async def surface(self) -> bool:
        """用户点「填表 / 重扫」时把窗口摆到眼前一次——不动「后台静默」这个设置。"""
        self.surfaced += 1
        return True

    def resume(self) -> None:
        """解开「手动关闭」封锁：网申入口每次都会调一次，记下来供断言。"""
        self.resumed += 1


# --- 资料：从简历正文里捞事实 -------------------------------------------------


def test_resume_facts_picks_phone_email_and_links():
    text = "李四\n手机：13800138000\n邮箱 li.si@example.com\nhttps://lisi.dev\nhttps://github.com/lisi\n"
    facts = resume_facts(text)
    assert facts["name"] == "李四"
    assert facts["phone"] == "13800138000"
    assert facts["email"] == "li.si@example.com"
    assert facts["portfolio"] == "https://lisi.dev"
    assert facts["github"] == "https://github.com/lisi"


def test_resume_facts_prefers_explicit_name():
    facts = resume_facts("个人简历\n姓名：王五\n电话 13900139000")
    assert facts["name"] == "王五"
    assert facts["phone"] == "13900139000"


def test_resume_facts_returns_nothing_for_empty_text():
    assert resume_facts("") == {}
    assert resume_facts("个人简历") == {}  # 只有标题，不当姓名


# --- 资料合成：结构化优先，简历兜底 -------------------------------------------


def test_profile_structured_first_then_resume(tmp_db):
    settings = ApplyProfileSettings(name="张三", phone="13900139000")
    resume = "姓名：李四\n邮箱 li@example.com\n13800138000"
    profile = ApplyProfile.from_sources(settings, resume)
    values = profile.values()
    assert values["name"] == "张三"  # 结构化字段优先
    assert values["phone"] == "13900139000"
    assert values["email"] == "li@example.com"  # 结构化没填，用简历兜底
    assert profile.entry("name").source == "资料"
    assert profile.entry("email").source == "简历"
    assert profile.labels()["name"] == "姓名"
    assert len(profile) == 3 and profile
    assert profile.get("nope") == "" and profile.entry("nope") is None


def test_profile_load_reads_saved_settings(tmp_db):
    ApplyProfileSettings(name="张三", phone="13900139000").save()
    SearchConfigRow.save(resume_text="姓名：张三\n13800138000")
    profile = ApplyProfile.load()
    assert profile.values()["name"] == "张三"
    assert profile.values()["phone"] == "13900139000"
    assert profile.resume  # 简历正文一并带上，供 AI 兜底参考


def test_profile_remembers_learned_field(tmp_db):
    """本机在别家官网手工补过、资料库里本来没有的那条，按「记忆」来源补进资料。"""
    profile = ApplyProfile.from_sources(ApplyProfileSettings(name="张三"), learned={"hobby": "游泳"})
    assert profile.get("hobby") == "游泳" and profile.entry("hobby").source == "记忆"
    assert profile.entry("hobby").label == "兴趣爱好"  # 内置键用内置标签
    # 库里已经有的（含推算出来的）不被记忆覆盖：记忆只补空档
    covered = ApplyProfile.from_sources(
        ApplyProfileSettings(name="张三", degree="本科"), learned={"degree": "博士"}
    )
    assert covered.get("degree") == "本科"
    # 空值不补；库里没有这个键就用键名当标签
    blank = ApplyProfile.from_sources(ApplyProfileSettings(name="张三"), learned={"民族": "  "})
    assert blank.get("民族") == ""
    other = ApplyProfile.from_sources(ApplyProfileSettings(name="张三"), learned={"民族": "汉族"})
    assert other.entry("民族").label == "民族"


def test_apply_profile_settings_ready_and_filled(tmp_db):
    blank = ApplyProfileSettings.load()
    assert blank.filled() == {} and not blank.ready
    assert ApplyProfileSettings(name="张三", phone="13900139000").ready
    assert ApplyProfileSettings(name="张三", email="a@b.com").ready
    assert not ApplyProfileSettings(name="张三").ready  # 只有姓名还不够
    filled = ApplyProfileSettings(name=" 张三 ", phone="").filled()
    assert filled["name"] == "张三" and "phone" not in filled


# --- 扫描：把页面控件读成字段 -------------------------------------------------


def test_normalize_drops_punct_and_case():
    assert normalize(" 姓名 ： *张三* ") == "姓名张三"
    assert normalize("Full Name") == "fullname"


def test_form_field_from_raw_prefers_label():
    raw = {
        "tag": "input",
        "type": "text",
        "name": "fullname",
        "id": "u_name",
        "placeholder": "请输入姓名",
        "labelText": "姓名",
        "nearText": "姓名",
        "required": True,
        "options": [],
        "value": "",
        "checked": False,
        "path": "form > input",
    }
    field = FormField.from_raw(raw, index=3, frame=1)
    assert field.label == "姓名" and field.frame == 1 and field.index == 3
    assert field.required and field.selector == '[data-gj-idx="3"]'
    assert field.fallback == "form > input"
    assert field.signature() == "姓名|text"
    assert not field.is_file


def test_form_field_label_falls_back_to_name():
    field = FormField.from_raw({"tag": "input", "type": "text", "name": "email"}, index=0)
    assert field.label == "email" and field.signature() == "email|text"
    assert field.normalized == normalize(field.text)


def test_form_field_locator_uses_per_frame_slot():
    """iframe 表单：``data-gj-idx`` 是按 frame 各自从 0 数的，不能拿全页面连号去定位。

    采集脚本每个 frame 都从 0 开始打号，而 ``index`` 是跨 frame 连号的；第二个
    frame 里的第 3 个控件，全局号可能是 9。用 9 去查会摸到别的一栏，必须用 2。
    """
    raw = {"tag": "input", "type": "text", "labelText": "姓名", "idx": 2}
    field = FormField.from_raw(raw, index=9, frame=1)
    assert field.index == 9 and field.frame == 1
    assert field.selector == '[data-gj-idx="2"]'


def test_form_field_label_uses_previous_cell_text():
    """表格型官网表单：控件所在单元格没文字时，用左边那一格的标签。"""
    raw = {
        "tag": "input",
        "type": "text",
        "name": "Resume_Email",
        "ownText": "",
        "siblingText": "电子邮箱：",
    }
    field = FormField.from_raw(raw, index=9)
    assert field.label == "电子邮箱：" and field.signature() == "电子邮箱|text"


def test_form_field_label_ignores_select_option_text():
    """下拉框的选项文字（男 / 女）不是这一栏的标签，得看左边那格。"""
    raw = {
        "tag": "select",
        "type": "select",
        "name": "sex",
        "ownText": "",
        "siblingText": "性别：",
        "options": ["男", "女"],
    }
    field = FormField.from_raw(raw, index=1)
    assert field.label == "性别：" and field.options == ["男", "女"]


def test_form_field_label_prefers_own_container_over_previous_cell():
    """容器自己写着标签时（<div><span>姓名</span><input></div>）优先用它。"""
    raw = {"tag": "input", "type": "text", "ownText": "姓名", "siblingText": "性别："}
    field = FormField.from_raw(raw, index=0)
    assert field.label == "姓名"


def test_form_field_label_uses_cell_text_when_container_is_silent():
    """表格里一格塞了多个控件（生源地那三个下拉）：容器没字时用左边那格。"""
    raw = {
        "tag": "select",
        "type": "select",
        "name": "Resume_OriginCity",
        "ownText": "",
        "cellText": "生源地：",
        "siblingText": "",
        "options": ["所属城市"],
    }
    field = FormField.from_raw(raw, index=12)
    assert field.label == "生源地：" and field.signature() == "生源地|select"


def test_form_field_label_skips_file_button_text():
    """文件上传旁边的「浏览…」是按钮字不是标签，真标签在左边那一格。"""
    raw = {
        "tag": "input",
        "type": "file",
        "name": "file",
        "ownText": "浏览...",
        "cellText": "简历上传：",
        "siblingText": "浏览...",
    }
    field = FormField.from_raw(raw, index=15)
    assert field.label == "简历上传：" and field.signature() == "简历上传|file"


def test_form_field_label_falls_back_when_every_candidate_is_button_text():
    """候选全是按钮字时退回 name，别把「浏览」当成字段名。"""
    raw = {"tag": "input", "type": "file", "name": "resume_file", "ownText": "选择文件"}
    field = FormField.from_raw(raw, index=0)
    assert field.label == "resume_file"


def test_form_field_round_trip_keeps_locator_and_file_flag():
    raw = {"tag": "input", "type": "file", "name": "resume", "labelText": "上传简历", "path": "#f"}
    field = FormField.from_raw(raw, index=7)
    assert field.is_file
    restored = FormField.from_dict(field.to_dict())
    assert restored.index == 7 and restored.label == "上传简历"
    assert restored.selector == '[data-gj-idx="7"]' and restored.fallback == "#f"
    assert restored.is_file


def test_form_field_to_dict_rounds_confidence():
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0)
    field.confidence = 0.9067
    assert field.to_dict()["confidence"] == 0.91


def test_scanner_reads_every_frame():
    raw = [{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}]
    page = _FakePage(raw=raw, selectors={'[data-gj-idx="0"]'})
    fields = asyncio.run(FormScanner(None).scan(page))
    assert len(fields) == 1 and fields[0].label == "姓名" and fields[0].frame == 0


class _Overlap:
    """记「同时有几个在跑」：并发跑起来峰值才会大于 1。"""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0

    def enter(self) -> None:
        self.live += 1
        self.peak = max(self.peak, self.live)

    def leave(self) -> None:
        self.live -= 1


def test_scanner_scan_reads_frames_concurrently():
    """各 frame 并发取回（峰值 > 1），但结果仍按 frame 顺序、跨 frame 连号。"""

    class _Frame:
        def __init__(self, raw: dict[str, Any], overlap: _Overlap) -> None:
            self.raw = raw
            self.overlap = overlap

        async def evaluate(self, source: str, *args: Any) -> Any:
            self.overlap.enter()
            try:
                await asyncio.sleep(0.01)  # 让出事件循环：串行实现这里就露馅
                return [self.raw]
            finally:
                self.overlap.leave()

    class _Page:
        def __init__(self, frames: list[Any]) -> None:
            self.frames = frames

    overlap = _Overlap()
    raws = [
        {"tag": "input", "type": "text", "labelText": "甲", "idx": 0},
        {"tag": "input", "type": "text", "labelText": "乙", "idx": 0},
        {"tag": "input", "type": "text", "labelText": "丙", "idx": 0},
    ]
    page = _Page([_Frame(raw, overlap) for raw in raws])
    fields = asyncio.run(FormScanner(None).scan(page))
    assert [field.label for field in fields] == ["甲", "乙", "丙"]
    assert [field.frame for field in fields] == [0, 1, 2]
    assert [field.index for field in fields] == [0, 1, 2]
    assert overlap.peak == 3


def test_readback_reads_fields_concurrently(tmp_db):
    """回读并发跑（峰值 > 1 且不超上限）：一百多栏串着读光等往返就要好几秒。"""
    overlap = _Overlap()

    class _Locator:
        async def count(self) -> int:
            return 1

        @property
        def first(self) -> "_Locator":
            return self

        async def input_value(self, **kw: Any) -> str:
            overlap.enter()
            try:
                await asyncio.sleep(0.01)
                return "值"
            finally:
                overlap.leave()

    class _Frame:
        def locator(self, selector: str) -> _Locator:
            return _Locator()

    class _Page:
        def __init__(self) -> None:
            self.frames = [_Frame()]

        @property
        def main_frame(self) -> _Frame:
            return self.frames[0]

    fields = [_field(index=i) for i in range(24)]
    asyncio.run(FormFiller(None).readback(_Page(), fields))
    assert all(field.shown == "值" for field in fields)
    assert 1 < overlap.peak <= filler._READBACK_CONCURRENCY


def test_scanner_gate_detects_captcha():
    assert "滑块" in asyncio.run(FormScanner(None).gate(_FakePage(text="请拖动滑块完成验证")))
    assert asyncio.run(FormScanner(None).gate(_FakePage(text="请填写您的姓名"))) == ""


def test_scanner_gate_ignores_captcha_field_label():
    """正文里有「验证码：」这一栏文字，不算拦路虎——不然每次扫描都误判。"""
    text = "验证码： 请输入右侧验证码 简历上传： 生活照上传："
    assert asyncio.run(FormScanner(None).gate(_FakePage(text=text))) == ""


def test_read_page_meta_tolerates_bad_payload():
    assert read_page_meta(None) == {"title": "", "site": "", "h1": "", "host": ""}
    assert read_page_meta("oops") == {"title": "", "site": "", "h1": "", "host": ""}
    assert read_page_meta({"title": " x ", "host": "a.cn"})["title"] == "x"


def test_guess_company_prefers_site_then_title_tail():
    assert guess_company({"site": "云图科技"}) == "云图科技"
    assert guess_company({"title": "后端开发实习生 - 云图科技"}) == "云图科技"
    assert guess_company({"site": "人才招聘", "title": "招聘"}) == ""


def test_guess_title_prefers_h1_then_title_head():
    assert guess_title({"h1": "后端开发实习生"}) == "后端开发实习生"
    assert guess_title({"title": "后端开发实习生 | 云图科技"}) == "后端开发实习生"
    assert guess_title({"title": "加入我们"}) == ""


def test_scanner_meta_reads_first_frame_with_content():
    page = _FakePage(meta={"title": "校园招聘 - 云图科技", "host": "jobs.yuntu.cn"})
    meta = asyncio.run(FormScanner(None).meta(page))
    assert meta["title"] == "校园招聘 - 云图科技" and meta["host"] == "jobs.yuntu.cn"


# --- 网申知识：确定性推算 / 同义归并 / 按表单提示变形 ---------------------------


def test_knowledge_degree_level_normalizes_wording():
    assert knowledge.degree_level("全日制本科") == "本科"
    assert knowledge.degree_level("大学本科") == "本科"
    assert knowledge.degree_level("硕士研究生") == "硕士"
    assert knowledge.degree_level("大专") == "专科"
    assert knowledge.degree_level("") == "" and knowledge.degree_level("在读") == ""


def test_knowledge_split_major_reads_code_and_name():
    assert knowledge.split_major("信息安全（080904K）") == ("080904K", "信息安全")
    assert knowledge.split_major("计算机科学与技术 080901") == ("080901", "计算机科学与技术")
    assert knowledge.split_major("汉语言文学") == ("", "汉语言文学")
    assert knowledge.split_major("") == ("", "")


def test_knowledge_major_info_by_code_and_by_name():
    """代码优先；没有代码时按专业名里的关键词认。"""
    assert knowledge.major_info("信息安全 080904K") == ("计算机类", "工学")
    assert knowledge.major_info("信息安全") == ("计算机类", "工学")
    assert knowledge.major_info("汉语言文学") == ("中国语言文学类", "文学")
    assert knowledge.major_info("水产养殖学") is None


def test_knowledge_derive_fills_only_blank_keys():
    """本科 → 学士；信息安全 → 计算机类 / 工学 → 工学学士学位；已经写过的键一个都不动。"""
    derived = dict(
        (key, value) for key, value, _ in knowledge.derive({"degree": "本科", "major": "信息安全 080904K"})
    )
    assert derived == {
        "degree_name": "学士",
        "major_category": "计算机类",
        "discipline": "工学",
        "degree_full": "工学学士学位",
    }
    assert all(basis for _, _, basis in knowledge.derive({"degree": "本科"}))
    # 用户自己填过的学位不动，专业类也一样
    kept = knowledge.derive({"degree": "本科", "degree_name": "工学学士", "major": "信息安全"})
    assert "degree_name" not in {key for key, _, _ in kept}
    # 学位里已经带着学科门类（工学学士）时不再套一层，免得拼成「工学工学学士学位」
    assert "degree_full" not in {key for key, _, _ in kept}


def test_knowledge_derive_backfills_degree_from_degree_name():
    """资料里只写了学位（学士），表单问学历时反推「本科」。"""
    assert ("degree", "本科") in {(k, v) for k, v, _ in knowledge.derive({"degree_name": "学士"})}
    assert knowledge.derive({"degree_name": "专科毕业证"}) == []


def test_knowledge_canonical_key_merges_synonyms():
    assert knowledge.canonical_key("ethnicity") == "nation"
    assert knowledge.canonical_key("stature") == "height"
    assert knowledge.canonical_key("majorclass") == "major_category"
    assert knowledge.canonical_key("自定义字段") == "自定义字段"
    assert knowledge.canonical_key("") == ""


def test_knowledge_unit_in_text_reads_hint():
    assert knowledge.unit_in_text("身高(cm)") == "cm"
    assert knowledge.unit_in_text("身高（厘米）") == "cm"
    assert knowledge.unit_in_text("体重 kg") == "kg"
    assert knowledge.unit_in_text("身高（米）") == "m"
    # form / mail 里的 m 不算单位
    assert knowledge.unit_in_text("form") == "" and knowledge.unit_in_text("邮箱 mail") == ""
    assert knowledge.unit_in_text("") == ""


def test_knowledge_adapt_shapes_height_by_hint():
    """同一份身高按表单提示换写法；看不出提示就原样照抄。"""
    assert knowledge.adapt("height", "175cm", label="身高(cm)") == "175"
    assert knowledge.adapt("height", "175", label="身高（米）") == "1.75"
    assert knowledge.adapt("height", "1.75", label="身高(cm)") == "175"
    assert knowledge.adapt("height", "175", label="身高", hints={"min": "1", "max": "3"}) == "1.75"
    assert knowledge.adapt("height", "175", label="身高", field_type="number") == "175"
    # 没有单位提示、也没说只要数字：不动用户写的那份
    assert knowledge.adapt("height", "175cm", label="身高") == "175cm"
    # 非度量字段一律原样返回
    assert knowledge.adapt("school", "云图大学", label="毕业学校(cm)") == "云图大学"
    # 读不出数值就别改
    assert knowledge.adapt("height", "保密", label="身高(cm)") == "保密"


def test_knowledge_adapt_shapes_weight_by_hint():
    assert knowledge.adapt("weight", "60", label="体重(kg)") == "60"
    assert knowledge.adapt("weight", "60", label="体重", hints={"unit": "斤"}) == "120"
    assert knowledge.adapt("weight", "55kg", label="体重（克）") == "55000"


def test_knowledge_fresh_status_judges_graduate_kind():
    """应届 / 留学归国 / 往届：只看资料里的硬证据，看不出依据就留空。"""
    assert knowledge.fresh_status({"graduate_year": "2026年6月"})[0] == "国内应届毕业生"
    assert knowledge.fresh_status({"graduate_year": "2019"})[0] == "往届毕业生"
    # 交换 / 留学经历写着留学，或学校名是外文 → 留学归国
    assert knowledge.fresh_status({"exchange": "大三赴海外交换一学期"})[0] == "留学归国人员"
    assert knowledge.fresh_status({"school": "Yuntu University"})[0] == "留学归国人员"
    # 读不出年份、也没有境外证据：不硬说
    assert knowledge.fresh_status({"school": "云图大学"}) == ("", "")
    assert knowledge.fresh_status({}) == ("", "")


def test_knowledge_canonical_key_merges_chinese_labels():
    """官网上的中文栏名也归到内置键：换个叫法不会再存成新的一条。"""
    assert knowledge.canonical_key("民族") == "nation"
    assert knowledge.canonical_key("专业类别") == "major_category"
    assert knowledge.canonical_key("籍贯") == "native_place"
    assert knowledge.canonical_key("nation") == "nation"


def test_knowledge_canonical_key_merges_site_wording():
    """各家官网把同一栏写出好几种名字：都归到同一个键，库里才不会有重复条。"""
    assert knowledge.canonical_key("学校名称") == "school"
    assert knowledge.canonical_key("学院名称") == "college_name"
    assert knowledge.canonical_key("专业名称") == "major"
    assert knowledge.canonical_key("出生日期") == "birth"
    assert knowledge.canonical_key("毕业时间") == "graduate_year"
    assert knowledge.canonical_key("户口所在地") == "hukou"
    assert knowledge.canonical_key("自我评价") == "self_intro"
    assert knowledge.canonical_key("技能/兴趣爱好") == "skills"
    assert knowledge.canonical_key("奖励/证书名称") == "awards"
    assert knowledge.canonical_key("证书描述") == "certificates"
    # 尾巴上挂着单位的：去掉单位照样认得出
    assert knowledge.canonical_key("身高(厘米)") == "height"
    assert knowledge.canonical_key("体重（公斤）") == "weight"
    # 认不出的还是原样返回，别硬往某个键上靠
    assert knowledge.canonical_key("自定义字段") == "自定义字段"


def test_knowledge_canonical_key_keeps_distinct_builtin_fields():
    """内置字段各归各的：学院/院系不是毕业学校，别名表不能把两者并成一条。

    早先 ``school`` 的别名里混进了 ``collegename``，于是「学院 / 院系」被归到
    「毕业学校」上，树里同一件事长出两片 ``school`` 叶子——用户看到的「重复信息」。
    """
    assert knowledge.canonical_key("collegename") == "college_name"
    assert knowledge.canonical_key("college_name") == "college_name"
    assert knowledge.canonical_key("school") == "school"
    # 每个内置键都归到自己身上，谁也不许串到别人家
    from job.models.setting import ApplyProfileSettings

    strays = {
        key: knowledge.canonical_key(key)
        for key in ApplyProfileSettings.LABELS
        if knowledge.canonical_key(key) != key
    }
    assert strays == {}


def test_knowledge_adapt_keeps_unit_when_placeholder_shows_it():
    """占位符把单位写进示例值（如 175cm）时，要连单位一起填；number 栏则只填数字。"""
    assert knowledge.adapt("height", "175", label="身高", placeholder="如 175cm") == "175cm"
    assert knowledge.adapt("height", "1.75", label="身高", placeholder="例：1.75米") == "1.75米"
    # 明确只要数字的栏不带单位
    assert knowledge.adapt("height", "175", label="身高", placeholder="如 175cm", field_type="number") == "175"
    # 示例里的单位与这一栏要的单位对不上时，不硬塞
    assert knowledge.adapt("weight", "60", label="体重", placeholder="如 60cm") == "60"


# --- 匹配：历史 > 规则 > AI 兜底 ----------------------------------------------


def test_matcher_maps_text_fields_by_keyword(tmp_db):
    matcher = _matcher(_profile(name="张三", phone="13900139000"))
    fields = [
        FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0),
        FormField.from_raw({"tag": "input", "type": "text", "labelText": "手机号"}, index=1),
    ]
    asyncio.run(matcher.match(fields))
    assert fields[0].key == "name" and fields[0].filled == "张三"
    assert fields[0].source == "资料" and fields[0].confidence == 0.9 and fields[0].ok
    assert fields[1].key == "phone" and fields[1].filled == "13900139000"


def test_matcher_aligns_select_option(tmp_db):
    matcher = _matcher(_profile(degree="本科"))
    exact = FormField.from_raw(
        {"tag": "select", "type": "select", "labelText": "学历", "options": ["大专", "本科"]},
        index=0,
    )
    fuzzy = FormField.from_raw(
        {"tag": "select", "type": "select", "labelText": "学历", "options": ["本科及以上", "硕士"]},
        index=1,
    )
    asyncio.run(matcher.match([exact, fuzzy]))
    assert exact.filled == "本科" and exact.ok
    assert fuzzy.filled == "本科及以上" and fuzzy.ok


def test_matcher_flags_select_without_matching_option(tmp_db):
    matcher = _matcher(_profile(degree="本科"))
    field = FormField.from_raw(
        {"tag": "select", "type": "select", "labelText": "学历", "options": ["硕士", "博士"]},
        index=0,
    )
    asyncio.run(matcher.match([field]))
    assert field.key == "degree" and field.ok is False and "手动" in field.note


def test_matcher_aligns_degree_wording_by_level(tmp_db):
    """资料写「本科」、官网选项排「大学」：按学历层次挑同一档；挑不准就留空。"""
    assert align_option("本科", ["大学", "大学专科"]) == "大学"
    assert align_option("本科", ["大专", "硕士"]) == ""
    matcher = _matcher(_profile(degree="本科"))
    field = FormField.from_raw(
        {"tag": "select", "type": "select", "labelText": "学历", "options": ["大学", "大学专科"]},
        index=0,
    )
    asyncio.run(matcher.match([field]))
    assert field.filled == "大学" and field.ok


def test_matcher_ignores_placeholder_option(tmp_db):
    """「所属省份 / 所属城市」这类占位项不是真选项，不能被选中。"""
    matcher = _matcher(_profile(origin_place="浙江省杭州市西湖区"))
    field = FormField.from_raw(
        {
            "tag": "select",
            "type": "select",
            "labelText": "生源地",
            "options": ["所属省份", "北京市", "浙江省", "上海市"],
        },
        index=0,
    )
    asyncio.run(matcher.match([field]))
    assert field.filled == "浙江省"


def test_matcher_spreads_cascade_across_levels(tmp_db):
    """同一标签的三个下拉是省 / 市 / 区级联：对照表只留第一级一行，其余几级隐藏。"""
    matcher = _matcher(_profile(origin_place="浙江省杭州市西湖区"))
    fields = [
        FormField.from_raw(
            {
                "tag": "select",
                "type": "select",
                "labelText": "生源地",
                "options": ["所属省份", "广东省", "北京市"],
            },
            index=0,
        ),
        FormField.from_raw(
            {
                "tag": "select",
                "type": "select",
                "labelText": "生源地",
                "options": ["所属城市", "杭州市", "成都市"],
            },
            index=1,
        ),
        FormField.from_raw(
            {
                "tag": "select",
                "type": "select",
                "labelText": "生源地",
                "options": ["所属地区", "西湖区", "拱墅区"],
            },
            index=2,
        ),
    ]
    asyncio.run(matcher.match(fields))
    # 面板上只看到「生源地」一行：整串地址挂在第一级，其余两级只跟着填、不单独展示
    assert fields[0].filled == "浙江省杭州市西湖区"
    assert fields[0].key == "origin_place" and fields[0].source == "资料"
    assert fields[0].group == [1, 2] and fields[0].hidden is False
    assert [f.hidden for f in fields] == [False, True, True]
    assert all(f.key == "origin_place" for f in fields)


def test_matcher_keeps_cascade_levels_apart_when_only_one_part(tmp_db):
    """资料里只有「浙江省」这一级时，不合并：后面几级留给用户自己在浏览器里选。"""
    matcher = _matcher(_profile(origin_place="浙江省"))
    fields = [
        FormField.from_raw(
            {"tag": "select", "type": "select", "labelText": "生源地", "options": ["浙江省"]},
            index=0,
        ),
        FormField.from_raw(
            {"tag": "select", "type": "select", "labelText": "生源地", "options": ["所属城市"]},
            index=1,
        ),
    ]
    asyncio.run(matcher.match(fields))
    assert fields[0].group == [] and all(f.hidden is False for f in fields)


def test_matcher_hides_cascade_level_without_key(tmp_db):
    """级联的下一级扫描时只有占位项、认不出资料键，也要跟着第一级一起隐藏。"""
    matcher = _matcher(_profile(origin_place="浙江省杭州市西湖区"))
    fields = [
        FormField.from_raw(
            {
                "tag": "select",
                "type": "select",
                "labelText": "生源地",
                "options": ["所属省份", "广东省", "北京市"],
            },
            index=0,
        ),
        FormField.from_raw(
            {"tag": "select", "type": "select", "labelText": "生源地", "options": ["所属城市"]},
            index=1,
        ),
        FormField.from_raw(
            {"tag": "select", "type": "select", "labelText": "生源地", "options": ["所属地区"]},
            index=2,
        ),
    ]
    asyncio.run(matcher.match(fields))
    # 只留「生源地」一行：整串地址挂在第一级，后面两级只跟着填、不单独展示
    assert fields[0].filled == "浙江省杭州市西湖区"
    assert fields[0].group == [1, 2]
    assert [f.hidden for f in fields] == [False, True, True]
    assert all(f.key == "origin_place" for f in fields)


def test_matcher_keeps_same_label_fields_with_different_keys(tmp_db):
    """同一标签下认领到两个不同资料键时不是级联，不能合并隐藏。"""
    matcher = _matcher(_profile(city="杭州市", expected_city="上海市"))
    fields = [
        _field(index=0, tag="select", type="select", key="city", filled="杭州市", label="城市"),
        _field(
            index=1,
            tag="select",
            type="select",
            key="expected_city",
            filled="上海市",
            label="城市",
        ),
    ]
    matcher._spread_cascade(fields)
    assert [f.hidden for f in fields] == [False, False]
    assert fields[0].group == []


def test_matcher_reuses_history_mapping(tmp_db):
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0)
    ApplyMappingRow.remember(
        "example.com",
        [{"signature": field.signature(), "label": "姓名", "value": "李四", "source": "manual"}],
    )
    asyncio.run(_matcher(_profile(name="张三"), "example.com").match([field]))
    assert field.filled == "李四" and field.key == "manual"
    assert field.source == "沿用" and field.confidence == 0.95


def test_matcher_drops_history_when_stored_label_differs(tmp_db):
    """签名撞上了、但记下来的标签和现在这一栏对不上，就别沿用旧值。"""
    ApplyMappingRow.remember(
        "example.com",
        [{"signature": "email|text", "label": "邮箱", "value": "old@example.com", "source": "manual"}],
    )
    field = _field(name="email")
    asyncio.run(_matcher(_profile(email="new@example.com"), "example.com").match([field]))
    assert field.source != "沿用"
    assert field.filled == "new@example.com" and field.key == "email"


def test_matcher_reuses_history_when_label_is_blank_on_both_sides(tmp_db):
    """没有标签、只靠 name 认的字段，标签都是空串时照旧沿用。"""
    ApplyMappingRow.remember(
        "example.com",
        [{"signature": "email|text", "label": "", "value": "old@example.com", "source": "manual"}],
    )
    field = _field(name="email")
    asyncio.run(_matcher(_profile(email="new@example.com"), "example.com").match([field]))
    assert field.filled == "old@example.com" and field.source == "沿用"


def test_matcher_ignores_stale_source_word_history(tmp_db):
    """旧版本把「资料 / 手动」这类来源词写进了键位，这种记录不能拿来还原下拉。"""
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0)
    ApplyMappingRow.remember(
        "example.com",
        [{"signature": field.signature(), "label": "姓名", "value": "李四", "source": "资料"}],
    )
    asyncio.run(_matcher(_profile(name="张三"), "example.com").match([field]))
    assert field.key == "name" and field.filled == "张三" and field.source == "资料"


def test_matcher_remember_only_stores_filled_fields(tmp_db):
    matcher = _matcher(_profile(name="张三"), "b.com")
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0)
    asyncio.run(matcher.match([field]))
    assert matcher.remember([field]) == 1
    stored = ApplyMappingRow.lookup("b.com", [field.signature()])
    assert stored[field.signature()].value == "张三"
    # 记的是资料键，不是「资料」这个来源词——否则下次沿用时下拉会显示「资料」
    assert stored[field.signature()].source == "name"
    blank = FormField.from_raw({"tag": "input", "type": "text", "labelText": "备注"}, index=1)
    assert matcher.remember([blank]) == 0


def test_matcher_skips_ai_when_service_off(tmp_db):
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "随便填点"}, index=0)
    asyncio.run(_matcher(_profile(name="张三")).match([field]))
    assert field.key == "" and field.filled == "" and field.ok


def test_matcher_rule_key_falls_back_to_custom_label(tmp_db):
    """关键词表够不着的栏（本机学到的自定义栏目）按界面标签反查键：换个叫法也能对上同一条资料。"""
    profile = ApplyProfile.from_sources(
        ApplyProfileSettings(name="张三"), learned={"所属行业": "互联网"}
    )
    field = _field(label="所属行业 *")
    asyncio.run(_matcher(profile).match([field]))
    assert field.key == "所属行业" and field.filled == "互联网" and field.source == "资料"


def test_matcher_rule_key_vetoes_generic_name_in_emergency_slot(tmp_db):
    """本人姓名不该被填进「紧急联系人姓名」：专属栏借走通用词时宁可留空。"""
    field = _field(label="紧急联系人姓名")
    asyncio.run(_matcher(_profile(name="张三")).match([field]))
    assert field.key == "" and field.filled == ""


def test_matcher_shapes_height_by_form_hint(tmp_db):
    """同一份身高按表单提示变形：要米就写 1.75，仅数字就不带单位。"""
    profile = _profile(height="175cm")
    meters = _field(index=0, label="身高（米）")
    asyncio.run(_matcher(profile).match([meters]))
    assert meters.filled == "1.75"
    numeric = _field(index=1, label="身高", type="number")
    asyncio.run(_matcher(profile).match([numeric]))
    assert numeric.filled == "175"


def test_matcher_derive_ok_requires_basis():
    """模型自写值必须给依据：没依据、没键、没值、依据长得不像依据的，一律作废。

    键的范围不再受限（可推算的那几栏只是可信度给高一档）；是不是资料原文由
    ``inferred`` 标出来，面板上单列一区请用户核对。
    """
    ok = FieldMatcher._derive_ok
    assert ok("degree_name", "学士", "资料里学历是本科")
    assert ok("major_category", "计算机类", "专业 080904K 属于计算机类")
    assert ok("birth", "1998-01-01", "按毕业年份倒推")  # 别的栏也允许推
    assert not ok("degree_name", "学士", "")  # 没依据
    assert not ok("degree_name", "学士", "x" * 61)  # 依据长得不像依据
    assert not ok("degree_name", "", "资料里学历是本科")  # 没值
    assert not ok("", "学士", "资料里学历是本科")  # 没键


def test_matcher_claims_fresh_graduate_kind_by_label(tmp_db):
    """「毕业生类型」这类栏也归到应届身份，并直接选中推出来的那一档。"""
    profile = _profile(graduate_year="2026")
    field = _field(label="毕业生类型", tag="select", options=["国内应届毕业生", "留学归国人员"])
    asyncio.run(_matcher(profile).match([field]))
    assert field.key == "is_fresh_graduate" and field.filled == "国内应届毕业生"


def test_matcher_leaves_fresh_graduate_blank_without_evidence(tmp_db):
    """推不出应届身份时（没毕业年份、学校也是国内的）不硬认领这一栏，留给人工。"""
    field = _field(label="毕业生类型", tag="select", options=["国内应届毕业生", "留学归国人员"])
    asyncio.run(_matcher(_profile(school="云图大学")).match([field]))
    assert field.key == "" and field.filled == ""


def test_matcher_prompt_lists_blank_fields_without_secrets(tmp_db):
    """提示词要把「资料库里还空着、但认得的字段」列给模型，敏感键连名都不给。"""
    text = _matcher(_profile(name="张三"))._prompt([_field(index=0, label="民族")])
    assert "资料库里还空着的字段" in text
    assert "nation（民族）" in text
    assert "id_card" not in text


def test_matcher_plan_derives_category_for_unknown_major(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """本机专业表认不出的专业，模型带依据推算时落成「推算」来源。"""
    profile = _profile(major="水产养殖学")
    matcher = _ai_matcher(profile)
    field = _field(index=0, label="专业类别", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str]]:
        return {"0": ("major_category", "水产类", "专业「水产养殖学」所属的专业类")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field]))
    assert field.key == "major_category" and field.filled == "水产类"
    assert field.source == "推算" and field.note == "专业「水产养殖学」所属的专业类"


def test_matcher_plan_rejects_derivation_without_basis(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """可推算的栏也要说清依据：没依据的推算一律作废。"""
    profile = _profile(major="水产养殖学")
    matcher = _ai_matcher(profile)
    field = _field(index=0, label="专业类别", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str]]:
        return {"0": ("major_category", "水产类", "")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field]))
    assert field.key == "" and field.filled == ""


def test_matcher_plan_maps_alias_key_to_local_entry(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型用同义键（stature）时归到库里那条（height），照样填得上。"""
    profile = _profile(height="178")
    matcher = _ai_matcher(profile)
    field = _field(index=0, label="身高", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str]]:
        return {"0": ("stature", "178", "")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field]))
    assert field.key == "height" and field.filled == "178"


def test_matcher_plan_marks_derived_row_as_inferred(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型按已有资料推出来的那条标成 inferred，面板才能把它和已对上的项隔开。"""
    # 本机专业表认不出的专业：只有模型能按依据推，走的就是「推算」那条路
    profile = _profile(major="水产养殖学")
    matcher = _ai_matcher(profile)
    field = _field(index=0, label="专业类别", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str, str]]:
        return {"0": ("major_category", "水产类", "专业「水产养殖学」所属的专业类", "")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field]))
    assert field.key == "major_category" and field.filled == "水产类"
    assert field.source == "推算" and field.inferred is True
    assert field.note == "专业「水产养殖学」所属的专业类"


def test_matcher_plan_never_infers_sensitive_key(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """身份证、银行卡这类敏感栏不推理：模型编出来的号码不许落进页面。"""
    matcher = _ai_matcher(_profile())
    field = _field(index=0, label="身份证号", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str, str]]:
        return {"0": ("id_card", "110101199001011234", "按出生日期推算", "")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field]))
    assert field.inferred is False
    assert field.filled != "110101199001011234"


def test_matcher_plan_keeps_local_value_for_sensitive_key(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """敏感栏本机有原值时照常填——挡的是「推理」，不是「用本机原值」。"""
    matcher = _ai_matcher(_profile(id_card="110101199001011234"))
    field = _field(index=0, label="身份证号", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str, str]]:
        return {}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match([field]))
    assert field.key == "id_card" and field.filled == "110101199001011234"
    assert field.inferred is False


def test_matcher_plan_resolves_same_as_to_local_entry(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型新起一个名字、却在 same_as 里说清「就是库里那条」时，归到库里那条上。"""
    profile = _profile(degree="本科")
    matcher = _ai_matcher(profile)
    field = _field(index=0, label="学历层次", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str, str]]:
        return {"0": ("edu_level", "本科", "", "degree")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field]))
    # 模型编的 edu_level 归不到库里，改按 same_as 指的 degree 落值，且不是推算
    assert field.key == "degree" and field.filled == "本科"
    assert field.inferred is False


# --- 夹具回归：真实官网表单写法 → 扫描出的标签 → 匹配出的资料键 ------------------
#
# 这一层跑的是生产里的采集脚本（FormScanner.scan → 页面里的 _COLLECT_JS）和匹配器
# 本身，不是替身。官网表单改版、标签提取退化、关键词串味，都能在这里先炸出来。

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "apply"

# 编造的演示资料：覆盖原有 17 键，外加 HR 登记表那批扩展字段
_PROFILE_VALUES: dict[str, str] = {
    "name": "林晓",
    "gender": "男",
    "phone": "13900000001",
    "email": "lin.xiao@example.com",
    "wechat": "linxiao_dev",
    "birth": "2001-05",
    "city": "杭州",
    "address": "浙江省杭州市西湖区示例路 1 号",
    "school": "示例大学",
    "major": "计算机科学与技术",
    "degree": "本科",
    "graduate_year": "2024",
    "expected_salary": "15K",
    "available": "一个月内",
    "github": "https://github.com/example",
    "portfolio": "https://example.dev",
    "self_intro": "三年后端开发经验，熟悉 Python。",
    "nation": "汉族",
    "political": "共青团员",
    "id_type": "居民身份证",
    "id_card": "330100200105010000",
    "id_expiry": "2031-05-01",
    "native_place": "浙江杭州",
    "origin_place": "浙江杭州",
    "hukou": "浙江省杭州市西湖区",
    "marital": "未婚",
    "height": "178",
    "study_mode": "全日制",
    "gpa": "3.6",
    "rank": "5/120",
    "cet": "CET-6",
    "emergency_name": "林建国",
    "emergency_relation": "父子",
    "emergency_phone": "13800000002",
    "family_member": "父亲 林建国（退休）；母亲 周敏（教师）",
    "referral_code": "REF-2024-88",
    "source_channel": "校园招聘",
}


def _launch_options() -> dict[str, Any]:
    """无头启动本机 Chrome：和正式流程用同一个可执行文件，不额外下载浏览器。"""
    exe = BossSession.chrome_executable()
    if exe is not None:
        return {"headless": True, "executable_path": str(exe)}
    return {"headless": True, "channel": "chrome"}


class _FixtureBrowser:
    """常驻的无头浏览器：夹具回归得用真浏览器跑生产里的采集脚本。

    playwright 的对象绑在创建它的事件循环上，而测试主体是同步的，所以单独起一个
    线程跑循环，用 ``run_coroutine_threadsafe`` 把协程投过去。
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._playwright: Any = None
        self._browser: Any = None
        self.error = ""

    def start(self) -> None:
        """启动浏览器；起不来就把原因记在 ``error`` 上，由夹具决定跳过。"""
        try:
            self.run(self._boot())
        except Exception as exc:  # noqa: BLE001 没装 Chrome 的机器不该跑挂测试
            self.error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

    def run(self, coro: Any) -> Any:
        """把协程投到浏览器线程的循环上，等它出结果。"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=120)

    def collect(self, html: str) -> list[FormField]:
        """把一段 HTML 塞进空白页，扫一遍里面的控件。"""
        return self.run(self._collect(html))

    def fill(self, html: str, prepare: Any, read_js: str) -> tuple[Any, Any]:
        """在真浏览器里扫一遍夹具、按 ``prepare`` 改字段、真填一遍，再回读页面。

        ``prepare`` 在浏览器线程里同步执行，用来把匹配结果（key / filled / group
        这类）写到扫出来的字段上；``read_js`` 是填完之后在页面上求值的回读脚本，
        断言「值真的写进控件了」，而不是只看填表函数自己报的成功。
        """
        return self.run(self._fill(html, prepare, read_js))

    def outline(self, html: str) -> str:
        """把一段 HTML 塞进空白页，取它的可访问性大纲（发给模型当表单底稿的那份）。"""
        return self.run(self._outline(html))

    def control_count(self, html: str) -> int:
        """数一遍页面里能填的控件——``_settle`` 判断「表单渲染完了没有」用的那个数。"""
        return self.run(self._control_count(html))

    def stop(self) -> None:
        """收尾：关浏览器、停 playwright、停循环；任何一步失败都不影响测试结论。"""
        for closer in (self._close_browser, self._stop_playwright):
            try:
                self.run(closer())
            except Exception:  # noqa: BLE001 收尾失败不该改变测试结论
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _boot(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(**_launch_options())

    async def _collect(self, html: str) -> list[FormField]:
        page = await self._browser.new_page()
        try:
            # 只等 DOM：夹具里存着真官网的外链图片，等 "load" 会卡在那些请求上
            # （偶发 30s 超时，就是 form_phoenix.html 那条 flaky 的根子）。
            await page.set_content(html, wait_until="domcontentloaded")
            # 扫描器只用到 page.frames / frame.evaluate，不碰 session，传 None 就够
            return await FormScanner(None).scan(page)  # type: ignore[arg-type]
        finally:
            await page.close()

    async def _fill(self, html: str, prepare: Any, read_js: str) -> tuple[Any, Any]:
        page = await self._browser.new_page()
        try:
            await page.set_content(html, wait_until="domcontentloaded")
            fields = await FormScanner(None).scan(page)  # type: ignore[arg-type]
            prepare(fields)
            outcome = await FormFiller(None).fill(page, fields)  # type: ignore[arg-type]
            return outcome, await page.evaluate(read_js)
        finally:
            await page.close()

    async def _outline(self, html: str) -> str:
        page = await self._browser.new_page()
        try:
            await page.set_content(html, wait_until="domcontentloaded")
            return await FormScanner(None).outline(page)  # type: ignore[arg-type]
        finally:
            await page.close()

    async def _control_count(self, html: str) -> int:
        page = await self._browser.new_page()
        try:
            await page.set_content(html, wait_until="domcontentloaded")
            return await ApplyAssistant(None)._control_count(page)  # type: ignore[arg-type]
        finally:
            await page.close()

    async def _close_browser(self) -> None:
        if self._browser is not None:
            await self._browser.close()

    async def _stop_playwright(self) -> None:
        if self._playwright is not None:
            await self._playwright.stop()


@pytest.fixture(scope="module")
def fixture_browser() -> Iterator[_FixtureBrowser]:
    """模块级复用一份无头浏览器：所有夹具共用，省掉反复启动的开销。"""
    browser = _FixtureBrowser()
    browser.start()
    if browser.error:
        pytest.skip(f"本机起不了无头 Chrome，跳过夹具表单回归：{browser.error}")
    yield browser
    browser.stop()


# 每个夹具期望扫出的标签（按顺序），以及其中该被认领的「标签 → 资料键」。
# 没列进 keys 的栏位一律不该被认领：宁可留空，也别填错。
_FIXTURES: dict[str, dict[str, Any]] = {
    "form_labeled.html": {
        "labels": [
            "姓名",
            "男",
            "女",
            "手机号码",
            "电子邮箱",
            "最高学历",
            "毕业学校",
            "所学专业",
            "毕业年份",
        ],
        "keys": {
            "姓名": "name",
            "男": "gender",
            "手机号码": "phone",
            "电子邮箱": "email",
            "最高学历": "degree",
            "毕业学校": "school",
            "所学专业": "major",
            "毕业年份": "graduate_year",
        },
    },
    "form_container.html": {
        "labels": ["姓名", "手机号", "邮箱", "微信号", "现居城市", "期望薪资", "到岗时间"],
        "keys": {
            "姓名": "name",
            "手机号": "phone",
            "邮箱": "email",
            "微信号": "wechat",
            "现居城市": "city",
            "期望薪资": "expected_salary",
            "到岗时间": "available",
        },
    },
    "form_table.html": {
        "labels": ["姓名", "出生年月", "通讯地址", "最高学历", "附件简历"],
        "keys": {
            "姓名": "name",
            "出生年月": "birth",
            "通讯地址": "address",
            "最高学历": "degree",
            # 文件上传栏：标签必须认对（不能被「浏览…」按钮字顶掉），并按标签
            # 认领成资料键 resume——填表环节再按 key 去本机找简历原件。
            "附件简历": "resume",
        },
    },
    "form_flat.html": {
        "labels": ["GitHub", "个人主页", "到岗时间", "自我介绍", "兴趣爱好"],
        "keys": {
            "GitHub": "github",
            "个人主页": "portfolio",
            "到岗时间": "available",
            "自我介绍": "self_intro",
        },
    },
    "form_hr.html": {
        "labels": [
            "民族",
            "政治面貌",
            "证件类型",
            "身份证号码",
            "身份证有效期",
            "籍贯",
            "生源地",
            "婚姻状况",
            "身高",
            "学习形式",
            "GPA",
            "专业排名",
            "英语等级",
            "紧急联系人姓名",
            "与本人关系",
            "紧急联系人电话",
            "家庭成员",
            "内推码",
            "信息来源",
            "开户行",
            "银行卡号",
        ],
        "keys": {
            "民族": "nation",
            "政治面貌": "political",
            "证件类型": "id_type",
            "身份证号码": "id_card",
            "身份证有效期": "id_expiry",
            "籍贯": "native_place",
            "生源地": "origin_place",
            "婚姻状况": "marital",
            "身高": "height",
            "学习形式": "study_mode",
            "GPA": "gpa",
            "专业排名": "rank",
            "英语等级": "cet",
            "紧急联系人姓名": "emergency_name",
            "与本人关系": "emergency_relation",
            "紧急联系人电话": "emergency_phone",
            "家庭成员": "family_member",
            "内推码": "referral_code",
            "信息来源": "source_channel",
        },
    },
    # 组件库控件：假下拉 / 级联 / 日期 / 拖拽上传，扫出来要带上 widget 标记，
    # 填表环节才知道不能直接 fill()。
    "form_widgets.html": {
        "labels": ["现居城市", "户籍所在地", "出生年月", "附件简历"],
        "keys": {
            "现居城市": "city",
            "户籍所在地": "hukou",
            "出生年月": "birth",
            # 文件上传栏按标签认领成 resume（简历走专门的上传流程去本机取文件）
            "附件简历": "resume",
        },
        "widgets": {
            "现居城市": "select",
            "户籍所在地": "cascader",
            "出生年月": "date",
            "附件简历": "",
        },
    },
    # 上传组件两件套（只读文本框 + 真 file 控件）：只读的「影子」框要丢掉，
    # 每栏只剩一行，且按标签认领成 resume / photo。
    "form_shadow.html": {
        "labels": ["姓名", "简历上传", "生活照上传"],
        "keys": {
            "姓名": "name",
            "简历上传": "resume",
            "生活照上传": "photo",
        },
    },
    # 更刁钻的上传栏：文件名框可编辑（不是 readonly）、中间还夹一个隐藏域，
    # 影子框和 file 控件并不相邻。同一栏仍然只该剩一行。
    "form_upload_pair.html": {
        "labels": ["姓名", "简历上传", "生活照上传"],
        "keys": {
            "姓名": "name",
            "简历上传": "resume",
            "生活照上传": "photo",
        },
    },
    # webuploader 式上传栏：真 file 控件被 CSS 藏起来（display:none），界面上
    # 只露一个「选择文件」按钮和一个「未选择文件」提示。隐藏的 file 控件仍要
    # 扫得到并按行标签认领，否则填表时找不到该往哪儿喂文件。
    "form_hidden_upload.html": {
        "labels": ["姓名", "简历上传", "生活照上传"],
        "keys": {
            "姓名": "name",
            "简历上传": "resume",
            "生活照上传": "photo",
        },
    },
    # 北森 phoenix 招聘官网（真实页面存成的脱敏快照）：标题在控件外面、中间还隔着
    # 三四层 unmodeled-layer 空壳，而且假下拉全是同一个 placeholder「请选择」。
    # 这一栏最容易整片认错，所以逐条钉住标签顺序与认领结果。
    # 「结束时间」旁边那个原生「至今」勾选框在真页面里被组件库的 CSS 藏掉了
    # （显示的是 phoenix-checkbox__realInput 那个壳），扫不到也不该扫到，夹具
    # 同样把它标成 display:none，保证夹具和线上扫出的是同一批控件。
    "form_phoenix.html": {
        "labels": [
            "姓名",
            "手机号码",
            "邮箱",
            "出生日期",
            "证件号码",
            "工作年限",
            "证件照",
            "期望从事行业",
            "期望从事职业",
            "现月薪(税前)",
            "期望月薪(税前)",
            "期望工作城市",
            "到岗时间",
            "学校名称",
            "专业名称",
            "学历",
            "开始时间",
            "结束时间",
            "公司名称",
            "职位名称",
            "开始时间",
            "结束时间",
            "工作职责",
            "项目名称",
            "职务",
            "开始时间",
            "结束时间",
            "项目描述",
            "性别",
        ],
        "keys": {
            "姓名": "name",
            "手机号码": "phone",
            "邮箱": "email",
            "出生日期": "birth",
            "证件号码": "id_card",
            # 证件照走专门的上传流程去本机取寸照
            "证件照": "photo",
            "期望月薪(税前)": "expected_salary",
            "期望工作城市": "city",
            "到岗时间": "available",
            "学校名称": "school",
            "专业名称": "major",
            "学历": "degree",
            "性别": "gender",
        },
        "widgets": {
            "姓名": "",
            "手机号码": "",
            "邮箱": "",
            "出生日期": "select",
            "证件号码": "",
            "工作年限": "select",
            "证件照": "",
            "期望从事行业": "select",
            "期望从事职业": "select",
            "现月薪(税前)": "select",
            "期望月薪(税前)": "select",
            "期望工作城市": "select",
            "到岗时间": "select",
            "学校名称": "",
            "专业名称": "",
            "学历": "select",
            "开始时间": "select",
            "结束时间": "select",
            "公司名称": "",
            "职位名称": "",
            "工作职责": "",
            "项目名称": "",
            "职务": "",
            "项目描述": "",
            "性别": "",
        },
    },
    # 纯角色控件：整栏一个 <input> 都没有，只有一个带 role 的 div。组件库这么写
    # 很常见（antd Select、element-ui el-select、各种自研组件），按控件标签扫会
    # 整栏漏掉。这份夹具钉住那条兜底：栏位要扫得到，控件类型（select / cascader /
    # date / switch）也要认对，否则填表环节会按错的套路去填。
    "form_roles.html": {
        "labels": ["期望工作城市", "户籍所在地", "出生年月", "政治面貌", "是否服从调剂"],
        "keys": {
            "期望工作城市": "city",
            "户籍所在地": "hukou",
            "出生年月": "birth",
            "政治面貌": "political",
        },
        "widgets": {
            "期望工作城市": "select",
            "户籍所在地": "cascader",
            "出生年月": "date",
            "政治面貌": "select",
            "是否服从调剂": "",
        },
    },
}


@pytest.mark.parametrize("name", list(_FIXTURES))
def test_fixture_form_labels_and_keys(fixture_browser: _FixtureBrowser, tmp_db, name: str):
    """官网表单存成静态 HTML，断言「扫描出的标签 + 匹配出的资料键」。"""
    case = _FIXTURES[name]
    html = (_FIXTURE_DIR / name).read_text(encoding="utf-8")
    fields = fixture_browser.collect(html)
    asyncio.run(_matcher(_profile(**_PROFILE_VALUES)).match(fields))

    assert [f.label for f in fields] == case["labels"]
    matched = {f.label: f.key for f in fields}
    for label, key in case["keys"].items():
        assert matched[label] == key, f"{name} 的「{label}」应匹配 {key}，实际是 {matched[label]!r}"
    for label in case["labels"]:
        if label not in case["keys"]:
            assert matched[label] == "", f"{name} 的「{label}」不该被认领"
    widgets = case.get("widgets") or {}
    if widgets:
        got = {f.label: f.widget for f in fields}
        assert got == widgets, f"{name} 的控件类型应是 {widgets}，实际是 {got}"


def test_scanner_outline_carries_roles_and_refs(fixture_browser: _FixtureBrowser):
    """可访问性大纲要拿得到：分段标题、组件库真实角色、[ref=eN] 都在里面。

    这份大纲是发给模型当「这张表单长什么样」的底稿。纯文字模型看不到布局，
    「教育经历 / 工作经历」里各有一个「开始时间」时，光看标签分不出哪栏属于哪段；
    大纲的缩进层级补的就是这层信息。取不到大纲时 AI 计划会退化成只看标签，
    所以这里钉住「真页面能取到、且带角色与 ref」。
    """
    html = (_FIXTURE_DIR / "form_roles.html").read_text(encoding="utf-8")
    text = fixture_browser.outline(html)
    assert text, "真浏览器上取不到可访问性大纲"
    assert "heading" in text and "应聘登记表" in text
    # 纯角色控件在大纲里是 combobox / switch，而不是被当成一坨 generic div
    assert "combobox" in text and "switch" in text
    # [ref=eN] 是后续按可访问性定位控件的抓手
    assert "[ref=e" in text


def test_scanner_outline_returns_blank_on_broken_page():
    """拿不到大纲时返回空串，不能把扫描带崩：没装浏览器 / 页面炸了都要能继续。"""

    class _DeadPage:
        frames: list[Any] = []

    assert asyncio.run(FormScanner(None).outline(_DeadPage())) == ""  # type: ignore[arg-type]


def test_settle_counts_pure_role_controls(fixture_browser: _FixtureBrowser):
    """纯角色表单也要被算成「有控件」。

    一个 ``<input>`` 都不渲染的组件库表单，以前控件数永远是 0，被 ``_settle``
    当成「空壳页」一路等到 ``SETTLE_MAX_MS``（6 秒）才开工——用户看到的就是
    「转好几秒才出结果」。这条钉住「按角色也算得上」，别再退化回去。
    """
    roles = (_FIXTURE_DIR / "form_roles.html").read_text(encoding="utf-8")
    assert fixture_browser.control_count(roles) >= ApplyAssistant.SETTLE_MIN_CONTROLS
    # 普通表单不受影响
    plain = (_FIXTURE_DIR / "form_labeled.html").read_text(encoding="utf-8")
    assert fixture_browser.control_count(plain) > 0
    # 没展开的浮层不算数：只有一堆藏起来的选项，说明真控件还没渲染出来
    hidden = '<div role="listbox" style="display:none"><div role="option">杭州</div></div>'
    assert fixture_browser.control_count(hidden) == 0


# 填完之后回读页面：每个 file 控件真挂上了哪份文件、每个 select 真选中了哪个值。
_FILES_JS = """
() => {
  const out = {};
  for (const input of document.querySelectorAll('input[type=file]')) {
    out[input.name] = input.files && input.files.length ? input.files[0].name : '';
  }
  return out;
}
"""

_SELECTED_JS = "() => Array.from(document.querySelectorAll('select')).map((s) => s.value)"


def test_real_browser_upload_lands_file_in_the_right_control(
    fixture_browser: _FixtureBrowser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """P0 最后一公里：文件真喂进了对的控件——简历进简历栏、寸照进生活照栏。

    这条走真浏览器 + 真 ``set_input_files``，断言 ``input.files`` 里真挂着那两份
    文件，而不是只看填表函数自己报的「成功」。
    """
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 fake resume")
    photo = tmp_path / "me.jpg"
    photo.write_bytes(b"\xff\xd8\xff fake photo")
    monkeypatch.setattr(
        FormFiller, "_pick_file", staticmethod(lambda key: photo if key == "photo" else cv)
    )
    html = (_FIXTURE_DIR / "form_upload_pair.html").read_text(encoding="utf-8")

    def prepare(fields: list[FormField]) -> None:
        for item in fields:
            if item.is_file:
                item.key = "resume" if "简历" in item.label else "photo"
                item.filled = "简历原件" if item.key == "resume" else "寸照"

    outcome, landed = fixture_browser.fill(html, prepare, _FILES_JS)
    assert outcome.filled == 2
    assert landed == {"Resume_File": "cv.pdf", "Photo_File": "me.jpg"}


def test_real_browser_upload_lands_file_in_hidden_control(
    fixture_browser: _FixtureBrowser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """P0 最后一公里：file 控件被 display:none 藏起来时，文件照样喂得进去。

    webuploader 这类组件把真控件藏起来、只露一个按钮，填表逻辑必须能穿透外壳
    找到那个控件，而不是看到「不可见」就放弃。
    """
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 fake resume")
    photo = tmp_path / "me.jpg"
    photo.write_bytes(b"\xff\xd8\xff fake photo")
    monkeypatch.setattr(
        FormFiller, "_pick_file", staticmethod(lambda key: photo if key == "photo" else cv)
    )
    html = (_FIXTURE_DIR / "form_hidden_upload.html").read_text(encoding="utf-8")

    def prepare(fields: list[FormField]) -> None:
        for item in fields:
            if item.is_file:
                item.key = "resume" if "简历" in item.label else "photo"
                item.filled = "简历原件" if item.key == "resume" else "寸照"

    outcome, landed = fixture_browser.fill(html, prepare, _FILES_JS)
    assert outcome.filled == 2
    assert landed == {"Resume_File": "cv.pdf", "Photo_File": "me.jpg"}


def test_real_browser_cascade_selects_every_level(fixture_browser: _FixtureBrowser):
    """P0 最后一公里：省 / 市 / 区三级联动在真浏览器里逐级选到底，而不是只填到省。"""
    html = (_FIXTURE_DIR / "form_cascade.html").read_text(encoding="utf-8")

    def prepare(fields: list[FormField]) -> None:
        selects = [f for f in fields if f.tag == "select"]
        assert len(selects) == 3
        head = selects[0]
        head.key = "origin_place"
        head.filled = "广东省广州市天河区"
        head.group = [item.index for item in selects[1:]]
        for item in selects[1:]:
            item.hidden = True
            item.key = "origin_place"
            item.filled = ""

    outcome, selected = fixture_browser.fill(html, prepare, _SELECTED_JS)
    assert outcome.filled == 1
    assert selected == ["440000", "440100", "440106"]


def test_mapping_row_lookup_and_forget(tmp_db):
    items = [{"signature": "姓名|text", "label": "姓名", "value": "张三", "source": "资料"}]
    assert ApplyMappingRow.remember("a.com", items) == 1
    assert ApplyMappingRow.remember("", items) == 0  # 没域名不记
    assert ApplyMappingRow.remember("a.com", []) == 0
    assert ApplyMappingRow.lookup("a.com", ["姓名|text"])["姓名|text"].value == "张三"
    assert ApplyMappingRow.lookup("a.com", []) == {}
    assert ApplyMappingRow.lookup("", ["姓名|text"]) == {}
    assert ApplyMappingRow.forget_domain("a.com") == 1
    assert ApplyMappingRow.lookup("a.com", ["姓名|text"]) == {}
    assert ApplyMappingRow.forget_domain("") == 0


def test_learned_row_learn_values_forget_clear(tmp_db):
    """本机学会的字段：空键空值不记，同键覆盖，能逐条忘掉也能整表清空。"""
    assert ApplyLearnedRow.learn("a.com", [{"key": "hobby", "label": "爱好", "value": "游泳"}]) == 1
    assert ApplyLearnedRow.learn("a.com", []) == 0
    assert ApplyLearnedRow.learn("a.com", [{"key": "", "value": "x"}]) == 0  # 空键不记
    assert ApplyLearnedRow.learn("a.com", [{"key": "hobby", "value": ""}]) == 0  # 空值不记
    # 同一个键再学到（用户改了内容）就覆盖，只留最新的一条
    ApplyLearnedRow.learn("b.com", [{"key": "hobby", "label": "兴趣爱好", "value": "跑步"}])
    assert ApplyLearnedRow.values() == {"hobby": "跑步"}
    row = ApplyLearnedRow.rows()[0]
    assert row["key"] == "hobby" and row["label"] == "兴趣爱好" and row["domain"] == "b.com"
    assert ApplyLearnedRow.forget("hobby") is True
    assert ApplyLearnedRow.forget("hobby") is False and ApplyLearnedRow.forget("") is False
    assert ApplyLearnedRow.values() == {}
    ApplyLearnedRow.learn("a.com", [{"key": "k1", "value": "v1"}, {"key": "k2", "value": "v2"}])
    assert ApplyLearnedRow.clear() == 2 and ApplyLearnedRow.values() == {}


# --- 任务：岗位 ↔ 网申地址 ----------------------------------------------------


def test_domain_of_strips_www_and_handles_garbage():
    assert domain_of("https://www.example.com/apply?x=1") == "example.com"
    assert domain_of("http://jobs.acme.cn/form") == "jobs.acme.cn"
    assert domain_of("") == "" and domain_of("not a url") == ""


def test_application_create_snapshots_job(tmp_db):
    uid = _seed_job("j1")
    row = ApplicationRow.create(uid, "https://www.acme.cn/apply")
    assert row.title == "AI 工程师" and row.company == "某公司"
    assert row.salary == "20-30K" and row.location == "上海"
    assert row.url == "https://www.acme.cn/apply" and row.domain == "acme.cn"
    assert row.status == "pending"
    data = ApplicationRow.get_dict(uid)
    assert data["jobUid"] == uid and data["statusLabel"] == "待网申" and data["fields"] == []


def test_application_create_keeps_old_url(tmp_db):
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://a.com/form")
    again = ApplicationRow.create(uid)  # 没传新地址就沿用上次的
    assert again.url == "https://a.com/form" and again.domain == "a.com"
    assert ApplicationRow.count() == 1


def test_application_set_url_updates_domain(tmp_db):
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://a.com/form")
    saved = ApplicationRow.set_url(uid, "https://b.com/apply")
    assert saved["url"] == "https://b.com/apply" and saved["domain"] == "b.com"
    assert ApplicationRow.set_url("nope", "https://b.com") is None


def test_application_create_standalone_needs_no_job(tmp_db):
    row = ApplicationRow.create_standalone("https://www.yuntu.cn/apply/1")
    assert row.uid.startswith("apply:") and row.job_uid == ""
    assert row.platform == "web" and row.domain == "yuntu.cn"
    assert row.status == "pending"


def test_application_set_meta_keeps_user_edits(tmp_db):
    row = ApplicationRow.create_standalone("https://yuntu.cn/apply/1")
    saved = ApplicationRow.set_meta(row.uid, title="后端实习生", company="云图科技")
    assert saved["title"] == "后端实习生" and saved["company"] == "云图科技"
    # 第二次检测到别的名字不能盖掉已有值，空白值也不能把已有值抹掉
    again = ApplicationRow.set_meta(row.uid, title="另一个岗位", company="")
    assert again["title"] == "后端实习生" and again["company"] == "云图科技"
    assert ApplicationRow.set_meta("nope", title="x") is None


def test_application_save_fields_and_status(tmp_db):
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://a.com/form")
    ApplicationRow.save_fields(uid, [{"index": 0, "label": "姓名", "key": "name"}])
    assert ApplicationRow.get_dict(uid)["fields"][0]["label"] == "姓名"
    ApplicationRow.set_status(uid, "submitted", note="已提交")
    data = ApplicationRow.get_dict(uid)
    assert data["status"] == "submitted" and data["statusLabel"] == "已提交"
    assert data["submittedAt"] and data["note"] == "已提交"
    # 不传 note 时保留旧备注；传空串才清掉（重扫成功要能抹去旧的拦路提示）
    ApplicationRow.save_fields(uid, [{"index": 0, "label": "姓名", "key": "name"}])
    assert ApplicationRow.get_dict(uid)["note"] == "已提交"
    ApplicationRow.save_fields(uid, [{"index": 0, "label": "姓名", "key": "name"}], note="")
    assert ApplicationRow.get_dict(uid)["note"] == ""
    assert ApplicationRow.save_fields("nope", []) is None
    assert ApplicationRow.set_status("nope", "filled") is None


def test_application_list_count_and_delete(tmp_db):
    first, second = _seed_job("j1"), _seed_job("j2")
    ApplicationRow.create(first, "https://a.com/form")
    ApplicationRow.create(second, "https://a.com/form")
    ApplicationRow.set_status(second, "filled")
    assert ApplicationRow.count() == 2 and ApplicationRow.count(status="filled") == 1
    assert [r["uid"] for r in ApplicationRow.list_dicts(status="filled")] == [second]
    assert ApplicationRow.delete_by_uid(first) is True
    assert ApplicationRow.delete_by_uid(first) is False
    assert ApplicationRow.count() == 1


def test_application_tolerates_corrupt_fields_json(tmp_db):
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://a.com/form")
    with db_session() as session:
        row = session.get(ApplicationRow, uid)
        row.fields_json = '{"oops": 1}'  # 不是数组
        session.add(row)
        session.commit()
    assert ApplicationRow.get_dict(uid)["fields"] == []
    with db_session() as session:
        row = session.get(ApplicationRow, uid)
        row.fields_json = "{不是 JSON"
        session.add(row)
        session.commit()
    assert ApplicationRow.get_dict(uid)["fields"] == []


def test_application_handoff_roundtrip_and_corrupt_json(tmp_db):
    """交付清单单独落一列：存进去能原样读回，不传就不动它，坏了回落成三堆空。"""
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://a.com/form")
    blank = {"gaps": [], "unsure": [], "manual": []}
    assert ApplicationRow.get_dict(uid)["handoff"] == blank

    handoff = {
        "gaps": [{"index": 1, "label": "民族", "key": "ethnicity", "type": "text", "required": True}],
        "unsure": [{"index": 2, "label": "身高", "key": "height", "type": "number", "required": False}],
        "manual": [{"index": 3, "label": "奇怪的一栏", "key": "", "type": "text", "required": True}],
    }
    ApplicationRow.save_fields(uid, [], handoff=handoff)
    assert ApplicationRow.get_dict(uid)["handoff"] == handoff

    # 只更新对照表（不传 handoff）不该把已有交付清单抹掉
    ApplicationRow.save_fields(uid, [{"index": 0, "label": "姓名", "key": "name"}])
    assert ApplicationRow.get_dict(uid)["handoff"] == handoff

    # 显式传一份空清单表示「换表了，旧的作废」，三堆都要清空
    ApplicationRow.save_fields(uid, [], handoff=blank)
    assert ApplicationRow.get_dict(uid)["handoff"] == blank

    # 坏掉的 JSON / 不是对象，一律回落成三堆空，前端不用判空
    with db_session() as session:
        row = session.get(ApplicationRow, uid)
        row.handoff_json = "{不是 JSON"
        session.add(row)
        session.commit()
    assert ApplicationRow.get_dict(uid)["handoff"] == blank
    with db_session() as session:
        row = session.get(ApplicationRow, uid)
        row.handoff_json = '{"gaps": "不是数组", "unsure": [1, {"index": 2, "key": "x"}]}'
        session.add(row)
        session.commit()
    assert ApplicationRow.get_dict(uid)["handoff"] == {
        "gaps": [],
        "unsure": [{"index": 2, "key": "x"}],
        "manual": [],
    }


# --- 填表：按控件类型分派 -----------------------------------------------------


def test_filler_dispatches_by_control_type(tmp_db):
    page = _FakePage(selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]', '[data-gj-idx="2"]'})
    fields = [
        _field(index=0, key="name", filled="张三"),
        _field(index=1, tag="select", type="select", key="degree", filled="本科"),
        _field(index=2, type="checkbox", key="agree", filled="1"),
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.ok and outcome.filled == 3 and outcome.skipped == 0 and outcome.failed == 0
    assert ("fill", '[data-gj-idx="0"]', "张三") in page.calls
    assert ("select", '[data-gj-idx="1"]', "本科") in page.calls
    assert ("check", '[data-gj-idx="2"]', "") in page.calls
    assert outcome.fields[0]["key"] == "name"


def test_filler_strips_unit_for_number_input(tmp_db):
    """数字栏只收数字：资料里的「175cm」要削成「175」，原样塞进去官网会报错。"""
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, key="height", type="number", filled="175cm")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("fill", '[data-gj-idx="0"]', "175") in page.calls


def test_filler_number_reports_when_value_is_not_numeric(tmp_db):
    """「8K-12K」这种带范围的写法不猜，老实报「请手动填写」。"""
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, key="expected_salary", type="number", filled="8K-12K")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1 and "不是数字" in fields[0].note
    assert not any(call[0] == "fill" for call in page.calls)


def test_filler_counts_skipped_and_failed(tmp_db):
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [
        _field(index=0),  # 没匹配到 → 跳过
        _field(index=1, key="degree", filled=""),  # 匹配到却没值 → 失败
        _field(index=2, key="name", filled="张三"),  # 页面上找不到控件 → 失败
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 0 and outcome.skipped == 1 and outcome.failed == 2
    assert not outcome.ok
    assert fields[1].ok is False and "手动" in fields[1].note
    assert "找不到" in fields[2].note


def test_filler_writes_hand_typed_value_without_key(tmp_db):
    """面板上能手动填的栏（比如验证码），点了「填入浏览器」就得真的写进去。"""
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, label="验证码：", filled="6PDmP")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1 and outcome.skipped == 0
    assert ("fill", '[data-gj-idx="0"]', "6PDmP") in page.calls


def test_filler_falls_back_to_css_path(tmp_db):
    page = _FakePage(selectors={"#submit-name"})
    fields = [_field(index=0, key="name", filled="张三", fallback="#submit-name")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("fill", "#submit-name", "张三") in page.calls


def test_filler_falls_back_to_main_frame(tmp_db):
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, frame=5, key="name", filled="张三")]
    assert asyncio.run(FormFiller(None).fill(page, fields)).filled == 1


def test_filler_needs_resume_file_for_upload(tmp_db):
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1 and "简历文件" in fields[0].note


def test_filler_uploads_resume_file(tmp_db, tmp_path: Path):
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("file", '[data-gj-idx="0"]', str(cv)) in page.calls


def test_filler_needs_photo_file_for_upload(tmp_db):
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, type="file", key="photo", filled="寸照")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1 and "寸照" in fields[0].note


def test_filler_uploads_photo_file(tmp_db, tmp_path: Path):
    photo = tmp_path / "me.jpg"
    photo.write_bytes(b"jpeg-bytes")
    SearchConfigRow.save(photo_path=str(photo))
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, type="file", key="photo", filled="寸照")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("file", '[data-gj-idx="0"]', str(photo)) in page.calls


def test_filler_skips_select_already_at_target(tmp_db):
    """「只填变化处」：页面里已经是目标值时不再选一遍（多轮补漏靠它收敛）。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        native={'[data-gj-idx="0"]': [["1", "本科（学士）"], ["2", "硕士"]]},
    )
    first = asyncio.run(
        FormFiller(None).fill(page, [_field(index=0, tag="select", type="select", key="degree", filled="本科")])
    )
    assert first.filled == 1 and ("select", '[data-gj-idx="0"]', "1") in page.calls
    page.calls.clear()
    again = asyncio.run(
        FormFiller(None).fill(page, [_field(index=0, tag="select", type="select", key="degree", filled="本科")])
    )
    assert again.filled == 0 and again.skipped == 0 and again.failed == 0
    assert not any(call[0] == "select" for call in page.calls)


def test_filler_does_not_reupload_file_already_on_page(tmp_db, tmp_path: Path):
    """上传栏已经挂着文件就不重复上传：再传一遍会覆盖官网自己解析出的内容。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    asyncio.run(FormFiller(None).fill(page, [_field(index=0, type="file", key="resume", filled="简历")]))
    page.calls.clear()
    again = asyncio.run(FormFiller(None).fill(page, [_field(index=0, type="file", key="resume", filled="简历")]))
    assert again.filled == 0 and again.skipped == 0
    assert not any(call[0] == "file" for call in page.calls)


def test_filler_flags_fields_the_site_complains_about(tmp_db):
    """填进去不等于填对了：官网在那一栏下面弹的红字要读回来挂到对应的栏上。"""
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "手机号", "path": "#p"}],
        selectors={'[data-gj-idx="0"]'},
        errors=[{"idx": "0", "text": "请填写正确的手机号"}],
    )
    fields = [_field(index=0, key="phone", filled="13900139000")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.flagged == 1
    assert fields[0].ok is False and fields[0].note == "官网提示：请填写正确的手机号"


def test_filler_picks_closest_native_option(tmp_db):
    """原生下拉的选项文字常和资料对不上，要挑最贴近的那一项，按 value 选中。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        native={'[data-gj-idx="0"]': [["1", "大专"], ["2", "本科（学士）"], ["3", "硕士"]]},
    )
    fields = [_field(index=0, tag="select", type="select", key="degree", filled="本科")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("select", '[data-gj-idx="0"]', "2") in page.calls


def test_filler_select_reports_when_option_missing(tmp_db):
    """下拉里根本没有这一项时，老实报「请手动选择」，绝不硬塞。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        native={'[data-gj-idx="0"]': [["1", "男"], ["2", "女"]]},
    )
    fields = [_field(index=0, tag="select", type="select", key="gender", filled="保密")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1 and "请手动选择" in fields[0].note


def test_filler_overlay_select_picks_and_verifies(tmp_db):
    """假下拉：点开浮层、按文字挑一项，再回读 input 校验确实选上了。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        choices={".ant-select-item-option": ["北京市", "杭州市", "广州市"]},
    )
    fields = [_field(index=0, key="city", filled="杭州", widget="select")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("click", '[data-gj-idx="0"]', "") in page.calls
    assert ("click", ".ant-select-item-option", "杭州市") in page.calls


def test_filler_overlay_select_reports_when_missing(tmp_db):
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        choices={".ant-select-item-option": ["北京市", "广州市"]},
    )
    fields = [_field(index=0, key="city", filled="上海市", widget="select")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1 and "请手动选择" in fields[0].note


def test_filler_overlay_select_opens_widget_when_click_times_out(tmp_db):
    """控件点不开（普通 click 过不了可操作性检查、直接超时）时退回 DOM 点击把它点开。

    日志里整批栏位栽的就是这个：``Locator.click: Timeout 3000ms exceeded``——
    值认出来了、控件也定位到了，就是点不开浮层，于是「识别出来了却填不进去」。
    """
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        choices={".ant-select-item-option": ["北京市", "杭州市"]},
        refuse_click={'[data-gj-idx="0"]'},
    )
    fields = [_field(index=0, key="city", filled="杭州", widget="select")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("click", ".ant-select-item-option", "杭州市") in page.calls


def test_filler_types_into_readonly_wrapped_input(tmp_db):
    """直接 fill 写不进去（组件库把真正的 input 压在浮层下面）时退回「聚焦 + 逐字敲」。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        refuse_fill={'[data-gj-idx="0"]'},
    )
    fields = [_field(index=0, key="phone", filled="13800000000")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("focus", '[data-gj-idx="0"]', "") in page.calls
    assert ("type", '[data-gj-idx="0"]', "13800000000") in page.calls


def test_filler_overlay_select_matches_synonym(tmp_db):
    """资料写「随时到岗」，官网只有「一周内」这类同义写法，要按意思挑中并回读通过。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        choices={".ant-select-item-option": ["一周内", "一个月内", "三个月内"]},
    )
    fields = [_field(index=0, key="available", filled="随时到岗", widget="select")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("click", ".ant-select-item-option", "一周内") in page.calls


def test_filler_overlay_select_matches_salary_range(tmp_db):
    """资料「8-10K」对官网「8001～10000」：按金额区间挑中，且回读拿官网文字比对。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        choices={".ant-select-item-option": ["1000以下", "8001～10000", "10001～15000"]},
    )
    fields = [_field(index=0, key="expected_salary", filled="8-10K", widget="select")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("click", ".ant-select-item-option", "8001～10000") in page.calls


def test_filler_overlay_select_scrolls_to_reach_option(tmp_db):
    """选项在浮层下面、要滚下去才渲染得出来时，先滚一屏再找，不能直接判「没有这一项」。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        choices={".ant-select-item-option": ["北京市", "广州市"]},
        late_choices={".ant-select-item-option": ["杭州市"]},
    )
    fields = [_field(index=0, key="city", filled="杭州市", widget="select")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert page.frame.scrolls >= 1
    assert ("click", ".ant-select-item-option", "杭州市") in page.calls


def test_filler_treats_probed_dropdown_as_overlay(tmp_db):
    """扫描时没认出来的自研下拉（只读输入框 + 浮层）：不能 fill 假值，要走「点开挑选项」。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        choices={".ant-select-item-option": ["本科（学士）", "硕士"]},
        probe={'[data-gj-idx="0"]': {"hint": True, "combo": False}},
    )
    fields = [_field(index=0, key="degree", filled="本科")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("fill", '[data-gj-idx="0"]', "本科") not in page.calls
    assert ("click", ".ant-select-item-option", "本科（学士）") in page.calls


def test_filler_overlay_select_verifies_via_shown_tag(tmp_db):
    """组件库把选中值挂在控件外层（input 是空的），回读要走展示节点那段脚本。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        choices={".ant-select-item-option": ["上海市", "北京市"]},
        shown={'[data-gj-idx="0"]': "期望工作城市\n上海市"},
    )
    fields = [_field(index=0, key="city", filled="上海", widget="select")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("click", ".ant-select-item-option", "上海市") in page.calls


def test_filler_area_cascade_checks_box_and_confirms(tmp_db):
    """地区级联：点格子里那个小勾选框落值，最后按文字挑「确定」，绝不点「取消」。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]', ".icon-container"},
        choices={
            ".area-selector-container": [""],
            ".area-item-container": ["上海市"],
            ".phoenix-button__content": ["取消", "确定"],
        },
        readback={'[data-gj-idx="0"]': "期望工作城市\n上海市"},
        tall={".area-selector-container"},
    )
    fields = [_field(index=0, key="city", filled="上海", widget="select")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("click", ".icon-container", "") in page.calls
    assert ("click", ".phoenix-button__content", "确定") in page.calls
    assert ("click", ".phoenix-button__content", "取消") not in page.calls


def test_filler_cascade_selects_each_level(tmp_db):
    """级联：按「/」拆成三级，一级一级点。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        choices={".ant-cascader-menu-item": ["浙江省", "杭州市", "西湖区"]},
    )
    fields = [_field(index=0, key="hukou", filled="浙江省/杭州市/西湖区", widget="cascader")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("click", ".ant-cascader-menu-item", "浙江省") in page.calls
    assert ("click", ".ant-cascader-menu-item", "杭州市") in page.calls
    assert ("click", ".ant-cascader-menu-item", "西湖区") in page.calls


def test_filler_cascade_native_selects_each_level(tmp_db):
    """原生三级联动下拉：同一串地址拆成省/市/区，逐级按 value 选中，下一级不再单独成行。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]', '[data-gj-idx="2"]'},
        native={
            '[data-gj-idx="0"]': [["", "所属省份"], ["1", "广东省"], ["2", "浙江省"]],
            '[data-gj-idx="1"]': [["", "所属城市"], ["1", "杭州市"], ["2", "成都市"]],
            '[data-gj-idx="2"]': [["", "所属地区"], ["1", "西湖区"], ["2", "上城区"]],
        },
    )
    fields = [
        _field(
            index=0,
            tag="select",
            type="select",
            key="origin_place",
            filled="浙江省杭州市西湖区",
            group=[1, 2],
        ),
        _field(index=1, tag="select", type="select", key="origin_place", filled="", hidden=True),
        _field(index=2, tag="select", type="select", key="origin_place", filled="", hidden=True),
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert outcome.skipped == 0
    assert ("select", '[data-gj-idx="0"]', "2") in page.calls
    assert ("select", '[data-gj-idx="1"]', "1") in page.calls
    assert ("select", '[data-gj-idx="2"]', "1") in page.calls


def test_filler_cascade_native_waits_for_next_level_options(tmp_db):
    """下一级的选项要等上一级选完才刷出来：重读选项、等它出现，省 / 市 / 区都选中。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]', '[data-gj-idx="2"]'},
        native={
            '[data-gj-idx="0"]': [["", "所属省份"], ["1", "广东省"], ["2", "浙江省"]],
            # 市 / 区两级一开始只有占位项，靠 lazy_native 模拟「选完上一级才刷出来」
            '[data-gj-idx="1"]': [["", "所属城市"]],
            '[data-gj-idx="2"]': [["", "所属地区"]],
        },
        lazy_native={
            '[data-gj-idx="1"]': ("广东省", [["", "所属城市"], ["1", "广州市"]]),
            '[data-gj-idx="2"]': ("广州市", [["", "所属地区"], ["1", "天河区"]]),
        },
    )
    fields = [
        _field(
            index=0,
            tag="select",
            type="select",
            key="origin_place",
            filled="广东省广州市天河区",
            group=[1, 2],
        ),
        _field(index=1, tag="select", type="select", key="origin_place", filled="", hidden=True),
        _field(index=2, tag="select", type="select", key="origin_place", filled="", hidden=True),
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert outcome.failed == 0
    assert ("select", '[data-gj-idx="0"]', "1") in page.calls
    assert ("select", '[data-gj-idx="1"]', "1") in page.calls
    assert ("select", '[data-gj-idx="2"]', "1") in page.calls


def test_filler_cascade_reports_which_level_stalled(tmp_db):
    """省选好了、市却没刷出来：报「下一级没等到」，别整栏都说成「没有这一项」。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]'},
        native={
            '[data-gj-idx="0"]': [["", "所属省份"], ["1", "广东省"]],
            '[data-gj-idx="1"]': [["", "所属城市"]],
        },
    )
    fields = [
        _field(
            index=0,
            tag="select",
            type="select",
            key="origin_place",
            filled="广东省广州市",
            group=[1],
        ),
        _field(index=1, tag="select", type="select", key="origin_place", filled="", hidden=True),
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1
    assert ("select", '[data-gj-idx="0"]', "1") in page.calls
    assert "下一级没等到「广州市」" in fields[0].note


def test_filler_cascade_reports_when_level_missing(tmp_db):
    """级联某一级的下拉里没有对应的行政区时，老实报「请手动选择」。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]'},
        native={
            '[data-gj-idx="0"]': [["", "所属省份"], ["1", "广东省"]],
            '[data-gj-idx="1"]': [["", "所属城市"], ["1", "成都市"]],
        },
    )
    fields = [
        _field(
            index=0,
            tag="select",
            type="select",
            key="origin_place",
            filled="浙江省杭州市",
            group=[1],
        ),
        _field(index=1, tag="select", type="select", key="origin_place", filled="", hidden=True),
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1 and "请手动选择" in fields[0].note


def test_filler_cascade_reports_level_that_reverted(tmp_db):
    """整页 postback 把刚选的那一级冲回占位项：逐级回读发现后当场认输，别留半截结果。

    用户看到过的「生源地只填了省、市和县没进去」就是这么来的：控件收下了
    ``select_option``，回读却发现它又退回了占位项，而旧代码不校验，直接算成功。
    """
    page = _FakePage(
        selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]'},
        native={
            '[data-gj-idx="0"]': [["", "所属省份"], ["1", "广东省"]],
            '[data-gj-idx="1"]': [["", "所属城市"], ["1", "广州市"]],
        },
        # 市级这一栏选完又被官网冲回占位项
        readback={'[data-gj-idx="1"]': "所属城市"},
    )
    fields = [
        _field(
            index=0,
            tag="select",
            type="select",
            key="origin_place",
            filled="广东省广州市",
            group=[1],
        ),
        _field(index=1, tag="select", type="select", key="origin_place", filled="", hidden=True),
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1
    assert "第 2 级「广州市」选了没生效" in fields[0].note


def test_filler_native_date_writes_iso(tmp_db):
    """原生 date：把「2001/5/1」这类写法转成 ISO 再写进去。"""
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, key="birth", type="date", filled="2001/5/1")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("fill", '[data-gj-idx="0"]', "2001-05-01") in page.calls


def test_filler_element_date_clicks_cell(tmp_db):
    """组件库日期：展开面板、点中那一格，再回读校验。"""
    iso = "2001-05-01"
    page = _FakePage(
        selectors={'[data-gj-idx="0"]', f'[title="{iso}"]'},
        readback={'[data-gj-idx="0"]': iso},
    )
    fields = [_field(index=0, key="birth", filled="2001年5月1日", widget="date")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("click", f'[title="{iso}"]', "") in page.calls


def test_filler_uploads_multiple_files(tmp_db, tmp_path: Path):
    """多文件控件要传列表，不是单个路径。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, type="file", key="resume", filled="简历", multiple=True)]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("file", '[data-gj-idx="0"]', [str(cv)]) in page.calls


def test_filler_upload_falls_back_to_file_chooser(tmp_db, tmp_path: Path):
    """拖拽上传区藏起了 input：退一步走系统的文件选择框。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(selectors={'[data-gj-idx="0"]'}, refuse_upload={'[data-gj-idx="0"]'})
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("chooser", "", str(cv)) in page.calls


def test_filler_upload_falls_back_to_drop_zone(tmp_db, tmp_path: Path):
    """拖拽上传区：直接喂 input 不行时，往区域上派一个带文件的 drop 事件。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        refuse_upload={'[data-gj-idx="0"]'},
        drop_ok=True,
    )
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("drop", '[data-gj-idx="0"]', "cv.pdf") in page.calls


def test_filler_upload_reports_when_control_refuses(tmp_db, tmp_path: Path):
    """两条路都走不通时，如实报「请手动上传」。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        refuse_upload={'[data-gj-idx="0"]'},
        refuse_click={'[data-gj-idx="0"]'},
    )
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1 and "请手动上传" in fields[0].note


def test_filler_upload_hint_names_the_file_and_size(tmp_db, tmp_path: Path):
    """官网拿大小把人挡回来时，提示要写清是哪一份、多大——用户才知道该换什么。

    官网只弹一句「F_EXCEED_SIZE」，旧提示翻成「文件超过官网大小限制」就没了下文；
    本机同时躺着好几份简历时，用户根本不知道被拒的是哪一份。
    """
    cv = tmp_path / "林一舟.png"
    cv.write_bytes(b"x" * 1536)
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(selectors={'[data-gj-idx="0"]'}, upload_alert="F_EXCEED_SIZE")
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1
    assert "「林一舟.png」（2 KB）" in fields[0].note
    assert "文件超过官网大小限制" in fields[0].note


def test_filler_upload_confirms_when_site_writes_path_back(tmp_db, tmp_path: Path):
    """官网把服务器路径写回只读栏，才算真的传上去了。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        upload_path={'[data-gj-idx="0"]': ""},
    )
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1


def test_filler_upload_reports_site_rejection(tmp_db, tmp_path: Path):
    """官网自己校验不过（webuploader 弹 alert）时，如实报原因，别当传成功了。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        upload_path={'[data-gj-idx="0"]': ""},
        upload_alert="错误：文件大小超出限制！",
    )
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1
    assert "大小限制" in fields[0].note


def test_filler_upload_reports_when_control_swallows_file(tmp_db, tmp_path: Path):
    """控件把文件吞了（调用没报错、input.files 一直空）：如实报失败，别当传成功了。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(selectors={'[data-gj-idx="0"]'}, drop_upload={'[data-gj-idx="0"]'})
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1 and "没能喂进" in fields[0].note


def test_filler_upload_pre_checks_accept_type(tmp_db, tmp_path: Path):
    """官网只收 Excel、本机简历是 PNG：喂之前就报清楚，别让官网弹一堆天书。"""
    cv = tmp_path / "cv.png"
    cv.write_bytes(b"png-bytes")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        accept={'[data-gj-idx="0"]': ".xls,.xlsx"},
    )
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.failed == 1 and "只收" in fields[0].note
    assert not any(call[0] == "file" for call in page.calls)  # 类型不对，连喂都没喂


def test_filler_cascade_fills_available_levels_when_parts_exceed(tmp_db):
    """资料里的地址比官网的下拉级数还多时，能填的级照填，不整栏失败。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]'},
        native={
            '[data-gj-idx="0"]': [["", "所属省份"], ["1", "浙江省"]],
            '[data-gj-idx="1"]': [["", "所属城市"], ["1", "杭州市"]],
        },
    )
    fields = [
        _field(
            index=0,
            tag="select",
            type="select",
            key="origin_place",
            filled="浙江省杭州市西湖区",
            group=[1],
        ),
        _field(index=1, tag="select", type="select", key="origin_place", filled="", hidden=True),
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert ("select", '[data-gj-idx="0"]', "1") in page.calls
    assert ("select", '[data-gj-idx="1"]', "1") in page.calls


# --- 助手：打开 → 扫描 → 填表 → 提交 -----------------------------------------


def test_open_task_scans_and_stores_fields(tmp_db):
    uid = _seed_job("j1")
    ApplyProfileSettings(name="张三", phone="13900139000").save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    result = asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    assert page.goto_calls == ["https://acme.cn/apply"]
    assert result["gate"] == ""
    task = result["task"]
    assert task["url"] == "https://acme.cn/apply" and task["domain"] == "acme.cn"
    assert task["fields"][0]["label"] == "姓名" and task["fields"][0]["filled"] == "张三"
    assert task["status"] == "pending"


def test_open_task_reopen_does_not_renavigate(tmp_db):
    """重开面板（同一条任务、同一个地址）不再导航：用户刚在浏览器里填的东西不会被冲掉。

    官网表单常在填完一栏后跳到下一步（地址跟着变），这时重开面板若再 ``goto``
    一次，会把进度拽回第一步——这就是「重新点填表又从头加载」的根子。
    """
    uid = _seed_job("j1")
    ApplyProfileSettings(name="张三").save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    # 用户手填之后页面自己跳到了下一步：重开面板不该把它拽回第一步
    page.url = "https://acme.cn/apply/step2"
    asyncio.run(assistant.open_task(uid, ""))
    assert page.goto_calls == ["https://acme.cn/apply"]


def test_open_task_renavigates_after_url_change(tmp_db):
    """用户换了网申地址：必须按新地址重新导航，不能拿旧页面顶包。"""
    uid = _seed_job("j1")
    ApplyProfileSettings(name="张三").save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    asyncio.run(assistant.open_task(uid, "https://beta.cn/form"))
    assert page.goto_calls == ["https://acme.cn/apply", "https://beta.cn/form"]


def test_open_task_without_url_is_rejected(tmp_db):
    uid = _seed_job("j1")
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    with pytest.raises(ApplyError, match="网申地址"):
        asyncio.run(assistant.open_task(uid))
    assert ApplicationRow.get_dict(uid) is None  # 没地址就不建任务


def test_open_task_rejects_unknown_task(tmp_db):
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    with pytest.raises(ApplyError, match="网申任务不存在"):
        asyncio.run(assistant.open_task("nope", "https://a.com/apply"))


def test_open_task_creates_standalone_from_url_only(tmp_db):
    """不挑岗位、只给官网地址，也能建出一条独立的网申任务。"""
    page = _FakePage(
        text="",
        meta={"title": "后端开发实习生 - 云图科技", "site": "云图科技", "h1": "后端开发实习生", "host": "jobs.yuntu.cn"},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    result = asyncio.run(assistant.open_task("", "https://jobs.yuntu.cn/apply/1"))
    task = result["task"]
    assert task["uid"].startswith("apply:")
    assert task["jobUid"] == "" and task["platform"] == "web"
    assert task["domain"] == "jobs.yuntu.cn"
    assert task["company"] == "云图科技" and task["title"] == "后端开发实习生"


def test_open_task_requires_url_when_no_uid(tmp_db):
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    with pytest.raises(ApplyError, match="网申地址"):
        asyncio.run(assistant.open_task("", "   "))


def test_assistant_outline_only_when_ai_ready(tmp_db):
    """可访问性大纲是给 AI 兜底用的：没开 AI 服务就别去算，省一趟真浏览器的活。"""
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    calls: list[Any] = []

    class _Scanner:
        async def outline(self, page: Any) -> str:
            calls.append(page)
            return '- heading "教育经历"'

    assistant.scanner = _Scanner()  # type: ignore[assignment]
    page = _FakePage()

    class _Matcher:
        def __init__(self, llm: LlmSettings, profile: ApplyProfile | None) -> None:
            self.llm = llm
            self.profile = profile

    ready = LlmSettings(api_key="k", model="m")
    off = _Matcher(LlmSettings(), _profile())
    assert asyncio.run(assistant._outline(page, off)) == ""  # type: ignore[arg-type]
    # 资料是空的（还没填过网申资料）也算了：没资料就没得填，别白扫一遍
    blank = _Matcher(ready, _profile())
    assert asyncio.run(assistant._outline(page, blank)) == ""  # type: ignore[arg-type]
    assert calls == []

    on = _Matcher(ready, _profile(name="林晓", phone="13900000001"))
    assert asyncio.run(assistant._outline(page, on)) == '- heading "教育经历"'  # type: ignore[arg-type]
    assert calls == [page]


def test_open_task_alerts_on_captcha(tmp_db):
    uid = _seed_job("j1")
    session = _FakeSession(_FakePage(text="请完成安全验证后继续"))
    assistant = ApplyAssistant(session)
    result = asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    assert "安全验证" in result["gate"]
    # 这是用户自己点「填表」发起的动作：窗口摆到眼前一次，好让他顺手把验证码点了；
    # 拦路提示本身只留在面板和日志里，不会再额外动窗口
    assert session.surfaced == 1
    # 「看一眼」不是改设置：不许顺手把「后台静默运行」关掉，否则后面每个查询
    # 任务都会把窗口顶到用户面前
    assert session.background == []
    assert "安全验证" in ApplicationRow.get_dict(uid)["note"]


def test_scan_auto_solves_inline_captcha_without_gate(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """表单里的一栏验证码：没有拦路虎也要自动识图填上，不留备注。"""
    uid = _seed_job("j1")
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "验证码", "path": "#verify_code"}],
        selectors={'[data-gj-idx="0"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))

    async def _solve(_page: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "code": "a8k2",
            "filled": True,
            "detected": True,
            "message": "已自动识别并填写验证码：a8k2",
        }

    monkeypatch.setattr(assistant, "_solve_captcha", _solve)
    result = asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    assert result["gate"] == ""
    assert result["captcha"]["filled"] is True
    assert ApplicationRow.get_dict(uid)["note"] == ""


def test_scan_keeps_note_when_captcha_unreadable(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """页面上确有验证码但没读出来：备注留下提示，不静默。"""
    uid = _seed_job("j1")
    assistant = ApplyAssistant(_FakeSession(_FakePage()))

    async def _solve(_page: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "",
            "filled": False,
            "detected": True,
            "message": "没读出验证码，请手动填写",
        }

    monkeypatch.setattr(assistant, "_solve_captcha", _solve)
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    assert "没读出验证码" in ApplicationRow.get_dict(uid)["note"]


def test_scan_clears_stale_note_when_page_is_clean(tmp_db):
    """上次留下的拦路备注，这次扫描干净了要清掉。"""
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://acme.cn/apply")
    ApplicationRow.save_fields(uid, [], note="页面出现「验证码」，请手动处理后再继续")
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    asyncio.run(ApplyAssistant(_FakeSession(page)).open_task(uid, "https://acme.cn/apply"))
    assert ApplicationRow.get_dict(uid)["note"] == ""


def test_assistant_reuses_open_page(tmp_db):
    uid = _seed_job("j1")
    session = _FakeSession(_FakePage(raw=[], selectors=set()))
    assistant = ApplyAssistant(session)
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    asyncio.run(assistant.scan(uid))
    assert len(session.pages) == 1  # 第二次扫描复用同一个标签页


def test_fill_marks_manual_and_remembers(tmp_db):
    uid = _seed_job("j1")
    ApplyProfileSettings(name="张三", phone="13900139000").save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    result = asyncio.run(assistant.fill(uid, [{"index": 0, "key": "name", "filled": "李四"}]))
    assert result["outcome"]["filled"] == 1
    assert result["task"]["status"] == "filled"
    assert "已填 1 项" in result["task"]["note"]
    field = result["task"]["fields"][0]
    assert field["filled"] == "李四" and field["source"] == "手动"
    assert field["confidence"] == 1.0
    assert ApplyMappingRow.lookup("acme.cn", ["姓名|text"])["姓名|text"].value == "李四"


def test_fill_defer_returns_pending_then_broadcasts(tmp_db):
    """「填入浏览器」挂后台跑：请求立刻交回 pending，跑完广播一次结果。

    整条填表链（逐栏写 + 回读 + AI 接手补全）少说一分钟，同步等下来前端会先报
    「请求超时，请确认后台服务是否在运行」。
    """
    uid = _seed_job("j1")
    ApplyProfileSettings(name="张三").save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    updates: list[tuple] = []

    async def scenario() -> tuple[dict, ApplyAssistant]:
        assistant = ApplyAssistant(
            _FakeSession(page), on_update=lambda *args: updates.append(args)
        )
        await assistant.open_task(uid, "https://acme.cn/apply")
        result = await assistant.fill(
            uid, [{"index": 0, "key": "name", "filled": "李四"}], defer=True
        )
        assert assistant._filling == {uid}  # 已经挂在后台了
        await asyncio.gather(*assistant._bg)
        return result, assistant

    result, assistant = asyncio.run(scenario())
    # 面板先拿到一张「正在填」的空表，不等结果
    assert result["pending"] is True
    assert result["outcome"] == {}
    assert result["task"]["uid"] == uid
    assert assistant._filling == set()
    # 后台这一趟确实填进去了
    saved = ApplicationRow.get_dict(uid)
    assert saved["status"] == "filled"
    assert saved["fields"][0]["filled"] == "李四"
    # 跑完广播一次，面板拿 outcome 提示「已填几项」
    assert len(updates) == 1
    task, learned, outcome = updates[0]
    assert task["uid"] == uid
    assert learned == []
    assert outcome["filled"] == 1


def test_fill_defer_skips_second_run_while_filling(tmp_db):
    """同一张页面不许开第二遍：上一遍还在跑时再点「填入浏览器」不会又挂一条。"""
    uid = _seed_job("j1")
    ApplyProfileSettings(name="张三").save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    updates: list[tuple] = []

    async def scenario() -> tuple[dict, dict]:
        assistant = ApplyAssistant(
            _FakeSession(page), on_update=lambda *args: updates.append(args)
        )
        await assistant.open_task(uid, "https://acme.cn/apply")
        # 让第一遍卡在后台（真实场景里这一遍要跑几十秒），再点第二下
        gate = asyncio.Event()
        inner = assistant._fill_now

        async def blocked(state):  # type: ignore[no-untyped-def]
            await gate.wait()
            await inner(state)

        assistant._fill_now = blocked  # type: ignore[method-assign]
        first = await assistant.fill(uid, [], defer=True)
        second = await assistant.fill(uid, [], defer=True)
        assert assistant._filling == {uid}
        gate.set()
        await asyncio.gather(*assistant._bg)
        return first, second

    first, second = asyncio.run(scenario())
    assert first["pending"] is True and second["pending"] is True
    assert len(updates) == 1  # 第二下没再跑一遍


def test_submit_marks_done_and_closes_page(tmp_db):
    uid = _seed_job("j1")
    page = _FakePage(raw=[], selectors=set())
    assistant = ApplyAssistant(_FakeSession(page))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    saved = asyncio.run(assistant.submit(uid))
    assert saved["status"] == "submitted" and saved["statusLabel"] == "已提交"
    assert page.is_closed() is True and uid not in assistant._pages


def test_fill_learns_new_manual_field(tmp_db):
    """用户在某个官网手工补的新字段回流进本机资料库：换个官网再遇到就有值可填。"""
    uid = _seed_job("j1")
    ApplyProfileSettings(name="张三").save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "籍贯地", "path": "#h"}],
        selectors={'[data-gj-idx="0"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    asyncio.run(assistant.fill(uid, [{"index": 0, "key": "籍贯地", "filled": "杭州"}]))
    assert ApplyLearnedRow.values() == {"籍贯地": "杭州"}


def test_fill_does_not_learn_sensitive_or_known_field(tmp_db):
    """敏感事实（身份证）与库里已有的键都不回流：只学「资料库本来缺的那一条」。"""
    uid = _seed_job("j1")
    ApplyProfileSettings(name="张三").save()
    page = _FakePage(
        raw=[
            {"tag": "input", "type": "text", "labelText": "身份证号", "path": "#i"},
            {"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"},
        ],
        selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    asyncio.run(
        assistant.fill(
            uid,
            [
                {"index": 0, "key": "id_card", "filled": "330100200105010000"},
                {"index": 1, "key": "name", "filled": "李四"},
            ],
        )
    )
    assert ApplyLearnedRow.values() == {}


def test_fill_learns_manual_field_under_canonical_key(tmp_db):
    """用户手补的「民族」按同义归并成内置键 nation 存下来，换个官网直接有值可填。"""
    uid = _seed_job("j1")
    ApplyProfileSettings(name="张三").save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "民族", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    asyncio.run(assistant.fill(uid, [{"index": 0, "key": "民族", "filled": "汉族"}]))
    assert ApplyLearnedRow.values() == {"nation": "汉族"}


def test_reopen_undoes_submit_and_clears_timestamp(tmp_db):
    """误点「已提交」后退回上一步：状态回已填表，提交时间清掉。"""
    uid = _seed_job("j1")
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    asyncio.run(assistant.submit(uid))
    assert ApplicationRow.get_dict(uid)["submittedAt"]

    saved = asyncio.run(assistant.reopen(uid))
    assert saved["status"] == "filled" and saved["statusLabel"] == "已填表"
    assert not saved["submittedAt"]
    assert saved["note"] == "已撤销提交"


def test_reopen_without_fields_goes_back_to_pending(tmp_db):
    """还没扫到过表单就退回「待网申」，别凭空说成已填表。"""
    uid = _seed_job("j1")
    assistant = ApplyAssistant(_FakeSession(_FakePage(raw=[], selectors=set())))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    asyncio.run(assistant.submit(uid))
    assert asyncio.run(assistant.reopen(uid))["status"] == "pending"


def test_delete_removes_task_and_page(tmp_db):
    uid = _seed_job("j1")
    page = _FakePage(raw=[], selectors=set())
    assistant = ApplyAssistant(_FakeSession(page))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    assert asyncio.run(assistant.delete(uid)) is True
    assert ApplicationRow.get_dict(uid) is None
    assert asyncio.run(assistant.delete(uid)) is False


def test_fill_and_close_without_task_are_safe(tmp_db):
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    with pytest.raises(ApplyError, match="网申任务不存在"):
        asyncio.run(assistant.fill("nope"))
    asyncio.run(assistant.close_page("nope"))  # 没开过页面，不该抛


# --- API：路由层 --------------------------------------------------------------


def test_apply_profile_config_round_trip(client: TestClient):
    data = client.get("/api/config/apply-profile").json()
    assert data["labels"]["name"] == "姓名" and data["labels"]["phone"] == "手机号"
    assert data["ready"] is False and data["values"]["name"] == ""
    assert "extra" not in data and "secret" not in data
    saved = client.put(
        "/api/config/apply-profile",
        json={"values": {"name": "张三", "phone": "13900139000"}},
    ).json()
    assert saved["values"]["name"] == "张三" and saved["ready"] is True
    assert saved["values"]["phone"] == "13900139000"
    # 没传的字段保持原样
    trimmed = client.put("/api/config/apply-profile", json={"values": {"phone": ""}}).json()
    assert trimmed["values"]["name"] == "张三" and trimmed["values"]["phone"] == ""


def test_config_bundle_exposes_apply_profile(client: TestClient):
    cfg = client.get("/api/config").json()
    assert cfg["apply_profile"]["labels"]["phone"] == "手机号"


def test_apply_learned_endpoints(client: TestClient):
    """本机学会的字段：接口能列出来、进资料，也能逐条忘掉、整表清空。"""
    ApplyLearnedRow.learn("acme.cn", [{"key": "员工编号", "label": "员工编号", "value": "A-1024"}])
    data = client.get("/api/config/apply-profile").json()
    assert [(r["key"], r["value"]) for r in data["learned"]] == [("员工编号", "A-1024")]
    assert data["values"]["员工编号"] == "A-1024"  # 记忆要能进对照表的下拉
    forgotten = client.delete("/api/config/apply-learned/员工编号").json()
    assert forgotten["learned"] == [] and not forgotten["values"].get("员工编号")
    ApplyLearnedRow.learn("acme.cn", [{"key": "员工编号", "value": "A-1024"}])
    cleared = client.delete("/api/config/apply-learned").json()
    assert cleared["learned"] == [] and ApplyLearnedRow.values() == {}


def test_apply_learned_builtin_key_folds_into_its_own_group(client: TestClient):
    """学到的是资料库里本来就有的栏（身高）：并进那一栏显示成「记忆」，不再单列一条。"""
    ApplyLearnedRow.learn(
        "acme.cn", [{"key": "身高(厘米)", "label": "身高(厘米)", "value": "165"}]
    )
    data = client.get("/api/config/apply-profile").json()
    assert data["learned"] == []
    heights = [
        field
        for group in data["tree"]["groups"]
        if group["kind"] == "group"
        for field in group["fields"]
        if field["canonical"] == "height"
    ]
    assert len(heights) == 1
    height = heights[0]
    # 来源照实标：这条是用户在官网页面上自己敲的，不是「网申资料」里写的那份
    assert height["value"] == "165" and height["source"] == "手动"
    # 并过来的那条还得能整条删掉，删的是库里的原键
    assert height["forget_key"] == "身高(厘米)" and height["memory"] is True
    assert data["values"]["height"] == "165"


def test_apply_learned_confirm_clears_pending(client: TestClient):
    """核对一条 AI 推理：确认之后来源变「手动」，树上「待核对」撤掉。

    推得对的走确认，推得不对的走删除；确认过的这条以后不再复核。
    """
    ApplyLearnedRow.learn(
        "acme.cn",
        [{"key": "婚姻状况", "label": "婚姻状况", "value": "未婚",
          "source": "AI推算", "basis": "按年龄推的"}],
    )
    before = client.get("/api/config/apply-profile").json()
    marital = next(
        field
        for group in before["tree"]["groups"]
        if group["kind"] == "group"
        for field in group["fields"]
        if field["canonical"] == "marital"
    )
    assert marital["source"] == "AI推算" and marital["pending"] is True
    # 确认：接口回整包资料，来源换成「手动」，待核对撤掉
    after = client.post("/api/config/apply-learned/confirm", json={"key": "婚姻状况"}).json()
    marital = next(
        field
        for group in after["tree"]["groups"]
        if group["kind"] == "group"
        for field in group["fields"]
        if field["canonical"] == "marital"
    )
    assert marital["source"] == "手动" and not marital["pending"]
    assert marital["value"] == "未婚"  # 值本身不动


def test_apply_learned_confirm_rejects_non_inferred(client: TestClient):
    """确认只对「模型推的」有意义：手填的、不在库里的、空键一律 400。"""
    assert client.post("/api/config/apply-learned/confirm", json={"key": "  "}).status_code == 400
    assert client.post(
        "/api/config/apply-learned/confirm", json={"key": "不存在的键"}
    ).status_code == 400
    # 用户手填的那条本来就不带「待核对」，没有可确认的东西
    ApplyLearnedRow.learn(
        "acme.cn", [{"key": "员工编号", "label": "员工编号", "value": "A-1024"}]
    )
    assert client.post(
        "/api/config/apply-learned/confirm", json={"key": "员工编号"}
    ).status_code == 400


def test_apply_learned_sensitive_row_shows_plaintext(client: TestClient):
    """学到手的身份证并进「身份证号」那片叶子：全程本机，界面明文回显、不再掩码。"""
    ApplyLearnedRow.learn(
        "acme.cn",
        [
            {"key": "证件号码", "label": "证件号码", "value": "110101199001011234"},
            {"key": "员工编号", "label": "员工编号", "value": "A-1024"},
        ],
    )
    data = client.get("/api/config/apply-profile").json()
    rows = {row["key"]: row for row in data["learned"]}
    # 身份证是内置「身份证号」的别名，值并进那片叶子；列表里只剩资料库没这一栏的那条
    assert "证件号码" not in rows and rows["员工编号"]["value"] == "A-1024"
    # 树上明文回显（本机操作、不上网），不再有 secret 掩码标记
    leaves = [
        field
        for group in data["tree"]["groups"]
        for field in group["fields"]
        if field["canonical"] == "id_card"
    ]
    assert len(leaves) == 1 and leaves[0]["key"] == "id_card"
    assert leaves[0]["value"] == "110101199001011234"
    assert "secret" not in leaves[0]
    assert not [
        field
        for group in data["tree"]["groups"]
        if group["title"] == "本机学会"
        for field in group["fields"]
        if field["canonical"] == "id_card"
    ]
    # 只是不回显，填表照旧用得上：记忆里那条值还在
    assert ApplyLearnedRow.values()["证件号码"] == "110101199001011234"


def test_apply_tree_folds_learned_aliases_into_one_leaf(client: TestClient):
    """学到的一批按规范键去重：官网上「身高(厘米)」和内置的「身高」是同一件事。

    不去重的话树上会并排出现两条同值资料：内置分组里一条「身高」，「本机学会」
    里再来一条「身高(厘米)」。内置字典认得出的键就该并回内置那片叶子，只有真的
    没处可归的自定字段（这里指 ``员工编号``）才留在「本机学会」里。
    """
    ApplyLearnedRow.learn(
        "acme.cn",
        [
            {"key": "身高(厘米)", "label": "身高(厘米)", "value": "165"},
            {"key": "height", "label": "身高", "value": "165"},
            {"key": "员工编号", "label": "员工编号", "value": "A-1024"},
        ],
    )
    data = client.get("/api/config/apply-profile").json()
    learned = [
        field
        for group in data["tree"]["groups"]
        if group["title"] == "本机学会"
        for field in group["fields"]
    ]
    # 两条身高都并回内置的「身高」，本机学会里只剩认不出归属的那条
    assert [field["canonical"] for field in learned] == ["员工编号"]
    assert learned[0]["value"] == "A-1024"
    heights = [
        field
        for group in data["tree"]["groups"]
        if group["kind"] == "group"
        for field in group["fields"]
        if field["canonical"] == "height"
    ]
    assert len(heights) == 1
    height = heights[0]
    # 标签用内置的中文名，值取学到的那条，来源照实标成「手动」
    assert height["key"] == "height" and height["label"] == "身高"
    assert height["value"] == "165" and height["source"] == "手动"


def test_apply_tasks_empty_and_delete_missing(client: TestClient):
    assert client.get("/api/apply/tasks").json() == []
    assert client.delete("/api/apply/task/nope").status_code == 404


def test_apply_open_requires_job_and_url(client: TestClient):
    uid = _seed_job("j1")
    missing_url = client.post("/api/apply/task", json={"job_uid": uid})
    assert missing_url.status_code == 400 and "网申地址" in missing_url.json()["detail"]
    assert client.post("/api/apply/task", json={"job_uid": "  "}).status_code == 400


def test_apply_set_url_validates_scheme(client: TestClient):
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://old.example.com/form")
    bad = client.put(f"/api/apply/task/{uid}/url", json={"url": "file:///etc/passwd"})
    assert bad.status_code == 400 and "http" in bad.json()["detail"]
    ok = client.put(f"/api/apply/task/{uid}/url", json={"url": "https://www.new.example.com/apply"})
    assert ok.status_code == 200 and ok.json()["domain"] == "new.example.com"
    assert client.put("/api/apply/task/nope/url", json={"url": "https://a.com"}).status_code == 404


def test_apply_task_endpoints_need_existing_task(client: TestClient):
    assert client.post("/api/apply/task/nope/scan").status_code == 400
    assert client.post("/api/apply/task/nope/fill", json={}).status_code == 400
    assert client.post("/api/apply/task/nope/learn").status_code == 400
    assert client.post("/api/apply/task/nope/submit").status_code == 400
    assert client.post("/api/apply/task/nope/reopen").status_code == 400
    detail = client.post("/api/apply/task/nope/scan").json()["detail"]
    assert "网申任务不存在" in detail


def test_apply_list_filter_and_delete(client: TestClient):
    first, second = _seed_job("j1"), _seed_job("j2")
    ApplicationRow.create(first, "https://a.com/form")
    ApplicationRow.create(second, "https://a.com/form")
    ApplicationRow.set_status(second, "filled")
    assert [r["uid"] for r in client.get("/api/apply/tasks").json()] == [second, first]
    filled = client.get("/api/apply/tasks", params={"status": "filled"}).json()
    assert [r["uid"] for r in filled] == [second]
    assert client.delete(f"/api/apply/task/{first}").json() == {"ok": True}
    assert client.delete(f"/api/apply/task/{first}").status_code == 404


# --- 验证码识别 -------------------------------------------------------------


def test_captcha_settings_round_trip(tmp_db):
    """开关与识图模型要能存下来、读回来。"""
    CaptchaSettings(enabled=True, model="qwen-vl").save()
    loaded = CaptchaSettings.load()
    assert loaded.enabled is True and loaded.model == "qwen-vl"
    CaptchaSettings().save()
    assert CaptchaSettings.load().enabled is False


def test_captcha_solver_degrades_when_disabled(tmp_db):
    """没开功能、没配大模型时，都只是老实说「请手动填」，绝不抛异常。"""
    solver = CaptchaSolver(settings=CaptchaSettings(enabled=False), llm=LlmSettings())
    assert solver.enabled is False
    assert "未开启" in asyncio.run(solver.solve(_FakePage())).message
    solver = CaptchaSolver(settings=CaptchaSettings(enabled=True), llm=LlmSettings())
    assert solver.enabled is False
    assert "大模型服务" in asyncio.run(solver.solve(_FakePage())).message


def test_captcha_solver_finds_image_and_input(tmp_db):
    """页面脚本要能把验证码图与它旁边的输入框配对取出来。"""
    page = _FakePage(captcha={"image": "#cap-img", "input": "#cap-code"})
    solver = CaptchaSolver(settings=CaptchaSettings(enabled=True))
    found = asyncio.run(solver._find(page))
    assert found == (0, {"image": "#cap-img", "input": "#cap-code"})


def test_captcha_solver_returns_none_when_absent(tmp_db):
    solver = CaptchaSolver(settings=CaptchaSettings(enabled=True))
    assert asyncio.run(solver._find(_FakePage(captcha=None))) is None


def test_captcha_local_ocr_reads_clean_image(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """本地 OCR 读得又准又干净时，直接采用，不去打扰大模型。"""
    from job.apply import captcha as captcha_mod

    monkeypatch.setattr(captcha_mod.ocr, "lines", lambda _png: [("a8K2", 0.95, 0.0, 0.0)])
    solver = CaptchaSolver(settings=CaptchaSettings(enabled=True), llm=LlmSettings())
    assert asyncio.run(solver._read_local(b"png")) == "a8K2"


def test_captcha_local_ocr_declines_when_unclear(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """低置信度、多行、字符数不对、引擎缺失，一律返回空串交给大模型。"""
    from job.apply import captcha as captcha_mod

    solver = CaptchaSolver(settings=CaptchaSettings(enabled=True), llm=LlmSettings())
    monkeypatch.setattr(captcha_mod.ocr, "lines", lambda _png: [("a8", 0.95, 0.0, 0.0)])
    assert asyncio.run(solver._read_local(b"png")) == ""
    monkeypatch.setattr(captcha_mod.ocr, "lines", lambda _png: [("a8k2", 0.2, 0.0, 0.0)])
    assert asyncio.run(solver._read_local(b"png")) == ""
    monkeypatch.setattr(
        captcha_mod.ocr, "lines", lambda _png: [("ab", 0.9, 0.0, 0.0), ("cd", 0.9, 9.0, 0.0)]
    )
    assert asyncio.run(solver._read_local(b"png")) == ""

    def _boom(_png: bytes) -> Any:
        raise ValueError("图片识别组件未安装")

    monkeypatch.setattr(captcha_mod.ocr, "lines", _boom)
    assert asyncio.run(solver._read_local(b"png")) == ""


def _captcha_solver_with_reply(
    monkeypatch: pytest.MonkeyPatch,
    *,
    content: str,
    reasoning: str = "",
    finish: str = "stop",
) -> tuple[CaptchaSolver, dict[str, Any]]:
    """造一个假的多模态响应，用来单独验证 ``_recognize`` 的取字逻辑。"""
    solver = CaptchaSolver(
        settings=CaptchaSettings(enabled=True, model="vlm"),
        llm=LlmSettings(api_key="k", model="vlm"),
    )
    seen: dict[str, Any] = {}

    class _Message:
        def __init__(self) -> None:
            self.content = content
            self.reasoning_content = reasoning

    class _Choice:
        def __init__(self) -> None:
            self.message = _Message()
            self.finish_reason = finish

    class _Response:
        def __init__(self) -> None:
            self.choices = [_Choice()]

    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return _Response()

    class _Client:
        def __init__(self) -> None:
            self.chat = type("_Chat", (), {"completions": _Completions()})()

    class _Llm:
        model = "vlm"

        def __init__(self) -> None:
            self.provider = type("_Provider", (), {"client": _Client()})()

    monkeypatch.setattr(solver, "llm", _Llm())
    return solver, seen


def test_captcha_recognize_reads_model_content(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型正常给出正文时取正文，并留足 token 余量（推理型模型会先写思考）。"""
    solver, seen = _captcha_solver_with_reply(monkeypatch, content=" a8k2 ")
    assert asyncio.run(solver._recognize(b"png")) == "a8k2"
    assert seen["max_tokens"] >= 256


def test_captcha_recognize_falls_back_to_reasoning_tail(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """推理型模型只把答案写进思考过程、正文留空时，从思考结尾捞一次。"""
    solver, _seen = _captcha_solver_with_reply(
        monkeypatch,
        content="",
        reasoning="用户需要识别验证码，图里的字符是 27mmr，所以直接输出 27mmr。",
    )
    assert asyncio.run(solver._recognize(b"png")) == "27mmr"


def test_captcha_recognize_skips_truncated_reasoning(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """思考被 token 截断时思考本身也不完整，宁可不填也不从残句里猜。"""
    solver, _seen = _captcha_solver_with_reply(
        monkeypatch,
        content="",
        reasoning="识别字符：第一个像 '2'，第二个像 '7'，第三个是 'm'，第四个",
        finish="length",
    )
    assert asyncio.run(solver._recognize(b"png")) == ""


def test_captcha_recognize_declines_when_model_gives_up(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型直说看不清时，如实退回「请手动填写」。"""
    solver, _seen = _captcha_solver_with_reply(monkeypatch, content="看不清")
    assert asyncio.run(solver._recognize(b"png")) == ""


def test_captcha_solver_asks_model_only_after_local_fails(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """本地读不出来才走多模态；两条路并联，不是串行。"""
    solver = CaptchaSolver(
        settings=CaptchaSettings(enabled=True), llm=LlmSettings(api_key="k", model="m")
    )
    seen: list[str] = []

    async def _find(_page: Any) -> tuple[int, dict[str, str]]:
        return 0, {"image": "#i", "input": "#n"}

    async def _shot(_page: Any, _index: int, _selector: str) -> bytes:
        return b"png"

    async def _local(_png: bytes) -> str:
        return ""

    async def _model(_png: bytes) -> str:
        seen.append("model")
        return "a8k2"

    async def _fill(_page: Any, _index: int, _selector: str, code: str) -> bool:
        seen.append(code)
        return True

    monkeypatch.setattr(solver, "_find", _find)
    monkeypatch.setattr(solver, "_shot", _shot)
    monkeypatch.setattr(solver, "_read_local", _local)
    monkeypatch.setattr(solver, "_recognize", _model)
    monkeypatch.setattr(solver, "_fill", _fill)
    result = asyncio.run(solver.solve(_FakePage()))
    assert result.ok and result.filled and result.code == "a8k2"
    assert seen == ["model", "a8k2"]


def test_captcha_shot_waits_for_image_to_load(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """刚打开页面就截图会拿到空白图：先等位图解码完，再截。"""
    from job.apply import captcha as captcha_mod

    monkeypatch.setattr(captcha_mod, "_LOAD_WAIT", 1.0)
    solver = CaptchaSolver(settings=CaptchaSettings(enabled=True), llm=LlmSettings())
    calls = {"n": 0}

    class _Loc:
        async def evaluate(self, _expr: str) -> bool:
            calls["n"] += 1
            return calls["n"] >= 3

        async def screenshot(self) -> bytes:
            return b"png"

    monkeypatch.setattr(solver, "_locator", lambda *_a, **_k: _Loc())
    assert asyncio.run(solver._shot(None, 0, "#cap")) == b"png"
    assert calls["n"] == 3


def test_captcha_shot_gives_up_waiting_and_still_shoots(
    tmp_db, monkeypatch: pytest.MonkeyPatch
):
    """等不到加载完也不强求，照旧截一张，让识图环节去判断。"""
    from job.apply import captcha as captcha_mod

    monkeypatch.setattr(captcha_mod, "_LOAD_WAIT", 0.05)
    solver = CaptchaSolver(settings=CaptchaSettings(enabled=True), llm=LlmSettings())

    class _Loc:
        async def evaluate(self, _expr: str) -> bool:
            return False

        async def screenshot(self) -> bytes:
            return b"png"

    monkeypatch.setattr(solver, "_locator", lambda *_a, **_k: _Loc())
    assert asyncio.run(solver._shot(None, 0, "#cap")) == b"png"


def test_captcha_shot_returns_none_when_locator_missing(
    tmp_db, monkeypatch: pytest.MonkeyPatch
):
    """选择器定位不到元素时，如实返回空，交给上层提示「请手动填写」。"""
    solver = CaptchaSolver(settings=CaptchaSettings(enabled=True), llm=LlmSettings())
    monkeypatch.setattr(solver, "_locator", lambda *_a, **_k: None)
    assert asyncio.run(solver._shot(None, 0, "#cap")) is None


def test_config_bundle_exposes_captcha(client: TestClient):
    assert client.get("/api/config").json()["captcha"] == {"enabled": False, "model": ""}


def test_captcha_config_round_trip(client: TestClient):
    saved = client.put("/api/config/captcha", json={"enabled": True, "model": " glm-4v "}).json()
    assert saved == {"enabled": True, "model": "glm-4v"}
    assert client.get("/api/config").json()["captcha"]["model"] == "glm-4v"


def test_apply_captcha_endpoint_needs_task(client: TestClient):
    assert client.post("/api/apply/task/nope/captcha").status_code == 400


# --- 表单定位兜底：截图 + 多模态指认，默认关闭、限个数、失败回落 -------------


def _locator(profile: ApplyProfile, **kw: Any) -> FieldLocator:
    """开着定位兜底的定位器；大模型只是摆设，``_ask`` 由各用例替换。"""
    return FieldLocator(
        profile,
        llm=LlmSettings(api_key="k", model="m"),
        settings=ApplyLocateSettings(enabled=True, **kw),
    )


def test_locate_settings_round_trip(tmp_db):
    """护栏（开关 / 个数 / 超时）要能存下来、读回来，默认是关的。"""
    ApplyLocateSettings(enabled=True, max_fields=5, timeout=30).save()
    loaded = ApplyLocateSettings.load()
    assert (loaded.enabled, loaded.max_fields, loaded.timeout) == (True, 5, 30)
    ApplyLocateSettings().save()
    assert ApplyLocateSettings.load().enabled is False
    assert ApplyLocateSettings.load().max_fields == 3


def test_locate_disabled_by_default_does_nothing(tmp_db):
    """没开功能时连页面都不碰：不截图、不填值。"""
    page = _FakePage()
    fields = [_field(index=0, label="毕业院校")]
    locator = FieldLocator(_profile(school="清华大学"))
    assert locator.enabled is False
    assert asyncio.run(locator.locate(page, fields)) == 0
    assert page.calls == [] and fields[0].filled == ""


def test_locate_needs_model_and_profile(tmp_db):
    """缺大模型或缺资料，都谈不上定位。"""
    off = FieldLocator(
        _profile(school="清华大学"),
        llm=LlmSettings(),
        settings=ApplyLocateSettings(enabled=True),
    )
    assert off.enabled is False
    empty = FieldLocator(
        ApplyProfile([]),
        llm=LlmSettings(api_key="k", model="m"),
        settings=ApplyLocateSettings(enabled=True),
    )
    assert empty.enabled is False


def test_locate_fills_field_from_screenshot(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """认出是哪一栏就补上值，来源记成「AI 定位」。"""
    page = _FakePage()
    fields = [_field(index=0, label="毕业院校", type="text")]
    locator = _locator(_profile(school="清华大学"))

    async def _ask(_field: Any, _options: Any, _png: bytes) -> str:
        return "school"

    monkeypatch.setattr(locator, "_ask", _ask)
    assert asyncio.run(locator.locate(page, fields)) == 1
    assert fields[0].filled == "清华大学"
    assert fields[0].source == "AI 定位" and fields[0].ok is True
    assert ("shot", '[data-gj-shot="1"]', "") in page.calls


def test_locate_parses_chatty_answer(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型多写一句也能取到键。"""
    page = _FakePage()
    fields = [_field(index=0, label="毕业院校")]
    locator = _locator(_profile(school="清华大学"))

    async def _ask(_field: Any, _options: Any, _png: bytes) -> str:
        return "答案：school"

    monkeypatch.setattr(locator, "_ask", _ask)
    assert asyncio.run(locator.locate(page, fields)) == 1
    assert fields[0].key == "school"


def test_locate_gives_up_when_model_unsure(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型说判断不了：留空并标「请手动填写」，绝不硬塞。"""
    page = _FakePage()
    fields = [_field(index=0, label="这一栏是什么")]
    locator = _locator(_profile(school="清华大学"))

    async def _ask(_field: Any, _options: Any, _png: bytes) -> str:
        return "无法判断"

    monkeypatch.setattr(locator, "_ask", _ask)
    assert asyncio.run(locator.locate(page, fields)) == 0
    assert fields[0].ok is False and fields[0].note == "请手动填写"
    assert fields[0].filled == ""


def test_locate_gives_up_when_screenshot_fails(tmp_db):
    """控件不可见、截图拿不到：同样回落「请手动填写」，不去打扰模型。"""
    page = _FakePage(shot="")
    fields = [_field(index=0, label="毕业院校")]
    locator = _locator(_profile(school="清华大学"))
    assert asyncio.run(locator.locate(page, fields)) == 0
    assert fields[0].note == "请手动填写"


def test_locate_skips_filled_file_and_choice_fields(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """填过的、上传控件、单选多选都不碰，只看没填上的普通栏位。"""
    page = _FakePage()
    fields = [
        _field(index=0, label="毕业院校", key="school", filled="北京大学"),
        _field(index=1, label="简历附件", type="file"),
        _field(index=2, label="性别", type="radio"),
        _field(index=3, label="期望薪资", type="select-one"),
    ]
    locator = _locator(_profile(expected_salary="20K"))
    seen: list[int] = []

    async def _ask(field: Any, _options: Any, _png: bytes) -> str:
        seen.append(field.index)
        return "expected_salary"

    monkeypatch.setattr(locator, "_ask", _ask)
    assert asyncio.run(locator.locate(page, fields)) == 1
    assert seen == [3] and fields[3].filled == "20K"


def test_locate_caps_attempts(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """一次扫描最多处理 max_fields 个栏位：护栏之一。"""
    page = _FakePage()
    fields = [_field(index=i, label=f"未知栏位{i}") for i in range(5)]
    locator = _locator(_profile(school="清华大学"), max_fields=2)
    tried: list[int] = []

    async def _ask(field: Any, _options: Any, _png: bytes) -> str:
        tried.append(field.index)
        return "无法判断"

    monkeypatch.setattr(locator, "_ask", _ask)
    assert asyncio.run(locator.locate(page, fields)) == 0
    assert tried == [0, 1]


def test_locate_timeout_falls_back(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型迟迟不回或调用报错：回落「请手动填写」，不拖垮整次扫描。"""
    page = _FakePage()
    fields = [_field(index=0, label="毕业院校")]
    locator = _locator(_profile(school="清华大学"), timeout=5)

    async def _ask(_field: Any, _options: Any, _png: bytes) -> str:
        raise TimeoutError("模型没回话")

    monkeypatch.setattr(locator, "_ask", _ask)
    assert asyncio.run(locator.locate(page, fields)) == 0
    assert fields[0].note == "请手动填写"


def test_locate_select_aligns_to_options(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """下拉栏位：值对得上选项才填，对不上就当没认出来。"""
    page = _FakePage()
    fields = [
        _field(
            index=0,
            tag="select",
            type="select-one",
            label="毕业院校",
            options=["北京大学", "清华大学"],
        )
    ]

    async def _ask(_field: Any, _options: Any, _png: bytes) -> str:
        return "school"

    hit = _locator(_profile(school="清华大学"))
    monkeypatch.setattr(hit, "_ask", _ask)
    assert asyncio.run(hit.locate(page, fields)) == 1
    assert fields[0].filled == "清华大学"

    fields[0].key = ""
    fields[0].filled = ""
    fields[0].note = ""
    fields[0].ok = True
    miss = _locator(_profile(school="某某大学"))
    monkeypatch.setattr(miss, "_ask", _ask)
    assert asyncio.run(miss.locate(page, fields)) == 0
    assert fields[0].note == "请手动填写"


def test_locate_prompt_sends_labels_not_values(tmp_db):
    """数据最小化：提示词只带资料键和中文标签，不带任何取值。"""
    locator = _locator(_profile(school="清华大学", phone="13800138000"))
    text = locator._prompt(_field(index=0, label="毕业院校"), locator._options())
    assert "- school：毕业学校" in text
    assert "清华大学" not in text and "13800138000" not in text


def test_locate_config_bundle_defaults_off(client: TestClient):
    assert client.get("/api/config").json()["locate"] == {
        "enabled": False,
        "max_fields": 3,
        "timeout": 15,
    }


def test_locate_config_round_trip(client: TestClient):
    saved = client.put(
        "/api/config/locate", json={"enabled": True, "max_fields": 4, "timeout": 20}
    ).json()
    assert saved == {"enabled": True, "max_fields": 4, "timeout": 20}
    assert client.get("/api/config").json()["locate"]["enabled"] is True


def test_locate_config_clamps_guardrails(client: TestClient):
    """护栏不能靠前端自觉：越界的个数与超时，后端直接夹回安全区间。"""
    saved = client.put(
        "/api/config/locate", json={"enabled": True, "max_fields": 99, "timeout": 1}
    ).json()
    assert saved["max_fields"] == 10 and saved["timeout"] == 5


# --- 安全规则：字段分类 / 数据最小化 / 重复模块倒序 / 上传后复核 ---------------


def test_safety_kind_classifies_keys():
    assert kind("id_card") == SENSITIVE
    assert kind("id_expiry") == SENSITIVE
    assert kind("self_intro") == DECLARATION
    assert kind("height") == OPTIONAL
    assert kind("school") == FACT
    assert kind("phone", source="简历") == DERIVED


def test_safety_sensitive_wins_over_source():
    """敏感事实不因来源是简历就降级：分类以键为准。"""
    assert kind("id_card", source="简历") == SENSITIVE


def test_safety_public_entries_hides_sensitive(tmp_db):
    profile = _profile(school="清华大学", id_card="110101199001011234", self_intro="我是……")
    kept = {entry.key for entry in public_entries(profile.entries)}
    assert "school" in kept and "self_intro" in kept
    assert "id_card" not in kept


def test_safety_split_and_order_by_end_date():
    text = "甲公司 2020.07-2022.06\n\n乙公司 2022.07-至今\n\n丙公司 2018.01-2020.06"
    parts = order_by_end_date(split_entries(text))
    assert [part[:3] for part in parts] == ["乙公司", "甲公司", "丙公司"]


def test_safety_end_date_treats_present_as_latest():
    assert end_date_of("至今") > end_date_of("2024.06")
    assert end_date_of("没有日期") == (0, 0, 0)


def test_profile_entry_carries_kind(tmp_db):
    profile = _profile(school="清华大学", id_card="110101199001011234")
    kinds = {entry.key: entry.kind for entry in profile.entries}
    assert kinds["school"] == FACT and kinds["id_card"] == SENSITIVE
    assert profile.entry("id_card").to_dict()["kind"] == SENSITIVE


def test_matcher_prompt_hides_sensitive_values(tmp_db):
    """数据最小化：身份证号这类敏感事实绝不进大模型的提示词。"""
    profile = _profile(school="清华大学", id_card="110101199001011234")
    text = _matcher(profile)._prompt([_field(index=0, label="毕业院校")])
    assert "清华大学" in text
    assert "id_card" not in text and "110101199001011234" not in text


def test_matcher_prompt_lists_page_options(tmp_db):
    """提示词要把页面上的真实选项带上，模型才有得挑。"""
    profile = _profile(available="一周内到岗")
    field = _field(index=0, label="到岗时间", type="select", options=["一周内", "一个月内"])
    text = _matcher(profile)._prompt([field])
    assert "可选项" in text and "一周内" in text and "一个月内" in text


def test_matcher_prompt_carries_form_outline(tmp_db):
    """有大纲时提示词要带上「表单结构」这一段：同名栏位靠层级才分得清。"""
    profile = _profile(school="示例大学")
    field = _field(index=0, label="开始时间", type="text")
    outline = '- heading "教育经历"\n  - textbox "开始时间" [ref=e5]'
    text = _matcher(profile)._prompt([field], outline=outline)
    assert "表单结构" in text
    assert "教育经历" in text and "[ref=e5]" in text
    # 序号仍以「还没识别的表单字段」那份为准，别让模型拿大纲里的 ref 当序号
    assert "序号以「还没识别的表单字段」那份为准" in text


def test_matcher_prompt_skips_outline_when_blank(tmp_db):
    """没取到大纲（没开 AI / 拿不到可访问性树）时提示词保持原样，不留空壳。"""
    text = _matcher(_profile(school="示例大学"))._prompt([_field(index=0, label="开始时间")])
    assert "表单结构" not in text


def _ai_matcher(profile: ApplyProfile) -> FieldMatcher:
    """配好大模型的匹配器；``_ask`` 由各用例替换，测试不触网。"""
    return FieldMatcher(
        profile, llm=LlmSettings(api_key="k", model="m"), domain="example.com"
    )


def test_matcher_plan_takes_model_option(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型给的是页面上真有的选项时，直接照它填。"""
    profile = _profile(available="随时到岗")
    matcher = _ai_matcher(profile)
    field = _field(
        index=0, label="最快到岗时间", type="select", options=["一周内", "一个月内"]
    )

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str]]:
        return {"0": ("available", "一周内", "")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field]))
    assert field.key == "available" and field.filled == "一周内"
    assert field.source == "AI" and field.ok


def test_matcher_plan_passes_outline_through(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """``match_ai(outline=…)`` 要把大纲一路带到 ``_ask``：它是模型分清同名栏位的唯一线索。"""
    matcher = _ai_matcher(_profile(available="随时到岗"))
    field = _field(index=0, label="最快到岗时间", type="select", options=["一周内"])
    seen: list[str] = []

    async def _ask(_fields: Any, outline: str = "") -> dict[str, tuple[str, str, str]]:
        seen.append(outline)
        return {"0": ("available", "一周内", "")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field], outline='- heading "教育经历"'))
    assert seen == ['- heading "教育经历"']


def test_matcher_plan_keeps_invented_value_for_review(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """资料里没有、模型按依据推出来的值不再直接作废，标成「推算」请用户核对。"""
    profile = _profile(name="张三")
    matcher = _ai_matcher(profile)
    field = _field(index=0, label="出生年月", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str, str]]:
        return {"0": ("birth", "1998-01-01", "按年龄估的")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field]))
    assert field.key == "birth" and field.filled == "1998-01-01"
    assert field.source == "推算" and field.inferred is True


def test_matcher_plan_rejects_value_without_basis(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """一点依据都说不出来的值仍然作废：留空比填错好。"""
    profile = _profile(name="张三")
    matcher = _ai_matcher(profile)
    field = _field(index=0, label="出生年月", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str, str]]:
        return {"0": ("birth", "1998-01-01", "")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field]))
    assert field.key == "" and field.filled == ""


def test_matcher_plan_flags_unmatchable_option(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型和资料都对不上官网选项时，认下这一栏但留空请用户手动选。"""
    profile = _profile(degree="本科")
    matcher = _ai_matcher(profile)
    field = _field(index=0, label="学历", type="select", options=["硕士", "博士"])

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str]]:
        return {"0": ("degree", "本科", "")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai([field]))
    assert field.key == "degree" and field.filled == ""
    assert field.ok is False and "请手动选择" in field.note


# --- 逐栏推理：规则与记忆够不着的栏，一栏一问 --------------------------------
#
# 「网申助手老填不好」的主治：整表出计划时模型顾此失彼，认得前一半就收工。
# 逐栏推理一次只把一栏交给模型，判完就地落地；这一批用例盯的是「只判一栏、
# 认不出就空着、出错只跳过这一栏」这几条边界。


def test_match_one_lands_single_field(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """逐栏推理：模型只判这一栏，判完就地落地，返回可复盘的摘要。"""
    matcher = _ai_matcher(_profile(major="水产养殖学"))
    field = _field(index=0, label="专业类别", type="text")

    async def _ask(fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str, str]]:
        assert [f.index for f in fields] == [0]  # 只把这一栏交出去
        return {"0": ("major_category", "水产类", "专业「水产养殖学」所属的专业类", "")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    summary = asyncio.run(matcher.match_one(field))
    assert field.key == "major_category" and field.filled == "水产类"
    assert field.inferred is True and field.source == "推算"
    assert "专业类别" in summary


def test_match_one_returns_blank_when_unclaimed(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型没认领这一栏：返回空串，字段原样不动。"""
    matcher = _ai_matcher(_profile(name="张三"))
    field = _field(index=0, label="随便填点", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str, str]]:
        return {}

    monkeypatch.setattr(matcher, "_ask", _ask)
    assert asyncio.run(matcher.match_one(field)) == ""
    assert field.key == "" and field.filled == ""


def test_match_one_skips_without_ai_service(tmp_db):
    """未开通 AI 服务：一栏都不判，直接返回空串。"""
    field = _field(index=0, label="民族", type="text")
    assert asyncio.run(_matcher(_profile(name="张三")).match_one(field)) == ""
    assert field.key == "" and field.filled == ""


def test_match_one_survives_model_error(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型调用出错只跳过这一栏，不往外抛。"""
    matcher = _ai_matcher(_profile(name="张三"))
    field = _field(index=0, label="民族", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str, str]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(matcher, "_ask", _ask)
    assert asyncio.run(matcher.match_one(field)) == ""


def test_reason_picks_only_askable_blank_fields(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """逐栏推理只挑「还空着、也值得问」的栏：已对上、文件、隐藏、提交都不问。"""
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    matcher = _ai_matcher(_profile(name="张三"))
    seen: list[int] = []

    async def _match_one(field: FormField, **_kw: Any) -> str:
        seen.append(field.index)
        return ""

    monkeypatch.setattr(matcher, "match_one", _match_one)
    items = [
        _field(index=0, label="姓名", key="name", filled="张三"),  # 已对上，不问
        _field(index=1, label="民族", type="text"),  # 空着，问
        _field(index=2, label="简历上传", type="file"),  # 文件栏，不问
        _field(index=3, label="隐藏项", hidden=True),  # 隐藏，不问
        _field(index=4, label="提交", type="button"),  # 非可问类型，不问
        _field(index=5, label="政治面貌", type="select", options=["群众"]),  # 空着，问
    ]
    landed = asyncio.run(assistant._reason(items, matcher))
    assert sorted(seen) == [1, 5]
    assert landed == 0


def test_reason_counts_landed_and_survives_error(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """认领计数只数真正落地的栏；某一栏抛异常不带走整批。"""
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    matcher = _ai_matcher(_profile(name="张三"))

    async def _match_one(field: FormField, **_kw: Any) -> str:
        if field.index == 1:
            raise RuntimeError("boom")
        return f"栏{field.index}"

    monkeypatch.setattr(matcher, "match_one", _match_one)
    items = [_field(index=i, label=f"栏{i}", type="text") for i in range(3)]
    assert asyncio.run(assistant._reason(items, matcher)) == 2


def test_reason_returns_zero_without_ai(tmp_db):
    """未开通 AI 服务：逐栏推理整批不做，返回 0。"""
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    items = [_field(index=0, label="民族", type="text")]
    assert asyncio.run(assistant._reason(items, _matcher(_profile(name="张三")))) == 0


def test_parse_plan_accepts_both_shapes():
    """兼容两种返回：新版的 {key, value, basis, same_as} 与旧版的纯键字符串。"""
    plan = parse_plan(
        {
            "3": "school",
            "5": {"key": "major", "value": "计算机", "basis": "资料里专业是计算机科学与技术"},
            "7": {"value": "只有值没有键"},
            "9": 123,
            "11": {"key": "degree_name", "value": "学士", "sameAs": "degree"},
        }
    )
    assert plan == {
        "3": ("school", "", "", ""),
        "5": ("major", "计算机", "资料里专业是计算机科学与技术", ""),
        "11": ("degree_name", "学士", "", "degree"),
    }
    assert parse_plan(None) == {} and parse_plan([1, 2]) == {}


def test_parse_drop_indexes_accepts_every_shape():
    """模型回 ``{"drop": [...]}`` / 裸列表 / ``{"3": true}`` 三种写法都照收。"""
    assert parse_drop_indexes({"drop": [3, 7]}) == {3, 7}
    assert parse_drop_indexes([3, "7"]) == {3, 7}
    assert parse_drop_indexes({"3": True, "5": False}) == {3}
    assert parse_drop_indexes({"drop": []}) == set()
    # 认不出的条目直接丢，别让一条脏数据把整批学习卡住
    assert parse_drop_indexes({"drop": ["甲", None, 2.7]}) == {2}
    assert parse_drop_indexes(None) == set() and parse_drop_indexes("3") == set()


def test_parse_merge_keys_accepts_every_shape():
    """模型回 ``{"merge": {...}}`` 时收下；没给 merge、序号或名字认不出，就丢。"""
    assert parse_merge_keys({"merge": {"3": "专业类别"}}) == {3: "专业类别"}
    assert parse_merge_keys({"merge": {3: " 民族 "}}) == {3: "民族"}
    # 序号认不出、名字是空串的条目都丢，别让一条脏数据把整批归纳卡住
    assert parse_merge_keys({"merge": {"x": "甲", "2": ""}}) == {}
    assert parse_merge_keys({"drop": [0]}) == {}
    assert parse_merge_keys(["3"]) == {} and parse_merge_keys(None) == {}


def test_tidy_learned_drops_rows_the_model_flagged(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """AI 整理：模型点名「不是一条资料」的条目剔掉，其余的照旧学。"""
    matcher = _ai_matcher(_profile(name="张三"))
    rows = [
        {"key": "submit", "label": "确认投递", "value": "确认投递"},
        {"key": "nation", "label": "民族", "value": "汉族"},
        {"key": "placeholder", "label": "请选择", "value": "请选择"},
    ]
    seen: list[str] = []

    class _Run:
        output = '{"drop": [0, 2]}'

    async def _ask(listing: str) -> Any:
        seen.append(listing)
        return _Run()

    monkeypatch.setattr(matcher, "_ask_tidy", _ask)
    kept = asyncio.run(matcher.tidy_learned(rows))
    assert kept == [{"key": "nation", "label": "民族", "value": "汉族"}]
    # 送进去的是「栏位 + 学到的值」，模型才判得出哪条不是资料
    assert seen and "民族" in seen[0] and "确认投递" in seen[0]


def test_tidy_learned_keeps_rows_when_ai_is_off(tmp_db):
    """没开通 AI 服务就原样返回：整理只是加分项，不该让学习本身失败。"""
    rows = [{"key": "submit", "label": "确认投递", "value": "确认投递"}]
    assert asyncio.run(_matcher(_profile()).tidy_learned(rows)) == rows


def test_tidy_learned_merges_synonyms_into_one_key(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """AI 归纳：同一件事的不同叫法并到一个规范键上，库里别存成两条。"""
    matcher = _ai_matcher(_profile(name="张三"))
    rows = [
        {"key": "major_category", "label": "专业类别", "value": "计算机类"},
        {"key": "专业类", "label": "专业类", "value": "计算机类"},
        {"key": "nation", "label": "民族", "value": "汉族"},
    ]

    class _Run:
        output = '{"drop": [], "merge": {"1": "专业类别"}}'

    async def _ask(_listing: str) -> Any:
        return _Run()

    monkeypatch.setattr(matcher, "_ask_tidy", _ask)
    kept = asyncio.run(matcher.tidy_learned(rows))
    # 第 1 条归到规范键 major_category，与第 0 条撞名，只留一条
    assert [row["key"] for row in kept] == ["major_category", "nation"]


def test_tidy_learned_keeps_rows_when_model_reply_is_unusable(
    tmp_db, monkeypatch: pytest.MonkeyPatch
):
    """模型回话解析不出来就整批保留：宁多留一条，也别把学到的东西删没了。"""
    matcher = _ai_matcher(_profile(name="张三"))
    rows = [{"key": "nation", "label": "民族", "value": "汉族"}]

    class _Run:
        output = "这不是 JSON"

    async def _ask(_listing: str) -> Any:
        return _Run()

    monkeypatch.setattr(matcher, "_ask_tidy", _ask)
    assert asyncio.run(matcher.tidy_learned(rows)) == rows


def test_matcher_spreads_repeated_blocks_newest_first(tmp_db):
    """一栏出现多次时，多段经历按结束日期倒序，最近的填在最前面。"""
    profile = _profile(work_experience="甲公司 2020.07-2022.06\n\n乙公司 2022.07-至今")
    fields = [
        _field(index=0, label="工作经历", key="work_experience", filled="甲公司 2020.07-2022.06"),
        _field(index=1, label="工作经历", key="work_experience", filled="甲公司 2020.07-2022.06"),
    ]
    _matcher(profile)._spread_repeats(fields)
    assert fields[0].filled.startswith("乙公司")
    assert fields[1].filled.startswith("甲公司")


def test_matcher_keeps_single_block_as_is(tmp_db):
    """资料只有一段时原样不动：别把一整段正文塞进第一栏、后面留空。"""
    profile = _profile(work_experience="一段经历")
    fields = [
        _field(index=0, label="工作经历", key="work_experience", filled="一段经历"),
        _field(index=1, label="工作经历", key="work_experience", filled="一段经历"),
    ]
    _matcher(profile)._spread_repeats(fields)
    assert [f.filled for f in fields] == ["一段经历", "一段经历"]


def test_filler_skips_irreversible_controls(tmp_db):
    """提交按钮永远留给人点：扫到也直接跳过，不产生任何点击。"""
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, type="submit", key="submit", filled="提交")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.skipped == 1 and outcome.filled == 0
    assert page.calls == []
    assert fields[0].ok is False and "手动" in fields[0].note


def test_filler_restores_fields_overwritten_by_upload(tmp_db, tmp_path: Path):
    """上传简历后官网覆盖了已填的栏，复核时按原值补回来。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(
        selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]'},
        overwrite_on_upload={'[data-gj-idx="0"]': "被简历覆盖的名字"},
    )
    fields = [
        _field(index=0, key="name", filled="张三"),
        _field(index=1, type="file", key="resume", filled="简历"),
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 2 and outcome.restored == 1
    assert page.frame.written['[data-gj-idx="0"]'] == "张三"
    assert page.calls.count(("fill", '[data-gj-idx="0"]', "张三")) == 2


def test_filler_restore_leaves_untouched_fields_alone(tmp_db, tmp_path: Path):
    """官网没动过的栏不该被重写一遍。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]'})
    fields = [
        _field(index=0, key="name", filled="张三"),
        _field(index=1, type="file", key="resume", filled="简历"),
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.restored == 0
    assert page.calls.count(("fill", '[data-gj-idx="0"]', "张三")) == 1


# --- 记忆策略：标签 → 键，手动优先，改版重判 ---------------------------------


def test_mapping_rows_for_domain(tmp_db):
    ApplyMappingRow.remember(
        "a.com", [{"signature": "x|text", "label": "甲", "value": "1", "source": "name"}]
    )
    ApplyMappingRow.remember(
        "b.com", [{"signature": "y|text", "label": "乙", "value": "2", "source": "name"}]
    )
    assert [row.label for row in ApplyMappingRow.rows_for_domain("a.com")] == ["甲"]
    assert ApplyMappingRow.rows_for_domain("") == []


def test_matcher_reuses_manual_memory_by_label_across_markup_change(tmp_db):
    """站点改版换了 name / id、栏名没变：靠归一化标签沿用用户手动改过的那条。"""
    ApplyMappingRow.remember(
        "example.com",
        [
            {
                "signature": "old-widget|text",
                "label": "姓名",
                "value": "李四",
                "source": "name",
                "manual": True,
            }
        ],
    )
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0)
    asyncio.run(_matcher(_profile(name="张三"), "example.com").match([field]))
    assert field.filled == "李四" and field.key == "name"
    assert field.source == "手动" and field.confidence == 1.0


def test_matcher_manual_memory_beats_keyword_rule(tmp_db):
    """手动改过优先：关键词规则命中也要让位于用户上次的改动。"""
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0)
    ApplyMappingRow.remember(
        "example.com",
        [
            {
                "signature": field.signature(),
                "label": "姓名",
                "value": "李四",
                "source": "name",
                "manual": True,
            }
        ],
    )
    asyncio.run(_matcher(_profile(name="张三"), "example.com").match([field]))
    assert field.filled == "李四" and field.source == "手动"


def test_matcher_remember_keeps_manual_flag(tmp_db):
    """用户手动改过的行，下次记住时仍标记为手动。"""
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0)
    field.key, field.filled, field.source = "name", "李四", "手动"
    matcher = _matcher(_profile(name="张三"), "example.com")
    assert matcher.remember([field]) == 1
    stored = ApplyMappingRow.lookup("example.com", [field.signature()])
    assert stored[field.signature()].manual is True


def test_matcher_auto_memory_is_not_manual(tmp_db):
    """规则自动填的行不该被记成手动，免得把一次自动结果锁死。"""
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0)
    matcher = _matcher(_profile(name="张三"), "example.com")
    asyncio.run(matcher.match([field]))
    assert matcher.remember([field]) == 1
    stored = ApplyMappingRow.lookup("example.com", [field.signature()])
    assert stored[field.signature()].manual is False


def test_filler_gives_up_on_a_stuck_field_and_moves_on(tmp_db, monkeypatch):
    """某一栏卡死时：按上限认输、标「请手动填写」，后面的栏照填，不整批停摆。

    真实站点上「点一下就没反应」的控件很常见（浮层不收起、选项永远不刷新）。
    没有这层上限时整张表会停在这一格上，用户看到的就是「点了没反应」。
    """
    monkeypatch.setattr(filler, "_FIELD_TIMEOUT", 0.05)
    visited: list[int] = []

    async def _stuck(self: FormFiller, page: Any, item: FormField, group: list[FormField]) -> None:
        visited.append(item.index)
        await asyncio.sleep(30)

    monkeypatch.setattr(FormFiller, "_apply", _stuck)
    fields = [
        _field(index=0, key="name", filled="张三"),
        _field(index=1, key="phone", filled="13800000000"),
    ]
    outcome = asyncio.run(FormFiller(None).fill(_FakePage(), fields))
    assert visited == [0, 1]  # 第一栏超时没有拖住第二栏
    assert outcome.failed == 2
    assert outcome.filled == 0
    assert [item.note for item in fields] == ["这一栏填得太久，先跳过，请手动填写"] * 2
    assert all(item.ok is False for item in fields)


# --- 网申状态机：入口路由 / 断点续跑 / 人工确认 --------------------------------


def _walk(assistant: ApplyAssistant, state: pipeline.ApplyState) -> pipeline.ApplyState:
    """从入口进图跑一遍，返回停在哪儿的状态（含走过的节点）。"""
    return asyncio.run(pipeline.run(assistant, state))


def _apply_page() -> _FakePage:
    """一张只有「姓名」一栏的企业官网表单，够跑通整条链。"""
    return _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )


def test_pipeline_entry_routes_every_stage(tmp_db):
    """五个阶段各走各的支路：从哪一关进图，就只跑这一关往后的事。"""
    ApplyProfileSettings(name="张三").save()
    uid = _seed_job("j1")
    assistant = ApplyAssistant(_FakeSession(_apply_page()))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))

    # 待扫描：整条链从头跑
    fresh = pipeline.ApplyState(uid=uid, navigate=False)
    walked = _walk(assistant, fresh)
    assert walked.trace == ["entry:pending", "open", "scan", "plan", "confirm"]
    assert walked.stage == pipeline.STAGE_AWAITING_CONFIRM

    # 已扫描：跳过开页与扫描，直接做慢活
    scanned = pipeline.ApplyState(uid=uid, stage=pipeline.STAGE_SCANNED, navigate=False)
    walked = _walk(assistant, scanned)
    assert walked.trace == ["entry:scanned", "plan", "confirm"]
    assert assistant.session.page.goto_calls == ["https://acme.cn/apply"]  # 没重新导航

    # 待确认：只把状态钉住，什么都不做
    waiting = pipeline.ApplyState(uid=uid, stage=pipeline.STAGE_AWAITING_CONFIRM, navigate=False)
    walked = _walk(assistant, waiting)
    assert walked.trace == ["entry:awaiting_confirm", "confirm"]
    assert pipeline.load_stage(uid) == pipeline.STAGE_AWAITING_CONFIRM

    # 已确认：只填表
    confirmed = pipeline.ApplyState(
        uid=uid,
        stage=pipeline.STAGE_CONFIRMED,
        navigate=False,
        overrides=[{"index": 0, "key": "name", "filled": "李四"}],
    )
    walked = _walk(assistant, confirmed)
    assert walked.trace == ["entry:confirmed", "fill"]
    assert walked.outcome["outcome"]["filled"] == 1

    # 已填表：跑到头了，什么都不做
    done = pipeline.ApplyState(uid=uid, stage=pipeline.STAGE_FILLED, navigate=False)
    walked = _walk(assistant, done)
    assert walked.trace == ["entry:filled"] and walked.outcome == {}


def test_pipeline_defer_stops_after_scan_and_defers_slow_work(tmp_db, monkeypatch):
    """``defer`` 时面板先拿对照表：快活做完就出图，验证码与 AI 兜底挂后台补。"""
    ApplyProfileSettings(name="张三").save()
    uid = _seed_job("j1")
    assistant = ApplyAssistant(_FakeSession(_apply_page()))
    finished: list[tuple[str, str]] = []

    async def _finish(_page: Any, task_uid: str, gate: str) -> None:
        finished.append((task_uid, gate))

    spawned: list[Any] = []
    monkeypatch.setattr(assistant, "_finish", _finish)
    monkeypatch.setattr(assistant, "_spawn", lambda coro: spawned.append(coro))

    result = asyncio.run(assistant.open_task(uid, "https://acme.cn/apply", defer=True))
    assert result["pending"] is True and result["gate"] == ""
    assert result["task"]["fields"][0]["filled"] == "张三"
    assert pipeline.load_stage(uid) == pipeline.STAGE_SCANNED
    assert len(spawned) == 1

    asyncio.run(spawned[0])  # 后台补全跑完：断点推到「待确认」
    assert finished == [(uid, "")]
    assert pipeline.load_stage(uid) == pipeline.STAGE_AWAITING_CONFIRM


def test_pipeline_resumes_from_checkpoint_after_restart(tmp_db, monkeypatch):
    """进程被关掉后重开：从断点那一关接着跑，不重开页面也不重扫。"""
    ApplyProfileSettings(name="张三").save()
    uid = _seed_job("j1")
    first = ApplyAssistant(_FakeSession(_apply_page()))
    monkeypatch.setattr(first, "_spawn", lambda coro: coro.close())
    asyncio.run(first.open_task(uid, "https://acme.cn/apply", defer=True))
    assert pipeline.load_stage(uid) == pipeline.STAGE_SCANNED

    # 换一个助手实例 = 进程重开；页面缓存没了，但断点还在
    page = _apply_page()
    again = ApplyAssistant(_FakeSession(page))
    state = pipeline.ApplyState(uid=uid, stage=pipeline.load_stage(uid), navigate=False)
    walked = _walk(again, state)
    assert walked.trace == ["entry:scanned", "plan", "confirm"]
    assert page.goto_calls == []  # 续跑不重新导航
    assert pipeline.load_stage(uid) == pipeline.STAGE_AWAITING_CONFIRM


def test_pipeline_fill_marks_checkpoint_and_delete_clears_it(tmp_db):
    """填完记一笔「已填表」；任务删掉后断点也跟着清干净。"""
    ApplyProfileSettings(name="张三").save()
    uid = _seed_job("j1")
    assistant = ApplyAssistant(_FakeSession(_apply_page()))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    assert asyncio.run(assistant.confirm(uid))["stageLabel"] == "已确认"
    result = asyncio.run(assistant.fill(uid, [{"index": 0, "key": "name", "filled": "李四"}]))
    assert result["outcome"]["filled"] == 1
    assert pipeline.load_stage(uid) == pipeline.STAGE_FILLED

    assert asyncio.run(assistant.delete(uid)) is True
    assert pipeline.load_stage(uid) == ""


def test_pipeline_stage_of_reads_the_task_snapshot(tmp_db):
    """阶段由「状态 + 对照表」现算，不额外存一列，省得两处数据打架。"""
    assert pipeline.stage_of({"status": "submitted"}) == pipeline.STAGE_FILLED
    assert pipeline.stage_of({"status": "filled"}) == pipeline.STAGE_FILLED
    assert pipeline.stage_of({"status": "pending", "fields": [{"label": "姓名"}]}) == (
        pipeline.STAGE_AWAITING_CONFIRM
    )
    assert pipeline.stage_of({"status": "pending", "fields": []}) == pipeline.STAGE_PENDING
    assert pipeline.stage_of({}) == pipeline.STAGE_PENDING
    assert pipeline.stage_label(pipeline.STAGE_PENDING) == "待扫描"
    assert pipeline.stage_label("没见过的阶段") == "没见过的阶段"


def test_pipeline_checkpoint_is_per_task(tmp_db):
    """断点按任务分开存：一条任务的进度不会盖掉另一条。"""
    pipeline.save_stage("apply:a", pipeline.STAGE_CONFIRMED)
    pipeline.save_stage("apply:b", pipeline.STAGE_SCANNED)
    assert pipeline.load_stage("apply:a") == pipeline.STAGE_CONFIRMED
    assert pipeline.load_stage("apply:b") == pipeline.STAGE_SCANNED
    assert pipeline.load_stage("apply:c") == ""
    pipeline.clear_stage("apply:a")
    assert pipeline.load_stage("apply:a") == ""
    assert pipeline.load_stage("apply:b") == pipeline.STAGE_SCANNED


def test_apply_task_dict_carries_stage(client: TestClient):
    """任务快照带上阶段：前端不用自己猜这张表扫没扫过。"""
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://acme.cn/apply")
    assert client.get("/api/apply/tasks").json()[0]["stageLabel"] == "待扫描"
    ApplicationRow.save_fields(uid, [{"label": "姓名", "key": "name", "filled": "张三"}])
    task = client.get("/api/apply/tasks").json()[0]
    assert task["stage"] == pipeline.STAGE_AWAITING_CONFIRM and task["stageLabel"] == "待确认"


def test_apply_confirm_endpoint_pushes_the_task_one_stage(client: TestClient):
    """「确认清单」只把任务往前推一关，不动页面。"""
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://acme.cn/apply")
    saved = client.post(f"/api/apply/task/{uid}/confirm")
    assert saved.status_code == 200 and saved.json()["stageLabel"] == "已确认"
    assert pipeline.load_stage(uid) == pipeline.STAGE_CONFIRMED
    assert client.post("/api/apply/task/nope/confirm").status_code == 400


class _LogCapture(logging.Handler):
    """把日志行收进列表：统一日志器把 propagate 关了，pytest 的 caplog 收不到。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _capture_apply_logs() -> tuple[logging.Logger, _LogCapture]:
    logger = logging.getLogger("jobcourier.apply")
    handler = _LogCapture()
    logger.addHandler(handler)
    return logger, handler


def test_matcher_logs_ai_plan_for_replay(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """每次模型判断都留一行可复盘摘要：认领几栏、落地几栏、各栏落到哪个键。"""
    matcher = _ai_matcher(_profile(available="随时到岗"))
    field = _field(index=0, label="最快到岗时间", type="select", options=["一周内", "一个月内"])

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str]]:
        return {"0": ("available", "一周内", "")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    logger, handler = _capture_apply_logs()
    try:
        asyncio.run(matcher.match_ai([field]))
    finally:
        logger.removeHandler(handler)
    line = next((t for t in handler.lines if t.startswith("AI 填表计划：")), "")
    assert line, handler.lines
    assert "落地 1 栏" in line and "available" in line


def test_matcher_logs_ai_failure_for_replay(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型调用没跑通时也要留痕，事后能复盘是哪一次判断、失败原因是什么。"""
    matcher = _ai_matcher(_profile(name="张三"))
    field = _field(index=0, label="出生年月", type="text")

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str]]:
        raise RuntimeError("模型超时")

    monkeypatch.setattr(matcher, "_ask", _ask)
    logger, handler = _capture_apply_logs()
    try:
        asyncio.run(matcher.match_ai([field]))
    finally:
        logger.removeHandler(handler)
    line = next((t for t in handler.lines if t.startswith("AI 填表计划未跑通")), "")
    assert line, handler.lines
    assert "RuntimeError" in line and "模型超时" in line


# --- 表单视觉核对：编号画框 + 多模态核对「多没多、少没少」 ---------------------


def _vision(**kw: Any) -> VisionChecker:
    """开着视觉核对的检查器；大模型只是摆设，``_ask`` 由各用例替换。"""
    return VisionChecker(
        llm=LlmSettings(api_key="k", model="m"),
        settings=ApplyVisionSettings(enabled=True, **kw),
    )


def test_vision_settings_round_trip(tmp_db):
    """护栏（开关 / 模型 / 超时 / 分片数 / 打码）要能存下来、读回来，默认是开的。"""
    ApplyVisionSettings(
        enabled=True, model="vlm", timeout=60, max_tiles=6, mask_sensitive=False
    ).save()
    loaded = ApplyVisionSettings.load()
    assert (loaded.enabled, loaded.model, loaded.timeout, loaded.max_tiles) == (
        True,
        "vlm",
        60,
        6,
    )
    assert loaded.mask_sensitive is False
    ApplyVisionSettings().save()
    assert ApplyVisionSettings.load().enabled is True
    assert ApplyVisionSettings.load().timeout == 40


def test_vision_needs_model_to_run(tmp_db):
    """没配大模型就没有视觉核对可言；开关关掉也一样。"""
    off = VisionChecker(llm=LlmSettings(), settings=ApplyVisionSettings(enabled=True))
    assert off.enabled is False
    shut = VisionChecker(
        llm=LlmSettings(api_key="k", model="m"), settings=ApplyVisionSettings(enabled=False)
    )
    assert shut.enabled is False
    assert shut.check.__self__ is shut  # 方法还在，只是没人会调


def test_vision_check_disabled_touches_nothing(tmp_db):
    """没开功能时连页面都不碰：不截图、不改对照表。"""
    page = _FakePage()
    fields = [_field(index=0, label="姓名")]
    checker = VisionChecker(llm=LlmSettings(), settings=ApplyVisionSettings())
    result = asyncio.run(checker.check(page, fields))
    assert result.checked is False and result.added == []
    assert page.shots == 0 and page.calls == [] and fields[0].hidden is False


def test_vision_parse_takes_json_out_of_chat(tmp_db):
    """模型爱把 JSON 包在解释或代码块里，也要能抠出来；胡话一律当没看见。"""
    raw = '好的，核对如下：\n```json\n{"not_field": [2, "3"], "missing": [{"label": "服从调剂"}]}\n```'
    assert VisionChecker._parse(raw) == ([2, 3], ["服从调剂"])
    assert VisionChecker._parse("") == ([], [])
    assert VisionChecker._parse("模型今天不想干活") == ([], [])
    assert VisionChecker._parse("{不是 JSON}") == ([], [])
    assert VisionChecker._parse('{"missing": "服从调剂"}') == ([], [])
    # 报重了只留一条，编号写成一堆别的字也不影响
    assert VisionChecker._parse('{"not_field": [1, "编号2"], "missing": ["服从调剂", "服从调剂"]}') == (
        [1],
        ["服从调剂"],
    )


def test_vision_candidates_skip_ghosts_buttons_and_unmarked(tmp_db):
    """能画框的只有「还没收起、不是提交按钮、带控件标记」的栏位。"""
    fields = [
        _field(index=0, label="姓名"),
        _field(index=1, label="已收起", hidden=True),
        _field(index=2, label="提交", type="submit"),
        _field(index=3, label="没有标记", selector=""),
    ]
    assert [item.index for item in VisionChecker._candidates(fields)] == [0]


def test_vision_tiles_focus_on_candidate_span(tmp_db):
    """分片优先只切候选栏位所在那一段，页头大横幅不白占几屏。"""
    checker = _vision(max_tiles=4)
    single = [_field(index=0, box=[10.0, 1200.0, 200.0, 30.0])]
    assert checker._tiles({"h": 4000, "vh": 720}, single) == [1160]
    # 候选栏位跨了三屏：按上限切片，不会把几十屏都甩给模型
    tall = [
        _field(index=0, box=[0.0, 0.0, 100.0, 20.0]),
        _field(index=1, box=[0.0, 3000.0, 100.0, 20.0]),
    ]
    assert len(checker._tiles({"h": 4000, "vh": 720}, tall)) == 4
    # 算不出位置（整张表都在 iframe 里）：退回整页切，也不超过上限
    assert len(checker._tiles({"h": 5000, "vh": 720}, [_field(index=0)])) == 4
    # 页面尺寸读不到：按「一屏」处理，不滚也不崩
    assert checker._tiles({}, [_field(index=0)]) == [0]


def test_vision_masks_prefilled_values(tmp_db):
    """登录后的招聘系统常把姓名、手机号预填在表里：带值的一律涂黑再截图。"""
    checker = _vision()
    assert checker._mask(_field(index=0, value="张三")) is True
    assert checker._mask(_field(index=1, value="   ")) is False
    assert _vision(mask_sensitive=False)._mask(_field(index=0, value="张三")) is False


def test_vision_merge_hides_ghosts_and_adds_missing(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """多出来的编号收起、漏掉的补一行；页面里找不回来的落成「请手动填写」。"""
    checker = _vision()
    fields = [_field(index=0, label="姓名"), _field(index=1, label="公司介绍")]

    async def _resolve(_page: Any, text: str, *, marker: str) -> Any:
        if text == "服从调剂":
            return FormField(index=0, label="服从调剂", selector=f'[data-gj-idx="{marker}"]')
        return None

    monkeypatch.setattr(checker, "_resolve", _resolve)
    numbered = VisionChecker._candidates(fields)
    result = asyncio.run(checker._merge(_FakePage(), fields, numbered, [2], ["服从调剂", "身高"]))
    assert result.checked is True and result.ghosts == 1
    assert fields[1].hidden is True and "不用填写" in fields[1].note
    assert [item.label for item in result.added] == ["服从调剂", "身高"]
    # 页面里找回来的那栏接着能定位；找不回来的落成手动填写的提示行
    assert fields[2].selector == '[data-gj-idx="v2"]' and fields[2].ok is True
    assert fields[3].selector == "" and fields[3].ok is False
    assert "手动填写" in fields[3].note
    assert [item.index for item in fields] == [0, 1, 2, 3]
    # 编号越界（模型乱报）不该把别的行搞坏
    assert asyncio.run(checker._merge(_FakePage(), fields, numbered, [99], [])) is not None
    assert [item.hidden for item in fields] == [False, True, False, False]


def test_vision_check_end_to_end_merges(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """完整走一遍：画框截图 → 问模型 → 收起多余、补上漏掉的。"""
    page = _FakePage(
        size={"h": 900, "vh": 900},
        found={
            "是否服从调剂": {
                "idx": "v2",
                "tag": "select",
                "type": "select",
                "labelText": "是否服从调剂",
                "options": ["是", "否"],
            }
        },
    )
    fields = [_field(index=0, label="姓名"), _field(index=1, label="公司介绍")]
    checker = _vision()
    asked: list[int] = []

    async def _ask(shots: Any, numbered: Any) -> str:
        asked.append(len(shots))
        return '{"not_field": [2], "missing": [{"label": "是否服从调剂"}]}'

    monkeypatch.setattr(checker, "_ask", _ask)
    result = asyncio.run(checker.check(page, fields))
    assert asked == [1]
    assert page.shots == 1
    assert result.checked is True and result.ghosts == 1
    assert fields[1].hidden is True
    assert len(fields) == 3 and fields[2].label == "是否服从调剂"
    assert fields[2].options == ["是", "否"]
    assert "收起 1 栏，补上 1 栏" in result.message
    # 标记必须摘干净：页面上不留痕
    assert any("data-gj-vision" in src for src in page.frame.marks)


def test_vision_check_falls_back_when_model_says_nothing(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型说看不清、或者干脆没回话：沿用普通扫描的结果，不硬改对照表。"""
    page = _FakePage(size={"h": 900, "vh": 900})
    fields = [_field(index=0, label="姓名")]
    checker = _vision()

    async def _ask(_shots: Any, _numbered: Any) -> str:
        return ""

    monkeypatch.setattr(checker, "_ask", _ask)
    result = asyncio.run(checker.check(page, fields))
    assert result.checked is False and result.added == []
    assert "沿用普通扫描" in result.message
    assert fields[0].hidden is False


def test_vision_check_survives_broken_page(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """截图失败、模型调用超时：都只记一笔，扫描照常往下走。"""
    page = _FakePage(size={"h": 900, "vh": 900})
    fields = [_field(index=0, label="姓名")]
    checker = _vision(timeout=5)

    async def _boom(*_args: Any, **_kw: Any) -> bytes:
        raise PlaywrightError("截图失败：页面已关闭")

    monkeypatch.setattr(page, "screenshot", _boom)
    result = asyncio.run(checker.check(page, fields))
    assert result.checked is False and "截图失败" in result.message

    async def _slow(_shots: Any, _numbered: Any) -> str:
        raise asyncio.TimeoutError

    monkeypatch.setattr(page, "screenshot", lambda **kw: _png())
    monkeypatch.setattr(checker, "_ask", _slow)
    result = asyncio.run(checker.check(page, fields))
    assert result.checked is False and "视觉核对失败" in result.message


async def _png() -> bytes:
    return b"png"


def test_scan_runs_vision_and_keeps_added_rows(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """扫描时接上视觉核对：它补进来的栏要落进对照表，原流程不受影响。"""
    uid = _seed_job("j1")
    ApplyProfileSettings(name="张三").save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    seen: list[int] = []

    class _Checker:
        enabled = True

        def __init__(self, *args: Any, **kw: Any) -> None:
            pass

        async def check(self, _page: Any, fields: list[FormField]) -> VisionResult:
            seen.append(len(fields))
            extra = FormField(
                index=len(fields), label="是否服从调剂", selector='[data-gj-idx="v1"]'
            )
            fields.append(extra)
            return VisionResult(checked=True, added=[extra], message="视觉核对：补上 1 栏")

    monkeypatch.setattr("job.apply.assistant.VisionChecker", _Checker)
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    assert seen == [1]
    rows = ApplicationRow.get_dict(uid)["fields"]
    assert [row["label"] for row in rows] == ["姓名", "是否服从调剂"]
    assert rows[0]["filled"] == "张三"


def test_finish_keeps_vision_added_rows(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """后台补全：视觉核对补进来的新栏也要落回对照表，别在合并时被丢掉。"""
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://acme.cn/apply")
    ApplicationRow.save_fields(
        uid,
        [
            {
                "index": 0,
                "label": "姓名",
                "key": "name",
                "filled": "张三",
                "source": "手动",
                "confidence": 0.9,
                "selector": '[data-gj-idx="0"]',
            }
        ],
    )
    # 页面上「姓名」那一栏还在：重扫能重新认出它，用户在面板上改的值才谈得上「盖回来」
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "姓名", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
    )
    assistant = ApplyAssistant(_FakeSession(page))

    class _Checker:
        enabled = True

        def __init__(self, *args: Any, **kw: Any) -> None:
            pass

        async def check(self, _page: Any, fields: list[FormField]) -> VisionResult:
            extra = FormField(index=1, label="是否服从调剂", selector='[data-gj-idx="v1"]')
            fields.append(extra)
            return VisionResult(checked=True, added=[extra])

    monkeypatch.setattr("job.apply.assistant.VisionChecker", _Checker)
    asyncio.run(assistant._finish(page, uid, ""))
    rows = ApplicationRow.get_dict(uid)["fields"]
    assert [row["label"] for row in rows] == ["姓名", "是否服从调剂"]
    # 用户在面板上改过的值不能被合并盖掉
    assert rows[0]["filled"] == "张三" and rows[0]["key"] == "name"


def test_vision_config_endpoints(client: TestClient):
    """配置中心：默认开启、参数夹在安全区间里，读回来的是夹过的值。"""
    bundle = client.get("/api/config").json()
    assert bundle["vision"]["enabled"] is True
    assert bundle["vision"]["max_tiles"] == 4 and bundle["vision"]["mask_sensitive"] is True
    saved = client.put(
        "/api/config/vision",
        json={"enabled": False, "model": " vlm ", "timeout": 999, "max_tiles": 0},
    ).json()
    assert saved["enabled"] is False and saved["model"] == "vlm"
    assert saved["timeout"] == 120 and saved["max_tiles"] == 1
    assert client.get("/api/config").json()["vision"]["model"] == "vlm"


# --- 网申填表：交付清单 / 上传与学习开关 ---------------------------------------


def test_auto_settings_defaults(tmp_db):
    """没配过就是「中档、自动传文件」，不会一上来就自己跑。"""
    settings = ApplyAutoSettings.load()
    assert settings.handoff == "balanced" and settings.upload_files is True


def test_auto_config_endpoints(client: TestClient):
    """配置中心：交付口径只认三个枚举，乱写的值退回中档。"""
    bundle = client.get("/api/config").json()
    assert bundle["auto"]["handoff"] == "balanced"
    assert bundle["auto"]["upload_files"] is True
    saved = client.put(
        "/api/config/auto",
        json={"handoff": " 乱写 ", "upload_files": False},
    ).json()
    assert saved["handoff"] == "balanced" and saved["upload_files"] is False
    strict = client.put(
        "/api/config/auto",
        json={"handoff": "strict", "upload_files": True},
    ).json()
    assert strict["handoff"] == "strict"


def test_handoff_splits_gaps_unsure_and_manual(tmp_db):
    """交付清单按口径分堆：必填空栏算缺口、AI 兜底算待复核、认不出的算待手动。"""
    rows = [
        {"index": 0, "label": "姓名", "key": "name", "filled": "张三", "required": True},
        {"index": 1, "label": "民族", "key": "ethnicity", "filled": "", "required": True},
        {"index": 2, "label": "身高", "key": "height", "filled": "175", "source": "AI"},
        {"index": 3, "label": "奇怪的一栏", "key": "", "filled": "", "required": True},
        {"index": 4, "label": "爱好", "key": "", "filled": "", "required": False},
        {"index": 5, "label": "影子", "key": "x", "filled": "", "hidden": True},
    ]
    loose = ApplyAssistant._handoff(rows, "loose")
    assert [item["key"] for item in loose["gaps"]] == ["ethnicity"]
    assert loose["unsure"] == [] and [item["label"] for item in loose["manual"]] == ["奇怪的一栏"]

    balanced = ApplyAssistant._handoff(rows, "balanced")
    assert [item["key"] for item in balanced["gaps"]] == ["ethnicity"]
    assert [item["label"] for item in balanced["unsure"]] == ["身高"]

    strict = ApplyAssistant._handoff(rows, "strict")
    assert [item["key"] for item in strict["gaps"]] == ["ethnicity"]
    assert [item["label"] for item in strict["manual"]] == ["奇怪的一栏", "爱好"]


def test_handoff_items_carry_type_and_required_for_the_panel(tmp_db):
    """清单条目带上控件类型与是否必填，面板才能标「必填」并挑对图标。"""
    rows = [
        {"index": 0, "label": "民族", "key": "ethnicity", "filled": "", "type": "select", "required": True},
        {"index": 1, "label": "奇怪的一栏", "key": "", "filled": "", "type": "text", "required": False},
    ]
    strict = ApplyAssistant._handoff(rows, "strict")
    assert strict["gaps"] == [
        {"index": 0, "label": "民族", "key": "ethnicity", "type": "select", "required": True}
    ]
    assert strict["manual"] == [
        {"index": 1, "label": "奇怪的一栏", "key": "", "type": "text", "required": False}
    ]


def test_deliver_recomputes_handoff_after_a_rescan(tmp_db):
    """重扫后交付清单跟着对照表重算：资料补上那一栏，就不该再挂在「缺口」里。"""
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://acme.cn/apply")
    ApplicationRow.set_status(uid, "filled", note="先前的说明")
    assistant = ApplyAssistant(_FakeSession(_apply_page()))

    gap = [{"index": 0, "label": "民族", "key": "ethnicity", "filled": "", "required": True}]
    assistant._deliver(uid, gap)
    saved = ApplicationRow.get_dict(uid)
    assert [item["key"] for item in saved["handoff"]["gaps"]] == ["ethnicity"]
    assert saved["note"] == "先前的说明"  # 不传 note 就保留旧备注

    filled = [{"index": 0, "label": "民族", "key": "ethnicity", "filled": "汉族", "required": True}]
    assistant._deliver(uid, filled)
    assert ApplicationRow.get_dict(uid)["handoff"] == {"gaps": [], "unsure": [], "manual": []}


def test_filler_skips_files_when_uploads_are_off(tmp_db):
    """关掉自动上传后，文件栏一项都不碰，留给用户自己在浏览器里选。"""
    ApplyProfileSettings(name="张三").save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "file", "labelText": "简历上传"}],
        selectors={'[data-gj-idx="0"]'},
    )
    filler_ = FormFiller(_FakeSession(page))
    filler_.upload_files = False
    fields = [_field(index=0, key="resume", label="简历上传", type="file")]
    outcome = asyncio.run(filler_.fill(page, fields))
    assert outcome.filled == 0 and outcome.skipped == 1
    assert outcome.fields[0]["filled"] == ""


# --- 填完回读：页面里到底显示成什么了 -----------------------------------------


def test_filler_records_readback_of_filled_value(tmp_db):
    """填完把页面里的实际值记到 ``shown`` 上，值对得上就不标红。"""
    page = _FakePage(selectors={'[data-gj-idx="0"]'})
    fields = [_field(index=0, key="name", filled="张三")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert fields[0].shown == "张三" and fields[0].echo is True
    assert outcome.fields[0]["shown"] == "张三" and outcome.fields[0]["echo"] is True


def test_filler_flags_row_when_page_shows_something_else(tmp_db):
    """页面回读出来的值和准备填的不一样：标红，让人去浏览器里核这一栏。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        readback={'[data-gj-idx="0"]': "李四"},
    )
    fields = [_field(index=0, key="name", filled="张三")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert fields[0].shown == "李四" and fields[0].echo is False


def test_filler_readback_keeps_quiet_when_component_rewrites_value(tmp_db):
    """组件库回写时换写法（「本科」→「本科（学士）」）不算不一致，别误标红。"""
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        native={'[data-gj-idx="0"]': [["1", "本科（学士）"]]},
        shown={'[data-gj-idx="0"]': "本科（学士）"},
    )
    fields = [_field(index=0, tag="select", type="select", key="degree", filled="本科")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert fields[0].shown == "本科（学士）" and fields[0].echo is True


def test_filler_readback_reports_uploaded_file(tmp_db, tmp_path: Path):
    """上传栏回读官网写回的路径：这是「文件真传上去了」的实证。"""
    cv = tmp_path / "cv.pdf"
    cv.write_text("简历正文", encoding="utf-8")
    SearchConfigRow.save(resume_path=str(cv))
    page = _FakePage(
        selectors={'[data-gj-idx="0"]'},
        upload_path={'[data-gj-idx="0"]': ""},
    )
    fields = [_field(index=0, type="file", key="resume", filled="简历")]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 1
    assert fields[0].shown == "/upload/2026/demo-file.pdf" and fields[0].echo is True


def test_form_field_keeps_readback_across_dict_roundtrip(tmp_db):
    """回读结果要能随对照表存下来、读回来，否则刷新面板就丢了。"""
    field = _field(index=3, key="name", filled="张三", shown="李四", echo=False)
    back = FormField.from_dict(field.to_dict())
    assert back.shown == "李四" and back.echo is False


# --- 用户绕过面板、直接在官网页面上填的内容回流资料库 ---------------------------


def test_harvest_records_what_user_filled_in_the_browser(tmp_db):
    """用户直接在官网页面上补的栏：库里没有，回读出来就记进资料库。"""
    items = [
        _field(index=0, key="nation", label="民族", filled="汉族", shown="汉族"),
        _field(index=1, key="referral_code", label="内推码", filled="ABC123", shown="ABC123"),
    ]
    learned = ApplyAssistant._harvest(_profile(name="张三"), items)
    assert learned == [
        {"key": "nation", "label": "民族", "value": "汉族"},
        {"key": "referral_code", "label": "内推码", "value": "ABC123"},
    ]


def test_harvest_skips_keys_the_profile_already_has(tmp_db):
    """库里已经有值的键不收：那是官网写法不同，不该拿页面上的写法盖掉用户填的资料。"""
    items = [_field(index=0, key="nation", label="民族", shown="汉族")]
    assert ApplyAssistant._harvest(_profile(nation="回族"), items) == []


def test_harvest_skips_sensitive_irreversible_and_file_rows(tmp_db):
    """敏感事实、验证码、提交按钮、文件栏、以及页面上没值的栏，一律不收。"""
    items = [
        _field(index=0, key="id_card", label="身份证号", shown="110101199001011234"),
        _field(index=1, key="captcha", label="验证码", shown="8f3d"),
        _field(index=2, type="submit", key="submit", label="提交", shown="提交"),
        _field(index=3, type="file", key="resume", label="简历上传", shown="resume.pdf"),
        _field(index=4, key="nation", label="民族", shown=""),
    ]
    assert ApplyAssistant._harvest(_profile(), items) == []


def test_harvest_merges_synonyms_onto_one_key(tmp_db):
    """官网叫「ethnicity」、资料库叫「nation」：归到一个键上，别在库里存成两条。"""
    items = [
        _field(index=0, key="ethnicity", label="民族", shown="汉族"),
        _field(index=1, key="nation", label="民族", shown="汉族"),
    ]
    assert ApplyAssistant._harvest(_profile(), items) == [
        {"key": "nation", "label": "民族", "value": "汉族"}
    ]


def test_harvest_skips_overlong_values_and_hidden_rows(tmp_db):
    """超长的值多半不是一条通用资料；级联下拉收起来的第 2、3 级也不单独入库。"""
    items = [
        _field(index=0, key="self_intro", label="自我介绍", shown="很" * 121),
        _field(index=1, key="origin_place", label="生源地", shown="示例省", hidden=True),
    ]
    assert ApplyAssistant._harvest(_profile(), items) == []


def test_unfilled_detects_placeholder_button_and_stacked_options():
    """「其实没填」的几种样子：空值、占位提示、按钮字、下拉把整份选项堆在一起。"""
    blank = _field(index=0, label="民族")
    assert unfilled(blank, "") is True
    assert unfilled(blank, "   ") is True
    # 下拉没选时回读到的就是占位提示
    gender = _field(index=0, label="性别", options=["男", "女"])
    assert unfilled(gender, "请选择") is True
    # 输入框把自己的 placeholder 当成值回读了
    assert unfilled(_field(index=0, label="邮箱", placeholder="请输入邮箱"), "请输入邮箱") is True
    # 按钮 / 声明栏 / 区块标题的文字被连人带栏扫了进来
    assert unfilled(_field(index=0, label="确认投递"), "确认投递") is True
    assert unfilled(_field(index=0, label="人员类型（校招）"), "人员类型（校招）") is True
    # 下拉把整份选项堆在一起（「有 / 无」）
    child = _field(index=0, label="有无子女", options=["有", "无"])
    assert unfilled(child, "有\n无") is True
    # 真实的人事信息一律不算「没填」，哪怕看着奇怪
    assert unfilled(_field(index=0, label="民族", options=["汉族", "回族"]), "汉族") is False
    assert unfilled(_field(index=0, label="紧急联系人"), "张三 13800138000") is False


def test_harvest_skips_placeholder_and_button_rows(tmp_db):
    """官网没填的下拉、按钮 / 声明栏的文字：回读得到，但都不是一条个人资料。"""
    items = [
        _field(index=0, key="gender", label="性别", options=["男", "女"], shown="请选择"),
        _field(index=1, key="", label="确认投递", shown="确认投递"),
        _field(index=2, key="", label="亲属声明", shown="亲属声明"),
        _field(index=3, key="", label="人员类型（校招）", shown="人员类型（校招）"),
        _field(index=4, key="has_child", label="有无子女", options=["有", "无"], shown="有\n无"),
    ]
    assert ApplyAssistant._harvest(_profile(), items) == []


def test_vague_rejects_subordinate_and_numbered_keys():
    """「类型 / 等级 / 称谓」这类从属栏位名、站点自己的编号，都指不明一条资料。

    它们脱离了所在的那一块就不知道是哪条资料，学进库里只会多出一个含义不明的
    键——别家官网随便哪一栏叫「类型」都会被它填上。判定只拿归一化之后的键来做，
    所以「证件类型」→ ``id_type``、「民族」→ ``nation`` 都不会被误伤。
    """
    for raw in ("类型", "等级", "称谓", "开始时间", "获得时间", "有无", ""):
        assert vague(raw) is True
    # 带站点编号的键（块里的第几个实例），换个站点就对不上
    assert vague("同意调剂的意向公司1") is True
    assert vague("家庭成员2") is True
    # 归一化之后的规范键一律保留
    for raw in ("id_type", "school", "nation", "height", "major_category", "certificates"):
        assert vague(raw) is False


def test_harvest_skips_vague_rows(tmp_db):
    """「类型 / 同意调剂的意向公司1」这种栏位名回读到了也不进资料库。"""
    items = [
        _field(index=0, key="类型", label="类型", shown="英语"),
        _field(index=1, key="等级", label="等级", shown="CET4"),
        _field(index=2, key="同意调剂的意向公司1", label="同意调剂的意向公司1", shown="不限"),
    ]
    assert ApplyAssistant._harvest(_profile(), items) == []


def test_merge_rows_takes_rescan_and_keeps_manual_edits(tmp_db):
    """重扫结果为准（页面上没有的栏就不留），用户在面板上改过的行按身份盖回来。"""
    rows = [
        {
            "index": 0,
            "label": "姓名",
            "key": "name",
            "filled": "张三",
            "source": "手动",
            "selector": '[data-gj-idx="0"]',
        },
        {
            "index": 1,
            "label": "页面上已经没有的栏",
            "key": "ghost",
            "filled": "旧值",
            "source": "资料",
            "selector": '[data-gj-idx="9"]',
        },
    ]
    fresh = [
        _field(index=0, label="姓名", key="name", filled="", source="规则"),
        _field(index=1, label="邮箱", key="email", filled="a@b.cn", source="资料"),
    ]
    merged = ApplyAssistant._merge_rows(rows, fresh)
    assert [row["label"] for row in merged] == ["姓名", "邮箱"]
    # 用户手改过的值盖回来，程序这一轮算出来的空值不覆盖它
    assert merged[0]["filled"] == "张三" and merged[0]["source"] == "手动"
    assert merged[1]["filled"] == "a@b.cn" and merged[1]["source"] == "资料"


# --- AI 推算：认可后落库、删掉的不再推 ---------------------------------------


def test_learn_inferred_records_accepted_rows(tmp_db):
    """用户认可的推算行落库，带 ``AI推算`` 来源、依据与可信度。"""
    items = [
        _field(
            index=0,
            key="height",
            label="身高",
            filled="167",
            note="按简历里的体型描述推算",
            confidence=0.5,
            inferred=True,
        )
    ]
    learned = asyncio.run(ApplyAssistant._learn_inferred({"domain": "example.com"}, items))
    assert learned == [
        {
            "key": "height",
            "label": "身高",
            "value": "167",
            "source": "AI推算",
            "basis": "按简历里的体型描述推算",
            "confidence": 0.5,
        }
    ]
    row = next(r for r in ApplyLearnedRow.rows() if r["key"] == "height")
    assert row["source"] == "AI推算" and row["basis"] == "按简历里的体型描述推算"
    assert row["value"] == "167" and row["domain"] == "example.com"


def test_learn_inferred_respects_learn_ai_switch(tmp_db):
    """关掉「自动学习 AI 推算」就一条都不落，库里一条都不多。"""
    ApplyAutoSettings(learn_ai=False).save()
    items = [
        _field(index=0, key="height", label="身高", filled="167", note="依据", inferred=True)
    ]
    assert asyncio.run(ApplyAssistant._learn_inferred({"domain": "example.com"}, items)) == []
    assert ApplyLearnedRow.rows() == []


def test_learn_page_respects_learn_web_switch(tmp_db):
    """关掉「学习我在浏览器里手填的内容」就一条都不学，库里一条都不多。"""
    ApplyAutoSettings(learn_web=False).save()
    items = [_field(index=0, key="nation", label="民族", shown="汉族")]
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    assert asyncio.run(assistant._learn_page({"domain": "example.com"}, items)) == []
    assert ApplyLearnedRow.rows() == []


def test_learn_page_learns_hand_typed_rows(tmp_db):
    """开关开着时，用户在官网手填、库里没有的那条记进本机资料库。"""
    items = [_field(index=0, key="nation", label="民族", shown="汉族")]
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    learned = asyncio.run(assistant._learn_page({"domain": "example.com"}, items))
    assert learned == [{"key": "nation", "label": "民族", "value": "汉族"}]
    assert ApplyLearnedRow.values() == {"nation": "汉族"}


def test_learn_inferred_skips_sensitive_file_and_empty(tmp_db):
    """敏感事实、文件栏、空值、收起的级联行、以及非推算行，一律不收。"""
    items = [
        _field(index=0, key="id_card", label="身份证号", filled="110101199001011234",
               note="依据", inferred=True),
        _field(index=1, type="file", key="resume", label="简历上传", filled="resume.pdf",
               note="依据", inferred=True),
        _field(index=2, key="height", label="身高", filled="", note="依据", inferred=True),
        _field(index=3, key="nation", label="民族", filled="汉族", note="依据",
               inferred=True, hidden=True),
        _field(index=4, key="weight", label="体重", filled="60", note="依据"),
    ]
    assert asyncio.run(ApplyAssistant._learn_inferred({"domain": "example.com"}, items)) == []
    assert ApplyLearnedRow.rows() == []


def test_learn_inferred_merges_synonyms(tmp_db):
    """官网叫「ethnicity」、资料库叫「nation」：归到一个键上，别在库里存成两条。"""
    items = [
        _field(index=0, key="ethnicity", label="民族", filled="汉族", note="依据",
               inferred=True),
        _field(index=1, key="nation", label="民族", filled="汉族", note="依据",
               inferred=True),
    ]
    learned = asyncio.run(ApplyAssistant._learn_inferred({"domain": "example.com"}, items))
    assert [row["key"] for row in learned] == ["nation"]
    assert [row["key"] for row in ApplyLearnedRow.rows()] == ["nation"]


def test_learn_inferred_tidies_rows_before_saving(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """落库前过一遍 AI 整理：模型判为「不是一条资料」的推算行不进库。"""
    items = [
        _field(index=0, key="nation", label="民族", filled="汉族", note="依据", inferred=True),
        _field(
            index=1, key="relative_declaration", label="亲属声明",
            filled="本人与本公司员工无亲属关系", note="依据", inferred=True,
        ),
    ]
    calls: list[list[dict[str, str]]] = []

    async def fake_tidy(self: FieldMatcher, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        calls.append([dict(row) for row in rows])
        return [row for row in rows if row["key"] != "relative_declaration"]

    monkeypatch.setattr(FieldMatcher, "tidy_learned", fake_tidy)
    learned = asyncio.run(
        ApplyAssistant._learn_inferred(
            {"domain": "example.com"}, items, _ai_matcher(_profile(name="张三"))
        )
    )
    assert [row["key"] for row in learned] == ["nation"]
    assert [row["key"] for row in ApplyLearnedRow.rows()] == ["nation"]
    # 两条都送进模型判，模型说不是资料的才剔掉
    assert calls and {row["key"] for row in calls[0]} == {"nation", "relative_declaration"}


# --- AI 补全：第一遍填完后把剩余空栏交给模型再补一轮 ---------------------------


def test_ask_option_takes_option_the_model_quoted(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """本地对不上官网选项时，请模型从可选项里挑一个最贴近的，原样抄回来。"""
    matcher = _ai_matcher(_profile(person_type="2027届本科应届毕业生"))
    field = _field(
        index=0, key="person_type", label="人员类型", type="select",
        options=["国内应届毕业生", "留学归国人员"],
    )
    seen: list[str] = []

    class _Run:
        output = '{"option": "国内应届毕业生"}'

    async def _ask(prompt: str) -> Any:
        seen.append(prompt)
        return _Run()

    monkeypatch.setattr(matcher, "_ask_option", _ask)
    assert asyncio.run(matcher.ask_option(field, "2027届本科应届毕业生")) == "国内应届毕业生"
    # 送进去的是栏位名 + 资料值 + 这一栏的全部可选项
    assert seen and "人员类型" in seen[0] and "留学归国人员" in seen[0]


def test_ask_option_rejects_option_the_model_invented(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """模型自己造一个可选项里没有的字，一律作废——宁可留空也不硬塞。"""
    matcher = _ai_matcher(_profile(person_type="2027届本科应届毕业生"))
    field = _field(
        index=0, key="person_type", label="人员类型", type="select",
        options=["国内应届毕业生", "留学归国人员"],
    )

    class _Run:
        output = '{"option": "海归应届生"}'

    async def _ask(_prompt: str) -> Any:
        return _Run()

    monkeypatch.setattr(matcher, "_ask_option", _ask)
    assert asyncio.run(matcher.ask_option(field, "2027届本科应届毕业生")) == ""


def test_ask_option_skips_sensitive_and_offline(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """敏感栏（身份证）不外发；没开通 AI 服务时一次都不调。"""
    field = _field(index=0, key="id_card", label="身份证号", type="select", options=["A", "B"])
    calls: list[str] = []

    class _Run:
        output = '{"option": "A"}'

    async def _ask(prompt: str) -> Any:
        calls.append(prompt)
        return _Run()

    matcher = _ai_matcher(_profile(id_card="110101199001011234"))
    monkeypatch.setattr(matcher, "_ask_option", _ask)
    assert asyncio.run(matcher.ask_option(field, "110101199001011234")) == ""
    assert calls == []
    # 未配置大模型：同样不调、返回空
    assert asyncio.run(_matcher(_profile()).ask_option(field, "x")) == ""


def test_align_or_ask_fills_local_alignment_without_model(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """资料值本地就对得上官网选项：当场落栏，不劳模型。"""
    matcher = _ai_matcher(_profile(degree="本科"))
    field = _field(
        index=0, key="degree", label="学历", type="select",
        options=["大学本科", "硕士研究生"],
    )

    async def _ask(_prompt: str) -> Any:
        raise AssertionError("本地挑得出来就不该再问模型")

    monkeypatch.setattr(matcher, "_ask_option", _ask)
    assert asyncio.run(matcher.align_or_ask(field, "degree", "本科")) is True
    assert field.filled == "大学本科" and field.source == "资料" and field.ok


def test_align_or_ask_falls_back_to_model_then_gives_up(
    tmp_db, monkeypatch: pytest.MonkeyPatch
):
    """本地挑不出、模型挑得出：照模型给的选项落栏；两边都挑不出就留空。"""
    matcher = _ai_matcher(_profile(person_type="2027届本科应届毕业生"))
    field = _field(
        index=0, key="person_type", label="人员类型", type="select",
        options=["国内应届毕业生", "留学归国人员"],
    )

    class _Run:
        output = '{"option": "国内应届毕业生"}'

    async def _ask(_prompt: str) -> Any:
        return _Run()

    monkeypatch.setattr(matcher, "_ask_option", _ask)
    assert asyncio.run(matcher.align_or_ask(field, "person_type", "2027届本科应届毕业生")) is True
    assert field.filled == "国内应届毕业生"

    blank = _field(
        index=1, key="person_type", label="人员类型", type="select",
        options=["国内应届毕业生", "留学归国人员"],
    )

    class _Null:
        output = '{"option": null}'

    async def _null(_prompt: str) -> Any:
        return _Null()

    monkeypatch.setattr(matcher, "_ask_option", _null)
    assert (
        asyncio.run(matcher.align_or_ask(blank, "person_type", "2027届本科应届毕业生")) is False
    )
    # 两边都挑不出：这一栏原样留空，等用户自己选
    assert blank.filled == "" and blank.source == ""


def test_complete_fills_only_askable_blank_fields(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """AI 补全只补「还空着、也值得问」的栏：已填、文件、隐藏、提交都不碰。"""
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    matcher = _ai_matcher(_profile(name="张三"))
    seen: list[int] = []
    written: list[int] = []

    async def _outline(_page: Any, _matcher: Any) -> str:
        return ""

    async def _match_one(field: FormField, **_kw: Any) -> str:
        seen.append(field.index)
        field.filled = f"补{field.index}"
        return f"栏{field.index}"

    async def _fill(_page: Any, _fields: Any, *, only: Any = None, done: Any = None) -> FillOutcome:
        written.append(only.index)
        return FillOutcome(filled=1)

    async def _settle(_page: Any, _fields: Any) -> FillOutcome:
        return FillOutcome()

    monkeypatch.setattr(assistant, "_outline", _outline)
    monkeypatch.setattr(matcher, "match_one", _match_one)
    monkeypatch.setattr(assistant.filler, "fill", _fill)
    monkeypatch.setattr(assistant.filler, "settle", _settle)
    items = [
        _field(index=0, label="姓名", key="name", filled="张三"),  # 已填，不碰
        _field(index=1, label="民族", type="text"),  # 空着，补
        _field(index=2, label="简历上传", type="file"),  # 文件栏，不碰
        _field(index=3, label="隐藏项", hidden=True),  # 隐藏，不碰
        _field(index=4, label="提交", type="button"),  # 提交，不碰
        _field(index=5, label="政治面貌", type="select", options=["群众"]),  # 空着，补
    ]
    out = asyncio.run(assistant._complete(_FakePage(), items, matcher))
    assert sorted(seen) == [1, 5]
    assert sorted(written) == [1, 5]  # 补出来的两栏真写进了页面
    assert out.filled == 2


def test_complete_returns_empty_without_ai(tmp_db):
    """未开通 AI 服务：补全整批不做，返回空结果。"""
    assistant = ApplyAssistant(_FakeSession(_FakePage()))
    items = [_field(index=0, label="民族", type="text")]
    out = asyncio.run(
        assistant._complete(_FakePage(), items, _matcher(_profile(name="张三")))
    )
    assert out.filled == 0 and out.failed == 0


# --- 「学习手填内容」：用户在浏览器里改完之后当场比对入库 -----------------------


def test_alike_compares_text_and_numbers():
    """值比对：去空白、不分大小写；数字栏只比数字，容忍「167cm」与「167」。"""
    assert _alike("本科（学士）", "本科（学士）") is True
    assert _alike(" Acme ", "acme") is True
    assert _alike("示例省 示例市", "示例省示例市") is True
    assert _alike("本科", "硕士") is False
    assert _alike("167cm", "167", numeric=True) is True
    assert _alike("167", "170", numeric=True) is False


def test_fixes_replaces_derived_value(tmp_db):
    """库里的值靠推算得来、又和官网页面对不上：用页面上的真实值纠正它。"""
    profile = _profile(degree="本科")
    assert profile.entry("degree_name").source == "推算"
    items = [_field(index=0, key="degree_name", label="学位", shown="工学学士学位")]
    assert ApplyAssistant._fixes(profile, items) == [
        {"key": "degree_name", "label": "学位", "value": "工学学士学位"}
    ]


def test_fixes_leaves_manual_values_and_component_rows_alone(tmp_db):
    """只纠推算出来的那条：用户自己写的、下拉 / 单选 / 日期 / 推算行一律不动。"""
    profile = _profile(name="张三", degree="本科")
    assert profile.entry("name").source == "资料"
    items = [
        _field(index=0, key="name", label="姓名", shown="李四"),
        _field(index=1, key="degree_name", label="学位", shown="学士"),  # 值一样，不用纠
        _field(index=2, key="degree_name", label="学位", tag="select", type="select",
               options=["学士", "硕士"], shown="硕士"),
        _field(index=3, key="degree_name", label="学位", type="radio", shown="硕士"),
        _field(index=4, key="degree_name", label="学位", widget="select", shown="硕士"),
        _field(index=5, key="degree_name", label="学位", shown="硕士", inferred=True),
        _field(index=6, key="id_card", label="身份证号", shown="110101199001011234"),
        _field(index=7, key="degree_name", label="学位", shown="请选择"),
    ]
    assert ApplyAssistant._fixes(profile, items) == []


def test_fixes_tolerates_unit_suffix_on_number_fields(tmp_db):
    """数字栏：「167」与「167cm」是同一个值，不拿来纠错。"""
    profile = ApplyProfile(
        [ProfileEntry(key="height", label="身高", value="167", source="推算")]
    )
    items = [_field(index=0, key="height", label="身高", type="number", shown="167cm")]
    assert ApplyAssistant._fixes(profile, items) == []


def test_profile_learned_replaces_derived_entry(tmp_db):
    """库里那条是推算出来的：学到页面上的真实值就地替换，不留成两条。"""
    settings = ApplyProfileSettings(degree="本科")
    assert ApplyProfile.from_sources(settings).entry("degree_name").source == "推算"
    profile = ApplyProfile.from_sources(settings, learned={"degree_name": "工学学士学位"})
    entry = profile.entry("degree_name")
    assert entry.value == "工学学士学位" and entry.source == "记忆"
    assert len([row for row in profile.entries if row.key == "degree_name"]) == 1
    # 用户自己在配置中心写的值不受记忆影响
    kept = ApplyProfile.from_sources(
        ApplyProfileSettings(degree="本科", name="张三"), learned={"name": "李四"}
    )
    assert kept.entry("name").value == "张三" and kept.entry("name").source == "资料"


def test_learn_now_respects_learn_web_switch(tmp_db):
    """关掉「学习我在浏览器里手填的内容」：一次都不学，库里一条都不多。"""
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://acme.cn/apply")
    ApplyAutoSettings(learn_web=False).save()
    page = _FakePage(
        raw=[{"tag": "input", "type": "text", "labelText": "民族", "path": "#n"}],
        selectors={'[data-gj-idx="0"]'},
        readback={'[data-gj-idx="0"]': "汉族"},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    assert asyncio.run(assistant.learn_now(uid)) == {
        "added": [],
        "fixed": [],
        "disabled": True,
    }
    assert ApplyLearnedRow.rows() == []


def test_learn_now_learns_new_and_fixes_derived(tmp_db):
    """点「学习手填内容」：页面上新填的学进来，库里推算错的按页面真实值纠正。"""
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://acme.cn/apply")
    ApplyProfileSettings(degree="本科").save()
    page = _FakePage(
        raw=[
            {"tag": "input", "type": "text", "labelText": "民族", "path": "#n"},
            {"tag": "input", "type": "text", "labelText": "学位", "path": "#d"},
        ],
        selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]'},
        readback={'[data-gj-idx="0"]': "汉族", '[data-gj-idx="1"]': "工学学士学位"},
    )
    assistant = ApplyAssistant(_FakeSession(page))
    out = asyncio.run(assistant.learn_now(uid))
    assert [row["key"] for row in out["added"]] == ["nation"]
    assert [row["key"] for row in out["fixed"]] == ["degree_name"]
    assert ApplyLearnedRow.values() == {"nation": "汉族", "degree_name": "工学学士学位"}
    # 纠正来的那条标成手填，落库后能压住旧的推算值
    row = next(r for r in ApplyLearnedRow.rows() if r["key"] == "degree_name")
    assert row["source"] == "手动" and row["domain"] == "acme.cn"


def test_learn_now_tidies_new_and_fixed_in_one_model_call(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """新学的和纠错的合成一趟模型请求：分两次调要串两趟，慢一倍、token 也翻倍。"""
    uid = _seed_job("j1")
    ApplicationRow.create(uid, "https://acme.cn/apply")
    ApplyProfileSettings(degree="本科").save()
    page = _FakePage(
        raw=[
            {"tag": "input", "type": "text", "labelText": "民族", "path": "#n"},
            {"tag": "input", "type": "text", "labelText": "学位", "path": "#d"},
        ],
        selectors={'[data-gj-idx="0"]', '[data-gj-idx="1"]'},
        readback={'[data-gj-idx="0"]': "汉族", '[data-gj-idx="1"]': "工学学士学位"},
    )
    calls: list[list[dict[str, str]]] = []

    async def fake_tidy(self: FieldMatcher, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        calls.append([dict(row) for row in rows])
        return rows

    monkeypatch.setattr(FieldMatcher, "tidy_learned", fake_tidy)
    out = asyncio.run(ApplyAssistant(_FakeSession(page)).learn_now(uid))
    assert len(calls) == 1  # 只调一趟模型
    assert {row["key"] for row in calls[0]} == {"nation", "degree_name"}  # 两拨一起送
    assert [row["key"] for row in out["added"]] == ["nation"]
    assert [row["key"] for row in out["fixed"]] == ["degree_name"]
    # 整理用的内部标记不该漏进结果
    assert all("_src" not in row for row in out["added"] + out["fixed"])


def test_drop_rejected_clears_rows_and_reports_keys(tmp_db):
    """用户删掉的行：清空它、不填进页面，并回报归一化后的键给拒绝表。"""
    items = [
        _field(index=0, key="height", label="身高", filled="167", source="推算",
               inferred=True, note="依据", confidence=0.5),
        _field(index=1, key="weight", label="体重", filled="60", source="推算",
               inferred=True, note="依据", confidence=0.5),
    ]
    keys = ApplyAssistant._drop_rejected(items, [1])
    assert keys == ["weight"]
    kept = items[0]
    assert kept.filled == "167" and kept.inferred is True and kept.source == "推算"
    dropped = items[1]
    assert dropped.filled == "" and dropped.key == "" and dropped.inferred is False
    assert dropped.source == "" and dropped.note == "" and dropped.ok is False
    assert dropped.confidence == 0.0


def test_drop_rejected_keeps_sensitive_key_out_of_reject_list(tmp_db):
    """敏感键不进拒绝表：它的值本来就不通用，不该在库里留下键名。"""
    items = [_field(index=0, key="id_card", label="身份证号", filled="1", source="推算",
                    inferred=True, note="依据")]
    assert ApplyAssistant._drop_rejected(items, [0]) == []
    assert items[0].filled == ""


def test_rejected_key_is_not_inferred_again(tmp_db, monkeypatch: pytest.MonkeyPatch):
    """用户删过的那条，同一站点下次出计划时不再推；别的键照推不误。"""
    ApplyRejectRow.remember("example.com", ["height"])
    matcher = _ai_matcher(_profile(name="张三"))
    fields = [_field(index=0, label="身高"), _field(index=1, label="体重")]

    async def _ask(_fields: Any, _outline: str = "") -> dict[str, tuple[str, str, str]]:
        return {"0": ("height", "167", "按简历推算"), "1": ("weight", "60", "按简历推算")}

    monkeypatch.setattr(matcher, "_ask", _ask)
    asyncio.run(matcher.match_ai(fields))
    assert fields[0].inferred is False and fields[0].filled == ""
    assert fields[1].inferred is True and fields[1].filled == "60"


def test_reject_row_round_trip(tmp_db):
    """拒绝表按「域名::资料键」记账：同一站点查得到、别的站点查不到。"""
    assert ApplyRejectRow.remember("example.com", ["height", "weight", ""]) == 2
    assert ApplyRejectRow.rejected("example.com") == {"height", "weight"}
    assert ApplyRejectRow.rejected("other.com") == set()
    assert ApplyRejectRow.forget("example.com", "height") is True
    assert ApplyRejectRow.rejected("example.com") == {"weight"}
    assert ApplyRejectRow.clear() == 1
    assert ApplyRejectRow.rejected("example.com") == set()


# --- 围栏：提交类控件一个都不碰 -----------------------------------------------


def test_filler_never_touches_irreversible_controls_in_a_mixed_form(tmp_db):
    """一张表里混着提交 / 重置 / 按钮 / 图片控件：程序碰都不碰，连点击都不产生。"""
    page = _FakePage(selectors={f'[data-gj-idx="{i}"]' for i in range(6)})
    fields = [
        _field(index=0, key="name", filled="张三"),
        _field(index=1, type="submit", key="submit", filled="提交"),
        _field(index=2, type="reset", filled="重置"),
        _field(index=3, type="button", filled="下一步"),
        _field(index=4, type="image", filled="图片按钮"),
        _field(index=5, key="phone", filled="13800138000"),
    ]
    outcome = asyncio.run(FormFiller(None).fill(page, fields))
    assert outcome.filled == 2 and outcome.skipped == 4
    banned = {f'[data-gj-idx="{i}"]' for i in range(1, 5)}
    assert [call for call in page.calls if call[1] in banned] == []
    for item in fields[1:5]:
        assert item.ok is False and "手动" in item.note
