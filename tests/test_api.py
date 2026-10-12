"""API 层测试：TestClient 打真实路由，内核逻辑由既有测试覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from job import models as models_pkg
from job.api.app import app
from job.models import init_db, reset_engine
from job.models.chat import ChatMessageRow, ChatStatusRow
from job.models.job import JobRow


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    with TestClient(app) as c:
        yield c
    reset_engine()


def test_runtime_snapshot_and_config(client: TestClient):
    snap = client.get("/api/runtime").json()
    assert snap["boss_state"] == "空闲" and snap["busy"] is False
    cfg = client.get("/api/config").json()
    assert "keywords" in cfg and "options" in cfg and "blacklist" in cfg
    assert cfg["options"]["cities"]


def test_search_config_validation_and_save(client: TestClient):
    body = {
        "query": "",
        "keywords": ["AI 工程师"],
        "cities": ["广州"],
        "experience": [],
        "education": [],
        "funding": [],
        "scale": [],
        "job_type_label": "全职",
        "salary_label": "不限",
        "pace_label": "正常",
        "pace_params": {"read_min": 3, "read_max": 8, "scroll_min": 2, "scroll_max": 5},
        "scam_block": True,
        "resume_match": False,
        "score_filter": False,
        "min_score": 70,
        "salary_min": "10",
        "salary_max": "20",
        "resume_path": "",
        "resume_text": "",
    }
    assert client.put("/api/config/search", json=body).status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["keywords"] == ["AI 工程师"] and cfg["cities"] == ["广州"]
    assert cfg["min_score"] == 70 and cfg["scam_block"] is True
    # 预设档：明细参数按预设归一化（与原 ConfigState.set_pace 一致）
    assert cfg["pace"]["params"] == {
        "read_min": 3,
        "read_max": 8,
        "scroll_min": 2,
        "scroll_max": 5,
        "rest_every": 15,
        "rest_min": 30,
        "rest_max": 60,
    }
    # 自定义档：明细参数原样保存；非法参数被拒
    custom = {
        **body,
        "pace_label": "自定义",
        "pace_params": {
            "read_min": 6,
            "read_max": 15,
            "scroll_min": 4,
            "scroll_max": 8,
            "rest_every": 10,
            "rest_min": 60,
            "rest_max": 120,
        },
    }
    assert client.put("/api/config/search", json=custom).status_code == 200
    assert client.get("/api/config").json()["pace"]["params"]["read_min"] == 6
    bad_pace = {
        **custom,
        "pace_params": {**custom["pace_params"], "read_min": 9999},
    }
    assert client.put("/api/config/search", json=bad_pace).status_code == 400
    # 越界校验
    bad = {**body, "min_score": 101}
    assert client.put("/api/config/search", json=bad).status_code == 400


def test_keyword_list_ops(client: TestClient):
    added = client.post("/api/config/list-item", json={"field": "keywords", "value": "新关键词"}).json()
    assert "新关键词" in added["items"]
    removed = client.delete("/api/config/list-item/keywords/新关键词").json()
    assert "新关键词" not in removed["items"]


def test_blacklist_crud(client: TestClient):
    rows = client.post("/api/blacklist", json={"name": "某公司"}).json()
    assert any(r["name"] == "某公司" for r in rows)
    key = next(r["brand_id"] for r in rows if r["name"] == "某公司")
    rows = client.delete(f"/api/blacklist/{key}").json()
    assert all(r["name"] != "某公司" for r in rows)


def test_jobs_flow(client: TestClient):
    from job.boss.jobs import Job

    JobRow.record(Job(job_id="j1", title="AI 工程师", company="某公司"), applied=True, score=80)
    rows = client.get("/api/jobs", params={"page": 1, "page_size": 10}).json()
    assert rows["total"] >= 1 and rows["rows"][0]["title"] == "AI 工程师"
    uid = rows["rows"][0]["uid"]
    detail = client.get(f"/api/jobs/{uid}").json()
    assert detail["company"] == "某公司"
    assert client.post(f"/api/jobs/{uid}/applied").status_code == 200
    assert client.delete(f"/api/jobs/{uid}").status_code == 200
    assert client.get(f"/api/jobs/{uid}").status_code == 404


def test_jobs_sort_param(client: TestClient):
    from job.boss.jobs import Job

    JobRow.record(Job(job_id="s1", title="低分", company="A", salary="10K"), score=40)
    JobRow.record(Job(job_id="s2", title="高分", company="B", salary="30K"), score=90)

    desc = client.get("/api/jobs", params={"sort": "score_desc"}).json()["rows"]
    asc = client.get("/api/jobs", params={"sort": "score_asc"}).json()["rows"]
    money = client.get("/api/jobs", params={"sort": "salary_desc"}).json()["rows"]

    assert [r["title"] for r in desc] == ["高分", "低分"]
    assert [r["title"] for r in asc] == ["低分", "高分"]
    assert [r["title"] for r in money] == ["高分", "低分"]


def test_messages_conversations_and_status(client: TestClient):
    with models_pkg.db_session() as session:
        session.add(
            ChatMessageRow(mid="m1", boss_id="b1", hr_name="张女士", from_hr=True, text="你好")
        )
        session.commit()
    conv = client.get("/api/messages/conversations").json()
    assert conv["conversations"][0]["hr_name"] == "张女士"
    client.put("/api/messages/b1/status", json={"label": "有面试"})
    assert ChatStatusRow.statuses().get("b1") == "invited"
    client.put("/api/messages/b1/status", json={"label": "无"})
    assert ChatStatusRow.statuses().get("b1", "") == ""


def test_stats_bundle(client: TestClient):
    from job.boss.jobs import Job

    JobRow.record(Job(job_id="s1", title="AI 工程师", company="某公司"), applied=True)
    data = client.get("/api/stats").json()
    assert data["funnel"]["total"] >= 1
    assert len(data["daily"]) == 14 and len(data["buckets"]) == 5


def test_clear_logs_clears_and_broadcasts(client: TestClient):
    from job.api.app import runtime

    runtime.push_log("第一条日志")
    runtime.push_log("第二条日志")
    assert len(runtime.logs) >= 2

    sub = runtime.bus.subscribe()
    try:
        assert client.delete("/api/logs").status_code == 200
        assert len(runtime.logs) == 0
        assert sub.get_nowait() == {"type": "logs_cleared"}
    finally:
        runtime.bus.unsubscribe(sub)
