"""投递漏斗统计：漏斗计数、响应时效右删失、评分校准分档。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job import models as models_pkg
from job.boss.jobs import Job
from job.models import init_db, reset_engine, stats
from job.models.chat import ChatMessageRow, ChatStatusRow
from job.models.job import JobRow


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


def _job(uid: str, *, score: int | None = None, applied: bool = False, suitable: bool = True):
    job = Job(job_id=uid, title=f"AI 工程师 {uid}", company="某公司")
    JobRow.record(job, score=score, applied=applied, suitable=suitable)


def _msg(boss_id: str, mid: str, from_hr: bool, *, hours_ago: float, job_uid: str = ""):
    with models_pkg.db_session() as session:
        session.add(
            ChatMessageRow(
                mid=mid,
                boss_id=boss_id,
                job_uid=job_uid,
                hr_name="张女士",
                from_hr=from_hr,
                text="你好",
                created_at=datetime.now(UTC) - timedelta(hours=hours_ago),
            )
        )
        session.commit()


def test_funnel_counts_each_stage(tmp_db):
    _job("a", applied=True)
    # 投递后被自动评分补判为低分：applied 为真、suitable 被翻成 False
    _job("b", applied=True, suitable=False)
    _job("c")
    _msg("b1", "m1", False, hours_ago=10, job_uid="a")
    _msg("b1", "m2", True, hours_ago=9, job_uid="a")
    ChatStatusRow.mark("b1", "invited", source="auto")

    data = stats.funnel()
    assert data["total"] == 3
    # 漏斗必须单调：判为合适要把已投递的算进来，否则会出现已投递 > 判为合适
    assert data["suitable"] == 3
    assert data["applied"] == 2
    assert data["suitable"] >= data["applied"]
    assert data["interviews"] == 1
    assert data["passed"] == 0


def test_daily_applied_pads_missing_days(tmp_db):
    _job("a", applied=True)
    data = stats.daily_applied(days=7)
    assert len(data) == 7
    assert data[-1]["count"] == 1
    assert sum(d["count"] for d in data) == 1


def test_response_times_right_censoring(tmp_db):
    """还在等的会话不进中位数，但要计数——否则只有秒回样本会低估等待。"""
    _msg("b1", "m1", False, hours_ago=100)
    _msg("b1", "m2", True, hours_ago=4)  # 已回，等了 96 小时
    _msg("b2", "m3", False, hours_ago=50)  # 没回
    data = stats.response_times()
    assert data["replied"] == 1
    assert data["waiting"] == 1
    assert data["median"] == pytest.approx(96, abs=0.2)


def test_response_times_hr_first_excluded(tmp_db):
    _msg("b1", "m1", True, hours_ago=100)
    data = stats.response_times()
    assert data == {"median": None, "replied": 0, "waiting": 0}


def test_response_times_empty(tmp_db):
    assert stats.response_times() == {"median": None, "replied": 0, "waiting": 0}


def test_score_buckets_rate_needs_min_sample(tmp_db):
    """样本不足的分档不给面试率，样本够才报。"""
    for i in range(6):
        uid = f"j{i}"
        _job(uid, score=70 + i, applied=True)
        _msg(f"b{i}", f"m{i}", False, hours_ago=10, job_uid=uid)
    _msg("b0", "r0", True, hours_ago=5, job_uid="j0")
    ChatStatusRow.mark("b0", "invited", source="auto")

    buckets = stats.score_buckets()
    hit = next(b for b in buckets if b["bucket"] == "70–79")
    assert hit["applied"] == 6 and hit["interviewed"] == 1
    assert hit["rate"] == 17  # round(1/6 * 100)
    low = next(b for b in buckets if b["bucket"] == "60 以下")
    assert low["rate"] is None


def test_status_distribution_includes_unmarked(tmp_db):
    _msg("b1", "m1", True, hours_ago=1)
    _msg("b2", "m2", True, hours_ago=1)
    ChatStatusRow.mark("b2", "invited")
    items = {i["label"]: i["count"] for i in stats.status_distribution()}
    assert items["无状态"] == 1
    assert items["有面试"] == 1


def test_pct_bar_width_is_numeric():
    """条宽必须是真实百分比：字符串 max 会把 "100.00%" 排在 "2px" 后面，条全变 2px。"""
    from job.models.stats import pct

    assert pct(0, 10) == "0"
    assert pct(10, 10) == "100.00%"
    assert pct(1, 3) == "33.33%"
    assert pct(1, 0) == "0"
