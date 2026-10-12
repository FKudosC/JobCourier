"""Tests for salary parsing and range fit."""

from __future__ import annotations

import pytest

from job.boss.salary import SalaryRange, bonus_months, fit_summary, parse


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        ("15-25K", 15, 25),
        ("15K-25K", 15, 25),
        ("1-2万", 10, 20),
        ("8千-1.2万", 8, 12),
        ("15-25K·14薪", 15, 25),
        ("8千以上", 8, 12),
        ("20K+", 20, 30),
        ("20-40万/年", 200 / 12, 400 / 12),
        ("30万年薪", 25, 25),
    ],
)
def test_parse(text: str, low: float, high: float):
    got = parse(text)
    assert got is not None
    assert got.low == pytest.approx(low, abs=0.01)
    assert got.high == pytest.approx(high, abs=0.01)


@pytest.mark.parametrize("text", ["", "面议", "300-500元/天", "薪资面议", "1.5万以上"])
def test_parse_unparsable(text: str):
    # 「1.5万以上」其实是可解析的「以上」档；单独验证
    if text == "1.5万以上":
        assert parse(text) == SalaryRange(15, 22.5)
        return
    assert parse(text) is None


def test_intersect_contains_endpoints():
    a = SalaryRange(15, 25)
    b = SalaryRange(20, 30)
    assert a.intersect(b) == SalaryRange(20, 25)
    assert a.intersect(SalaryRange(30, 40)) is None


def test_bonus_months():
    assert bonus_months("15-25K·14薪") == 14
    assert bonus_months("15-25K") == 12
    assert bonus_months("13薪·15-25K") == 13
    assert bonus_months("") == 12


def test_fit_summary_intersection():
    expected = SalaryRange(18, 30)
    text = fit_summary("15-25K", expected)
    assert "交集为 18K-25K" in text


def test_fit_summary_no_overlap():
    expected = SalaryRange(40, 60)
    text = fit_summary("15-25K", expected)
    assert "没有交集" in text and "15K-25K" in text


def test_fit_summary_unparsable_or_absent():
    assert "信息不足" in fit_summary("面议", SalaryRange(18, 30))
    assert "不要臆测" in fit_summary("薪资详谈", SalaryRange(18, 30))


def test_fit_summary_without_expected():
    assert "没有设定" in fit_summary("15-25K", None)
