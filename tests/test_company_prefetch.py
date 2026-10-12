"""公司背景自动预查与定制简历落盘。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from job import models as models_pkg
from job.boss.company import CompanyChecker, CompanyPrefetcher, CompanyVerdict
from job.boss.resume_tailor import load_saved_resume, save_custom_resume
from job.models import init_db, reset_engine
from job.models.company import CompanyRow


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


class FakeSearcher:
    """记录搜索调用；可配置抛错。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def search(self, name, on_step=None):
        self.calls.append(name)
        if self.fail:
            raise RuntimeError("网络不通")
        return []


class FakeReviewer:
    """按公司名给结论：名字含「雷」判高风险。"""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def review(self, name, full_name, info, hits) -> CompanyVerdict:
        self.asked.append(name)
        if "雷" in name:
            return CompanyVerdict(risk="high", summary="有欠薪记录", points=[])
        return CompanyVerdict(risk="low", summary="没有负面", points=[])


def _checker(search: FakeSearcher, reviewer) -> CompanyChecker:
    return CompanyChecker(page=None, search=search, reviewer=reviewer)


def test_prefetcher_checks_once_per_company(tmp_db):
    search, reviewer = FakeSearcher(), FakeReviewer()
    prefetcher = CompanyPrefetcher(lambda: _checker(search, reviewer))

    async def main():
        first = prefetcher.schedule(platform="boss", brand_id="某公司", name="某公司")
        same = prefetcher.schedule(platform="boss", brand_id="某公司", name="某公司")
        await asyncio.sleep(0.2)  # 让后台任务跑完
        return first, same

    first, same = asyncio.run(main())
    assert first is True and same is False
    assert len(search.calls) == 1 and len(reviewer.asked) == 1
    # 公司键 = 品牌键，岗位页风险徽章按它关联
    assert CompanyRow.risks(["某公司"]) == {"某公司": "low"}


def test_prefetcher_skips_fresh_companies(tmp_db):
    CompanyRow.save(
        "某公司",
        name="某公司",
        full_name="",
        info={},
        hits=[],
        risk="low",
        summary="刚查过",
        points=[],
    )
    search = FakeSearcher()
    prefetcher = CompanyPrefetcher(lambda: _checker(search, FakeReviewer()))

    async def main():
        scheduled = prefetcher.schedule(platform="boss", brand_id="某公司", name="某公司")
        await asyncio.sleep(0.1)
        return scheduled

    assert asyncio.run(main()) is False
    assert search.calls == []  # 7 天内查过，不再查


def test_prefetcher_high_risk_auto_blacklists(tmp_db):
    from job.models.blacklist import CompanyBlacklistRow

    search, reviewer = FakeSearcher(), FakeReviewer()
    prefetcher = CompanyPrefetcher(lambda: _checker(search, reviewer))

    async def main():
        prefetcher.schedule(platform="boss", brand_id="地雷科技", name="地雷科技")
        await asyncio.sleep(0.2)

    asyncio.run(main())
    # 高风险自动进黑名单，按公司名兜底匹配
    assert CompanyBlacklistRow.blocked("任意外部ID", "地雷科技") is not None


def test_prefetcher_swallows_search_failures(tmp_db):
    """搜索全失败的预查只记日志，不影响投递主流程。"""
    prefetcher = CompanyPrefetcher(lambda: _checker(FakeSearcher(fail=True), FakeReviewer()))

    async def main():
        scheduled = prefetcher.schedule(platform="boss", brand_id="某公司", name="某公司")
        await asyncio.sleep(0.2)
        return scheduled

    assert asyncio.run(main()) is True


def test_custom_resume_save_and_load(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("job.boss.resume_tailor.CUSTOM_DIR", tmp_path / "custom")
    assert load_saved_resume("boss", "boss:j1") is None
    path = save_custom_resume("boss", "boss:j1", "定制简历正文")
    assert path is not None and path.is_file()
    assert load_saved_resume("boss", "boss:j1") == "定制简历正文"
    assert save_custom_resume("boss", "boss:j1", "") is None  # 空内容不落盘


def test_company_checker_blacklists_high_risk(tmp_db):
    """手动「查企业」与后台预查共用：高风险自动进黑名单。"""
    checker = _checker(FakeSearcher(), FakeReviewer())
    key = asyncio.run(
        checker.check_company(platform="boss", brand_id="雷区公司", name="雷区公司")
    )
    assert key == "雷区公司"
    from job.models.blacklist import CompanyBlacklistRow

    assert CompanyBlacklistRow.blocked("", "雷区公司") is not None


def test_company_targets_dedupes_and_skips_fresh(tmp_db):
    """批量查企业：同一家公司只查一次，7 天内查过的跳过。"""
    from job.api.runtime import Runtime
    from job.boss.jobs import Job
    from job.models.job import JobRow

    for jid, company, brand in (
        ("c1", "甲公司", "b1"),
        ("c2", "甲公司", "b1"),  # 与 c1 同一家公司
        ("c3", "乙公司", "b2"),
        ("c4", "丙公司", ""),  # 没有公司 ID，用名字兜底
    ):
        JobRow.record(
            Job.model_validate(
                {"job_id": jid, "title": "岗位", "company": company, "brand_id": brand}
            )
        )
    # 甲公司刚查过，不该再排进批量任务
    CompanyRow.save(
        "b1", name="甲公司", full_name="", info={}, hits=[], risk="low", summary="", points=[]
    )

    targets = Runtime._company_targets(["c1", "c2", "c3", "c4"])
    assert [t["brand_id"] for t in targets] == ["b2", ""]
    assert [t["name"] for t in targets] == ["乙公司", "丙公司"]


def test_company_targets_empty_uids_returns_nothing(tmp_db):
    """不再扫全库补历史数据：没传岗位就没有要查的公司。"""
    from job.api.runtime import Runtime
    from job.boss.jobs import Job
    from job.models.job import JobRow

    for jid, company, brand in (("a1", "甲公司", "b1"), ("a2", "乙公司", "b2")):
        JobRow.record(
            Job.model_validate(
                {"job_id": jid, "title": "岗位", "company": company, "brand_id": brand}
            )
        )
    CompanyRow.save(
        "b1", name="甲公司", full_name="", info={}, hits=[], risk="low", summary="", points=[]
    )

    assert Runtime._company_targets([]) == []


def test_company_scan_requires_uids(tmp_db):
    """批量查企业必须带岗位：空的 uids 直接报错，不再回退到全库。"""
    from job.api.runtime import ApiError, Runtime

    runtime = Runtime()
    with pytest.raises(ApiError):
        runtime.company_scan([])


def test_record_prefetches_company(tmp_db):
    """岗位入库（进入岗位管理）时就把公司交给后台预查。"""
    from job.boss.jobs import Job
    from job.platforms.engine import DeliveryEngine

    class FakePrefetcher:
        def __init__(self) -> None:
            self.scheduled: list[tuple[str, str, str]] = []

        def schedule(self, *, platform, brand_id, name, link=""):
            self.scheduled.append((platform, brand_id, name))
            return True

    prefetcher = FakePrefetcher()
    engine = DeliveryEngine(
        platform="boss", daily_limit=10, stop=asyncio.Event(), company_prefetcher=prefetcher
    )
    engine.record(
        Job.model_validate(
            {"job_id": "p1", "title": "岗位", "company": "入库公司", "brand_id": "pb1"}
        ),
        suitable=True,
    )

    assert prefetcher.scheduled == [("boss", "pb1", "入库公司")]
