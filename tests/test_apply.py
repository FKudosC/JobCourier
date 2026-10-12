"""网申助手：资料合成、表单扫描、字段匹配、填表、任务与 API。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from job import models as models_pkg
from job.api.app import app
from job.apply.assistant import ApplyAssistant, ApplyError
from job.apply.filler import FormFiller
from job.apply.matcher import FieldMatcher
from job.apply.profile import ApplyProfile, resume_facts
from job.apply.scanner import (
    FormField,
    FormScanner,
    guess_company,
    guess_title,
    normalize,
    read_page_meta,
)
from job.boss.jobs import Job
from job.models import db_session, init_db, reset_engine
from job.models.application import ApplicationRow, ApplyMappingRow, domain_of
from job.models.job import JobRow
from job.models.search import SearchConfigRow
from job.models.setting import ApplyProfileSettings

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

    def __init__(self, frame: "_FakeFrame", selector: str) -> None:
        self.frame = frame
        self.selector = selector

    async def count(self) -> int:
        return 1 if self.selector in self.frame.selectors else 0

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def fill(self, value: str) -> None:
        self.frame.calls.append(("fill", self.selector, value))

    async def select_option(self, value: str) -> None:
        self.frame.calls.append(("select", self.selector, value))

    async def check(self) -> None:
        self.frame.calls.append(("check", self.selector, ""))

    async def set_input_files(self, value: str) -> None:
        self.frame.calls.append(("file", self.selector, value))


class _FakeFrame:
    """假 frame：``evaluate`` 按脚本内容区分「取控件」还是「取正文」。"""

    def __init__(
        self,
        raw: list[dict[str, Any]] | None = None,
        text: str = "",
        selectors: Any = (),
        meta: dict[str, str] | None = None,
    ) -> None:
        self.raw = raw or []
        self.text = text
        self.selectors = set(selectors)
        self.meta = meta or {}
        self.calls: list[tuple[str, str, str]] = []

    async def evaluate(self, source: str) -> Any:
        if "data-gj-idx" in source:
            return self.raw
        if "og:site_name" in source:
            return self.meta
        return self.text

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)


class _FakePage:
    """假标签页：一个 frame 承载全部控件。"""

    def __init__(
        self,
        raw: list[dict[str, Any]] | None = None,
        text: str = "",
        selectors: Any = (),
        frame_count: int = 1,
        meta: dict[str, str] | None = None,
    ) -> None:
        self.frame = _FakeFrame(raw, text, selectors, meta)
        self.main_frame = self.frame
        self._frames = [self.frame] * frame_count
        self.goto_calls: list[str] = []
        self._closed = False

    @property
    def frames(self) -> list[_FakeFrame]:
        return self._frames

    @property
    def calls(self) -> list[tuple[str, str, str]]:
        return self.frame.calls

    async def goto(self, url: str, **kw: Any) -> None:
        self.goto_calls.append(url)

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True


class _FakeSession:
    """假会话：只记 new_page / set_background 的调用。"""

    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.pages: list[_FakePage] = []
        self.background: list[bool] = []

    async def new_page(self) -> _FakePage:
        self.pages.append(self.page)
        return self.page

    async def set_background(self, value: bool) -> None:
        self.background.append(value)


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


def test_profile_structured_first_then_resume_then_extra(tmp_db):
    settings = ApplyProfileSettings(name="张三", phone="13900139000", extra={"籍贯": "杭州"})
    resume = "姓名：李四\n邮箱 li@example.com\n13800138000"
    profile = ApplyProfile.from_sources(settings, resume)
    values = profile.values()
    assert values["name"] == "张三"  # 结构化字段优先
    assert values["phone"] == "13900139000"
    assert values["email"] == "li@example.com"  # 结构化没填，用简历兜底
    assert values["籍贯"] == "杭州"
    assert profile.entry("name").source == "资料"
    assert profile.entry("email").source == "简历"
    assert profile.entry("籍贯").source == "自定义"
    assert profile.labels()["name"] == "姓名"
    assert len(profile) == 4 and profile
    assert profile.get("nope") == "" and profile.entry("nope") is None


def test_profile_load_reads_saved_settings(tmp_db):
    ApplyProfileSettings(name="张三", phone="13900139000").save()
    SearchConfigRow.save(resume_text="姓名：张三\n13800138000")
    profile = ApplyProfile.load()
    assert profile.values()["name"] == "张三"
    assert profile.values()["phone"] == "13900139000"
    assert profile.resume  # 简历正文一并带上，供 AI 兜底参考


def test_apply_profile_settings_ready_and_filled(tmp_db):
    blank = ApplyProfileSettings.load()
    assert blank.filled() == {} and not blank.ready
    assert ApplyProfileSettings(name="张三", phone="13900139000").ready
    assert ApplyProfileSettings(name="张三", email="a@b.com").ready
    assert not ApplyProfileSettings(name="张三").ready  # 只有姓名还不够
    filled = ApplyProfileSettings(name=" 张三 ", phone="", extra={"作品集": "x"}).filled()
    assert filled["name"] == "张三" and "phone" not in filled and filled["作品集"] == "x"


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


def test_scanner_gate_detects_captcha():
    assert "滑块" in asyncio.run(FormScanner(None).gate(_FakePage(text="请拖动滑块完成验证")))
    assert asyncio.run(FormScanner(None).gate(_FakePage(text="请填写您的姓名"))) == ""


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


def test_matcher_reuses_history_mapping(tmp_db):
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0)
    ApplyMappingRow.remember(
        "example.com",
        [{"signature": field.signature(), "label": "姓名", "value": "李四", "source": "manual"}],
    )
    asyncio.run(_matcher(_profile(name="张三"), "example.com").match([field]))
    assert field.filled == "李四" and field.key == "manual"
    assert field.source == "沿用" and field.confidence == 0.95


def test_matcher_remember_only_stores_filled_fields(tmp_db):
    matcher = _matcher(_profile(name="张三"), "b.com")
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "姓名"}, index=0)
    asyncio.run(matcher.match([field]))
    assert matcher.remember([field]) == 1
    stored = ApplyMappingRow.lookup("b.com", [field.signature()])
    assert stored[field.signature()].value == "张三"
    blank = FormField.from_raw({"tag": "input", "type": "text", "labelText": "备注"}, index=1)
    assert matcher.remember([blank]) == 0


def test_matcher_skips_ai_when_service_off(tmp_db):
    field = FormField.from_raw({"tag": "input", "type": "text", "labelText": "随便填点"}, index=0)
    asyncio.run(_matcher(_profile(name="张三")).match([field]))
    assert field.key == "" and field.filled == "" and field.ok


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


def test_open_task_alerts_on_captcha(tmp_db):
    uid = _seed_job("j1")
    session = _FakeSession(_FakePage(text="请完成安全验证后继续"))
    assistant = ApplyAssistant(session)
    result = asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    assert "安全验证" in result["gate"]
    assert session.background == [False]  # 把窗口调到屏幕前，等用户手动过验证
    assert "安全验证" in ApplicationRow.get_dict(uid)["note"]


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


def test_submit_marks_done_and_closes_page(tmp_db):
    uid = _seed_job("j1")
    page = _FakePage(raw=[], selectors=set())
    assistant = ApplyAssistant(_FakeSession(page))
    asyncio.run(assistant.open_task(uid, "https://acme.cn/apply"))
    saved = asyncio.run(assistant.submit(uid))
    assert saved["status"] == "submitted" and saved["statusLabel"] == "已提交"
    assert page.is_closed() is True and uid not in assistant._pages


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
    assert data["ready"] is False and data["values"]["name"] == "" and data["extra"] == {}
    saved = client.put(
        "/api/config/apply-profile",
        json={"values": {"name": "张三", "phone": "13900139000", "extra:籍贯": "杭州"}},
    ).json()
    assert saved["values"]["name"] == "张三" and saved["ready"] is True
    assert saved["extra"] == {"籍贯": "杭州"}
    assert any(e["key"] == "籍贯" and e["source"] == "自定义" for e in saved["entries"])
    # 空值删掉自定义字段；没传的字段保持原样
    trimmed = client.put("/api/config/apply-profile", json={"values": {"extra:籍贯": ""}}).json()
    assert trimmed["extra"] == {} and trimmed["values"]["name"] == "张三"


def test_config_bundle_exposes_apply_profile(client: TestClient):
    cfg = client.get("/api/config").json()
    assert cfg["apply_profile"]["labels"]["phone"] == "手机号"


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
    assert client.post("/api/apply/task/nope/submit").status_code == 400
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
