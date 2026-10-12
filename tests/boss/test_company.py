"""查企业：公司 ID、工商信息整理、网上搜索、AI 评估与整体流程。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from job import models as models_pkg
from job.boss.company import (
    CompanyChecker,
    CompanyPage,
    CompanyReviewer,
    CompanySearch,
    CompanyVerdict,
    SearchHit,
    company_aliases,
    filter_relevant_hits,
)
from job.boss.jobs import Job
from job.models import init_db, reset_engine
from job.models.company import CompanyRow
from job.models.job import JobRow


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


@pytest.mark.parametrize(
    ("href", "brand_id"),
    [
        ("/gongsi/a829a7bf61d7263c1nx_3dS0GVM~.html", "a829a7bf61d7263c1nx_3dS0GVM~"),
        (
            "https://www.zhipin.com/gongsi/0d95f6f97cab53ac1Hdz0t61.html?ka=x",
            "0d95f6f97cab53ac1Hdz0t61",
        ),
        ("/gongsi/job/a829a7bf61d7263c1nx_3dS0GVM~.html", ""),
        ("https://www.zhipin.com/gongsi/", ""),
    ],
)
def test_brand_id_from_link(href: str, brand_id: str):
    assert CompanyPage.brand_id_from(href) == brand_id


def test_parse_business_pairs():
    pairs = [
        ["企业名称：", " 广州昊誉信息科技有限公司 "],
        ["曾用名：", "-"],
        ["所属行业：", ""],
        ["经营状态：", "在营"],
    ]
    assert CompanyPage.parse_business(pairs) == {
        "企业名称": "广州昊誉信息科技有限公司",
        "经营状态": "在营",
    }


def test_search_runs_all_queries_and_dedupes():
    asked: list[str] = []

    def text(query: str) -> list[dict[str, str]]:
        asked.append(query)
        if "失信" in query:
            raise RuntimeError("限流")
        return [
            {"title": "同一篇", "href": "https://a.com/1", "body": "摘要"},
            {
                "title": f"{query} 独有",
                "href": f"https://b.com/{query}",
                "body": "",
            },
        ]

    hits = asyncio.run(CompanySearch(text=text).search("广州昊誉信息科技有限公司"))
    # 三组并发搜索都会发起（失败的组也计入）
    assert len(asked) == len(CompanySearch.QUERIES)
    assert all(q.startswith("广州昊誉信息科技有限公司 ") for q in asked)
    assert [h.href for h in hits].count("https://a.com/1") == 1
    assert len(hits) == 3


def test_search_all_failed_raises():
    def text(_query: str) -> list[dict[str, str]]:
        raise RuntimeError("网络不通")

    with pytest.raises(RuntimeError, match="搜索失败"):
        asyncio.run(CompanySearch(text=text).search("某公司"))


def test_parse_so_skips_non_web_results():
    """360 结果页里的图片 / 相关搜索不是网页结果，要过滤掉。"""
    body = """
    <ul>
      <li class="res-list"><h3><a href="https://www.so.com/link?m=abc">某某公司 欠薪 - 天眼查</a></h3>
        <p class="res-desc"> 被列为被执行人 </p></li>
      <li class="res-list"><h3><a href="https://image.so.com/i?src=imageonebox&amp;q=x">某某公司_360图片</a></h3></li>
      <li class="res-list"><h3><a href="https://www.so.com/s?q=相关">相关搜索</a></h3></li>
      <li class="res-list"><h3><a href="https://b.com/2">另一条报道</a></h3></li>
    </ul>
    """
    hits = CompanySearch.parse_so(body)
    assert [h["href"] for h in hits] == ["https://www.so.com/link?m=abc", "https://b.com/2"]
    assert hits[0]["body"] == "被列为被执行人"


def test_parse_so_respects_limit():
    items = "".join(
        f'<li class="res-list"><h3><a href="https://b.com/{i}">标题 {i}</a></h3></li>'
        for i in range(5)
    )
    assert len(CompanySearch.parse_so(f"<ul>{items}</ul>", limit=3)) == 3


def test_web_uses_fallback_source(monkeypatch: pytest.MonkeyPatch):
    """360 搜索失败时自动换 ddgs。"""
    search = CompanySearch()
    calls: list[str] = []

    def boom(_query: str) -> list[dict[str, str]]:
        calls.append("so")
        raise RuntimeError("连接超时")

    def ok(_query: str) -> list[dict[str, str]]:
        calls.append("ddgs")
        return [{"title": "t", "href": "https://x.com", "body": ""}]

    monkeypatch.setattr(search, "_so", boom)
    monkeypatch.setattr(search, "_ddgs", ok)
    assert search._web("某公司 欠薪")[0]["href"] == "https://x.com"
    assert calls == ["so", "ddgs"]


def test_web_all_sources_fail_reports_reason(monkeypatch: pytest.MonkeyPatch):
    """全部搜索源都失败时，报错要带上各源原因，而不是笼统的「请稍后重试」。"""
    search = CompanySearch()

    def boom(_query: str) -> list[dict[str, str]]:
        raise RuntimeError("连接超时")

    monkeypatch.setattr(search, "_so", boom)
    monkeypatch.setattr(search, "_ddgs", boom)
    with pytest.raises(RuntimeError, match="360 搜索不可用.*ddgs不可用"):
        search._web("某公司")


def test_reviewer_prompt_has_name_info_and_hits():
    prompt = CompanyReviewer()._prompt(
        "昊誉",
        "广州昊誉信息科技有限公司",
        {"经营状态": "在营"},
        [
            SearchHit(
                title="欠薪传闻",
                href="https://a.com",
                body="拖欠工资",
                query="拖欠工资",
            )
        ],
    )
    assert "广州昊誉信息科技有限公司" in prompt
    assert "经营状态：在营" in prompt
    assert "欠薪传闻" in prompt and "https://a.com" in prompt
    assert "未取得工商信息" in CompanyReviewer()._prompt("昊誉", "", {}, [])


class FakePage:
    def __init__(
        self, brand_id: str = "b1", info: dict[str, str] | None = None
    ) -> None:
        self._brand_id, self._info = brand_id, info or {}
        self.looked_up: list[str] = []
        self.opened: list[str] = []

    async def brand_id(self, job_link: str) -> str:
        self.looked_up.append(job_link)
        return self._brand_id

    async def business(self, brand_id: str) -> dict[str, str]:
        self.opened.append(brand_id)
        return self._info


class FakeSearch:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def search(
        self, name: str, on_step: Callable[[str], Awaitable[None]] | None = None
    ) -> list[SearchHit]:
        self.names.append(name)
        return [
            SearchHit(title="评价", href="https://a.com", body="还行", query="员工评价")
        ]


class FakeReviewer:
    async def review(
        self, name: str, full_name: str, info: dict[str, str], hits: list[SearchHit]
    ) -> CompanyVerdict:
        return CompanyVerdict(risk="low", summary="未见负面", points=[])


def _steps() -> tuple[list[str], Callable[[str], Awaitable[None]]]:
    steps: list[str] = []

    async def on_step(text: str) -> None:
        steps.append(text)

    return steps, on_step


def _record(**kw: str) -> None:
    JobRow.record(
        Job.model_validate(
            {"job_id": "j1", "title": "AI 工程师", "company": "昊誉", **kw}
        )
    )


def _job() -> dict[str, Any]:
    job = JobRow.get_dict("j1")
    assert job is not None
    return job


def _saved(brand_id: str) -> dict[str, Any]:
    saved = CompanyRow.get_dict(brand_id)
    assert saved is not None
    return saved


def test_checker_reads_searches_reviews_and_saves(tmp_db):
    _record(brand_id="b1")
    page, search = FakePage(info={"企业名称": "广州昊誉信息科技有限公司"}), FakeSearch()
    steps, on_step = _steps()
    brand_id = asyncio.run(
        CompanyChecker(page, search, FakeReviewer()).check("j1", on_step)
    )

    assert brand_id == "b1"
    assert (page.looked_up, page.opened) == ([], ["b1"])
    assert search.names == ["广州昊誉信息科技有限公司"]
    saved = _saved("b1")
    assert (saved["risk"], saved["summary"], saved["name"]) == (
        "low",
        "未见负面",
        "昊誉",
    )
    assert steps


def test_checker_works_without_ai(tmp_db):
    _record(brand_id="b9")
    search = FakeSearch()
    _, on_step = _steps()
    asyncio.run(CompanyChecker(FakePage(), search, None).check("j1", on_step))

    assert search.names == ["昊誉"]
    saved = _saved("b9")
    assert saved["risk"] == "unknown"
    assert "AI 服务" in saved["summary"]


def test_checker_looks_up_missing_brand_id(tmp_db):
    _record()
    page = FakePage(brand_id="b2")
    _, on_step = _steps()
    assert (
        asyncio.run(CompanyChecker(page, FakeSearch(), None).check("j1", on_step))
        == "b2"
    )

    assert (page.looked_up, page.opened) == ([_job()["link"]], ["b2"])
    assert _job()["brandId"] == "b2"


def test_checker_without_company_page_raises(tmp_db):
    _record()
    _, on_step = _steps()
    with pytest.raises(RuntimeError, match="公司主页"):
        asyncio.run(
            CompanyChecker(FakePage(brand_id=""), FakeSearch(), None).check(
                "j1", on_step
            )
        )


def test_company_aliases_strips_city_and_suffix():
    """字号 = 全称去掉行政区前缀与公司后缀，用于判断搜索结果是否相关。"""
    aliases = company_aliases("亿西科技", "上海亿西信息科技有限公司")
    assert "亿西科技" in aliases
    assert "上海亿西信息科技有限公司" in aliases
    assert "亿西信息科技" in aliases


def test_filter_relevant_hits_drops_unrelated_news():
    """搜负面关键词常混进同音/行业通用新闻，只保留含公司字号的结果。"""
    aliases = company_aliases("亿西科技", "上海亿西信息科技有限公司")
    hits = [
        SearchHit(title="亿西科技被曝欠薪", href="https://a.com", body="员工称拖欠工资"),
        SearchHit(title="西贝爆大雷！欠薪寒人心", href="https://b.com", body="餐饮连锁"),
        SearchHit(title="沃尔沃旗下 NOVO Energy 裁员 75 人", href="https://c.com"),
        SearchHit(title="创业邦 - DeepSeek 一口气扩招 150 人", href="https://d.com"),
    ]
    kept = filter_relevant_hits(hits, aliases)
    assert [h.href for h in kept] == ["https://a.com"]


def test_filter_relevant_hits_without_aliases_keeps_all():
    """拿不到公司名时不误杀，原样保留。"""
    hits = [SearchHit(title="任意报道", href="https://a.com")]
    assert filter_relevant_hits(hits, []) == hits
