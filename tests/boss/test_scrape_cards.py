"""逐卡片处理：重复直接跳过，不合适的也带原因入库，AI 出错不入库。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

from job import models as models_pkg
from job.boss.filters import KeywordFilter
from job.boss.jobs import Job, JobScraper
from job.boss.review import Verdict
from job.models import init_db, reset_engine
from job.models.job import JobRow


class FakeCards:
    def __init__(self, n: int) -> None:
        self.n = n

    async def count(self) -> int:
        return self.n

    def nth(self, i: int) -> int:
        return i


class FakePage:
    def __init__(self, n: int) -> None:
        self.cards = FakeCards(n)

    def locator(self, _selector: str) -> FakeCards:
        return self.cards


class FakeReviewer:
    """按职位名给结论：含「销售」不合适，含「出错」抛异常。"""

    async def check(self, job: Job) -> Verdict:
        if "出错" in job.title:
            raise UnexpectedModelBehavior("接口超时")
        if "销售" in job.title:
            return Verdict(match=False, reason="销售岗")
        return Verdict(match=True, reason="Agent 方向吻合")


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


def test_scrape_cards_records_every_decision(tmp_db):
    jobs = [
        Job(job_id="dup", title="AI 工程师", company="老公司", hr_name="张女士"),
        Job(job_id="kw", title="AI 实习生", company="某公司"),
        Job(job_id="sale", title="AI 销售", company="某公司"),
        Job(job_id="err", title="AI 出错", company="某公司"),
        Job(job_id="ok", title="AI Agent 工程师", company="某公司"),
    ]
    JobRow.record(
        Job(job_id="old", title="AI 工程师", company="老公司", hr_name="张女士"),
        suitable=False,
        reason="外包公司",
    )
    scraper = JobScraper(session=None)  # type: ignore[arg-type]
    scraper._keywords = KeywordFilter(exclude=["实习"])
    scraper._reviewer = FakeReviewer()  # type: ignore[assignment]
    applied: list[int] = []
    logs: list[str] = []

    async def listed(_card, i: int) -> Job:
        return jobs[i]

    async def detail(_page, _card, job: Job) -> Job:
        return job

    async def apply(_page, _job) -> str:
        applied.append(1)
        return ""

    async def pause(_span) -> None:
        pass

    async def on_log(_level: str, text: str) -> None:
        logs.append(text)

    scraper._listed_job_for = listed  # type: ignore[method-assign]
    scraper._open_detail = detail  # type: ignore[method-assign]
    scraper._apply = apply  # type: ignore[method-assign]
    scraper._on_log = on_log
    engine = scraper.engine  # 懒建引擎并拾取上面的关键词 / 复核器 / 日志配置
    engine.pause = pause  # type: ignore[method-assign]

    batch = asyncio.run(scraper._scrape_cards(FakePage(len(jobs)), set(), None))

    assert [j.job_id for j in batch] == ["ok"] and len(applied) == 1
    assert JobRow.get_dict("dup") is None
    assert any("看过，不合适：外包公司" in line for line in logs)
    assert JobRow.get_dict("kw")["result"] == "不合适"
    assert JobRow.get_dict("kw")["reason"] == "职位名含排除词"
    assert JobRow.get_dict("sale")["reason"] == "销售岗"
    # AI 复核失败：岗位入库留痕（等收尾自动评分补上），但不投递
    err = JobRow.get_dict("err")
    assert err is not None and err["suitable"] is True and err["applied"] is False
    assert "AI 复核失败" in err["reason"] or err["reason"] == "AI 复核失败，未评分"
    ok = JobRow.get_dict("ok")
    assert (ok["result"], ok["reason"]) == ("已投递", "Agent 方向吻合")


def test_scrape_cards_stops_at_keyword_quota(tmp_db, monkeypatch):
    """每轮每关键词投满额度就停：剩余的合适岗位不入库，留给下一轮再投。"""
    monkeypatch.setattr(JobScraper, "PER_KEYWORD_ROUND", 1)
    jobs = [
        Job(job_id="ok1", title="AI Agent 工程师", company="某公司"),
        Job(job_id="sale", title="AI 销售", company="某公司"),
        Job(job_id="ok2", title="AI Agent 专家", company="某公司"),
    ]
    scraper = JobScraper(session=None)  # type: ignore[arg-type]
    scraper._reviewer = FakeReviewer()  # type: ignore[assignment]
    applied: list[int] = []
    logs: list[str] = []

    async def listed(_card, i: int) -> Job:
        return jobs[i]

    async def detail(_page, _card, job: Job) -> Job:
        return job

    async def apply(_page, _job) -> str:
        applied.append(1)
        return ""

    async def pause(_span) -> None:
        pass

    async def on_log(_level: str, text: str) -> None:
        logs.append(text)

    scraper._listed_job_for = listed  # type: ignore[method-assign]
    scraper._open_detail = detail  # type: ignore[method-assign]
    scraper._apply = apply  # type: ignore[method-assign]
    scraper._on_log = on_log
    engine = scraper.engine  # 懒建引擎并拾取上面的关键词 / 复核器 / 日志配置
    engine.pause = pause  # type: ignore[method-assign]

    batch = asyncio.run(scraper._scrape_cards(FakePage(len(jobs)), set(), None))

    # 第 1 个合适岗位投出后本轮额度即满：ok2 不执行投递也不入库，下一轮重新处理
    assert [j.job_id for j in batch] == ["ok1"] and len(applied) == 1
    assert JobRow.get_dict("ok1")["applied"] is True
    # 不合适的岗位照常入库留痕
    assert JobRow.get_dict("sale")["result"] == "不合适"
    assert JobRow.get_dict("ok2") is None
    assert any("额度已满" in line for line in logs)
