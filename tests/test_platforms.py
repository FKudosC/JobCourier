"""投递决策引擎（BOSS 直聘）：筛选记录、失败留痕、断点续采。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from job import models as models_pkg
from job.boss.filters import KeywordFilter
from job.boss.jobs import Job
from job.models import init_db, reset_engine
from job.models.job import JobRow
from job.platforms import checkpoint
from job.platforms.engine import (
    APPLIED,
    CHATTED,
    FAILED,
    SKIP,
    DeliveryEngine,
)


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


async def _instant(_span) -> None:
    """测试用：引擎的随机停顿换成立即返回。"""


def _job(job_id: str = "j1", title: str = "AI 工程师", company: str = "某公司") -> Job:
    return Job(job_id=job_id, title=title, company=company)


def _engine(**over) -> DeliveryEngine:
    params = dict(
        platform="boss",
        daily_limit=10,
        on_log=None,
        stop=asyncio.Event(),
    )
    params.update(over)
    engine = DeliveryEngine(**params)
    engine.load_blacklist()
    engine.pause = _instant
    return engine


def test_engine_keyword_reject_records_unsuitable(tmp_db):
    engine = _engine(keywords=KeywordFilter(exclude=["实习"]))
    outcome = asyncio.run(engine.process(_job("j1", "AI 实习生"), apply=None))
    assert outcome.status == SKIP
    row = JobRow.get_dict("j1")
    assert row["suitable"] is False and row["reason"] == "职位名含排除词"


def test_engine_blacklist_blocks_by_id_and_name(tmp_db):
    from job.models.blacklist import CompanyBlacklistRow

    CompanyBlacklistRow.add(brand_id="bossId1", reason="BOSS 拉黑")
    CompanyBlacklistRow.add(name="黑名单公司", reason="手动拉黑")
    engine = _engine()
    bad = Job(job_id="b9", title="T", company="别家", brand_id="bossId1")
    assert asyncio.run(engine.process(bad, apply=None)).status == SKIP
    by_name = Job(job_id="b10", title="T2", company="黑名单公司")
    assert asyncio.run(engine.process(by_name, apply=None)).status == SKIP


def test_engine_downgrade_off_keeps_boss_behavior(tmp_db):
    """BOSS 投递失败只记日志不入库（与原行为一致），岗位下次还会再看。"""

    async def fail(_job: Job) -> str:
        return "投递失败：超时"

    engine = _engine(downgrade_on_fail=False)
    outcome = asyncio.run(engine.process(_job(), apply=fail))
    assert outcome.status == FAILED
    assert JobRow.get_dict("j1") is None


def test_engine_applied_records_and_counts(tmp_db):
    async def ok(_job: Job) -> str:
        return ""

    engine = _engine(daily_limit=2)
    outcome = asyncio.run(engine.process(_job("j1", "AI 工程师 A"), apply=ok))
    assert outcome.status == APPLIED
    assert engine.applied_today == 1
    assert JobRow.get_dict("j1")["applied"] is True

    # 达到日限额：结果标记 LIMIT 并停止
    engine.daily_limit = 1
    engine.applied_today = 1
    outcome = asyncio.run(engine.process(_job("j2", "AI 工程师 B"), apply=ok))
    assert outcome.status == "limit" and engine.limit_hit is True


def test_engine_chatted_marks_without_reapply(tmp_db):
    async def chatted(_job: Job) -> str:
        return "此前已沟通"

    engine = _engine()
    outcome = asyncio.run(
        engine.process(_job(), apply=chatted, chatted_mark="此前已沟通")
    )
    assert outcome.status == CHATTED
    row = JobRow.get_dict("j1")
    assert row["applied"] is False and row["suitable"] is True


def test_engine_require_description_skips_when_no_jd(tmp_db):
    """开启了 AI 复核且详情拿不到 → 只采集不评分不投递。"""

    class FakeReviewer:
        async def check(self, job: Job):
            from job.boss.review import Verdict

            return Verdict(match=True, reason="ok")

    engine = _engine(reviewer=FakeReviewer())
    job = Job(job_id="l1", title="T", company="C")  # 无描述
    outcome = asyncio.run(engine.process(job, apply=None, require_description=True))
    assert outcome.status == SKIP
    assert JobRow.get_dict("l1")["reason"] == "详情需登录后可见，未评分"


def test_engine_review_failure_keeps_record_for_rescore(tmp_db):
    """AI 复核失败：岗位入库留痕，收尾自动评分会补上。"""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    class BrokenReviewer:
        async def check(self, job: Job):
            raise UnexpectedModelBehavior("接口超时")

    engine = _engine(reviewer=BrokenReviewer())
    outcome = asyncio.run(engine.process(_job(), apply=None))
    assert outcome.status == FAILED
    row = JobRow.get_dict("j1")
    assert row["suitable"] is True and row["reason"] == "AI 复核失败，未评分"


# --- 断点按平台键存储（当前仅 BOSS） ---


def test_checkpoint_roundtrip(tmp_db):
    targets = [("广州", "u1"), ("深圳", "u2")]
    sig = checkpoint.sig_of(targets, [""])
    entries = [f"@{city}" for city, _ in targets]
    checkpoint.save("boss", sig, entries, {"@广州"})

    assert checkpoint.load("boss", sig) == {"@广州"}
    # 关键词变化进度作废
    assert checkpoint.load("boss", checkpoint.sig_of(targets, ["Python"])) == set()
    checkpoint.clear("boss")
    assert checkpoint.load("boss", sig) == set()
