"""多城市轮换：一个城市看完自动换下一个，遇到停止 / 上限 / 登录就结束。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from job import models as models_pkg
from job.boss.jobs import Job, JobScraper
from job.models import init_db, reset_engine


class FakePage:
    def on(self, *_args) -> None:
        pass

    def remove_listener(self, *_args) -> None:
        pass


class FakeSession:
    user_data_dir = Path("profile")

    async def page(self) -> FakePage:
        return FakePage()


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


def _run(states: dict[str, str]) -> tuple[list[str], list[Job], str, list[str]]:
    """按城市返回预设状态，记录访问过的 URL 与日志。

    真实 URL 构建被替换成「城市名即 URL」，fake_city 据此映射预设状态。
    """
    from unittest.mock import patch

    scraper = JobScraper(FakeSession())  # type: ignore[arg-type]
    visited: list[str] = []
    logs: list[str] = []

    async def fake_city(_page, url, jobs, _on_job) -> str:
        visited.append(url)
        jobs.append(Job(job_id=url, title="AI 工程师", company="某公司"))
        return states[url]

    async def on_log(_level: str, text: str) -> None:
        logs.append(text)

    scraper._search_city = fake_city  # type: ignore[method-assign]
    targets = [(city, city) for city in states]
    with patch("job.boss.jobs.SearchUrl") as fake_search_url:
        fake_search_url.build.side_effect = lambda _config, city, _kw=None: city
        jobs, state = asyncio.run(scraper.search(targets, on_log=on_log))
    return visited, jobs, state, logs


def test_switches_to_next_city_when_list_ends(tmp_db):
    visited, jobs, state, logs = _run({"广州": "done", "深圳": "done", "惠州": "done"})
    assert visited == ["广州", "深圳", "惠州"]
    assert len(jobs) == 3 and state == "done"
    # 日志带前缀，用拼接后的全文匹配
    assert "开始搜索深圳的岗位" in "".join(logs)
    assert "开始搜索惠州的岗位" in "".join(logs)


def test_resumes_from_checkpoint(tmp_db):
    """断点续采：看完的城市记进进度，重开时自动跳过，全部看完清除断点。

    进度条目按「关键词@城市」记录（多关键词各跑一轮）。
    """
    import json

    from job.models.setting import SettingRow
    from job.platforms import checkpoint

    scraper = JobScraper(FakeSession())  # type: ignore[arg-type]
    targets = [("广州", "gz"), ("深圳", "sz")]
    sig = checkpoint.sig_of(targets, [""])
    key = checkpoint.key_of("boss")

    async def fake_city(_page, url, jobs, _on_job) -> str:
        jobs.append(Job(job_id=url, title="AI 工程师", company="某公司"))
        return "done"

    scraper._search_city = fake_city  # type: ignore[method-assign]
    asyncio.run(scraper.search(targets))
    assert SettingRow.get_many((key,)).get(key, "") == ""

    # 第一轮跑完会把断点清掉；模拟中途看完「@广州」之后的状态
    SettingRow.put_many(
        {
            key: json.dumps(
                {"sig": sig, "done": ["@广州"], "updated_at": "2026-01-01T00:00:00"}
            )
        }
    )
    visited, jobs, state, logs = _run({"广州": "done", "深圳": "done"})
    assert visited == ["深圳"]
    assert len(jobs) == 1 and state == "done"
    assert "断点续采：@广州上次已看完，自动跳过" in "".join(logs)

    # 换了城市序列，旧断点作废
    SettingRow.put_many(
        {
            key: json.dumps(
                {"sig": "北京|上海", "done": ["北京"], "updated_at": "2026-01-01T00:00:00"}
            )
        }
    )
    visited, _jobs, state, _ = _run({"广州": "done", "深圳": "done"})
    assert visited == ["广州", "深圳"] and state == "done"


@pytest.mark.parametrize("stop", ["stopped", "limit", "need_login"])
def test_stops_rotation_on_non_done_state(tmp_db, stop: str):
    visited, jobs, state, _ = _run({"广州": "done", "深圳": stop, "惠州": "done"})
    assert visited == ["广州", "深圳"]
    assert len(jobs) == 2 and state == stop
