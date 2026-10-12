"""公司黑名单：添加 / 移出 / 匹配（按 ID 与按名字兜底）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from job import models as models_pkg
from job.models import init_db, reset_engine
from job.models.blacklist import CompanyBlacklistRow


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


def test_add_requires_id_or_name(tmp_db):
    assert CompanyBlacklistRow.add(brand_id="", name="  ") is False
    assert CompanyBlacklistRow.add(brand_id="b1", name="某科技") is True
    assert CompanyBlacklistRow.add(name="外包之家", reason="手动") is True


def test_blocked_matches_by_id_then_name(tmp_db):
    CompanyBlacklistRow.add(brand_id="b1", name="某科技", reason="欠薪")
    CompanyBlacklistRow.add(name="外包之家", reason="手动拉黑")
    assert CompanyBlacklistRow.blocked("b1", "某科技") == "欠薪"
    # ID 没查过时按公司名兜底
    assert CompanyBlacklistRow.blocked("b2", "外包之家") == "手动拉黑"
    assert CompanyBlacklistRow.blocked("b3", "正常公司") is None
    assert CompanyBlacklistRow.blocked("", "  ") is None


def test_remove(tmp_db):
    CompanyBlacklistRow.add(brand_id="b1", name="某科技")
    assert CompanyBlacklistRow.remove("b1") is True
    assert CompanyBlacklistRow.remove("b1") is False
    assert CompanyBlacklistRow.blocked("b1", "某科技") is None


def test_list_dicts_has_labels(tmp_db):
    CompanyBlacklistRow.add(brand_id="b1", name="某科技", reason="高风险", source="ai")
    items = CompanyBlacklistRow.list_dicts()
    assert len(items) == 1
    assert items[0]["source_label"] == "查企业评估"
    assert items[0]["brand_id"] == "b1" and items[0]["reason"] == "高风险"
