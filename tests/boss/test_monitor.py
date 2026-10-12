"""自动回复监控的程序侧判断：拒绝快路径、简历请求兜底、跟进提醒。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job import models as models_pkg
from job.boss.chat import detect_rejection, detect_resume_request
from job.models import init_db, reset_engine
from job.models.chat import ChatMessage, ChatMessageRow, ChatStatusRow
from job.models.pending import ReplyPendingRow

# --- 程序侧意图分类 ---


@pytest.mark.parametrize(
    "text",
    [
        "看了您的简历，觉得和岗位不太匹配，感谢投递",
        "不好意思，这个岗位已经招满了",
        "暂时不考虑，祝求职顺利",
        "我们先放入人才库了",
    ],
)
def test_rejection_detected(text: str):
    assert detect_rejection(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "您觉得自己的优势是什么？",
        "方便聊聊吗，我们这边是做 AI 应用的，岗位职责包括……" + "很长" * 60,
        "您之前做过大模型相关的项目吗，和岗位要求不符的部分您怎么看？",
        "您好，看了你的经历很感兴趣",
        "",
    ],
)
def test_rejection_not_detected(text: str):
    assert detect_rejection(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "方便发一份简历到我的邮箱吗",
        "我想要一份您的附件简历",
        "麻烦把简历发我一下",
    ],
)
def test_resume_request_detected(text: str):
    assert detect_resume_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "附件简历请求已发送",
        "已同意您的附件简历请求",
        "您好，我们聊聊岗位情况",
    ],
)
def test_resume_request_not_detected(text: str):
    assert detect_resume_request(text) is False


# --- 待确认回复队列 ---


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


def test_pending_create_replaces_open_ones(tmp_db):
    first = ReplyPendingRow.create(
        boss_id="b1", job_uid="j1", hr_name="张女士", company="某科技", text="你好", outcome="continue"
    )
    second = ReplyPendingRow.create(
        boss_id="b1", job_uid="j1", hr_name="张女士", company="某科技", text="再想想", outcome="continue"
    )
    items = ReplyPendingRow.list_dicts()
    assert len(items) == 1
    assert items[0]["id"] == second.id and items[0]["text"] == "再想想"
    assert ReplyPendingRow.approved() == []
    assert first.status == "pending"


def test_pending_decide_and_send(tmp_db):
    row = ReplyPendingRow.create(
        boss_id="b1", job_uid="j1", hr_name="张女士", company="某科技", text="好的", outcome="continue"
    )
    assert ReplyPendingRow.decide(row.id, "approved") is True
    assert [r.id for r in ReplyPendingRow.approved()] == [row.id]
    ReplyPendingRow.mark_sent(row.id)
    assert ReplyPendingRow.approved() == []
    assert ReplyPendingRow.list_dicts() == []
    # 已关闭的不能再改
    assert ReplyPendingRow.decide(row.id, "dismissed") is False


def test_pending_failed_send_returns_to_open(tmp_db):
    row = ReplyPendingRow.create(
        boss_id="b1", job_uid="j1", hr_name="张女士", company="某科技", text="好的", outcome="continue"
    )
    ReplyPendingRow.decide(row.id, "approved")
    ReplyPendingRow.mark_sent(row.id, failed=True)
    # failed 不在待确认列表里，也不会再自动发送，避免死循环
    assert ReplyPendingRow.approved() == []
    assert ReplyPendingRow.list_dicts() == []


def test_pending_seen_for_persists_across_restarts(tmp_db):
    """同一条 HR 消息建过待确认回复（哪怕已被处理）就不再重复建。"""
    ReplyPendingRow.create(
        boss_id="b1", job_uid="j1", hr_name="张女士", company="某科技",
        text="好的", outcome="continue", last_mid="m-1",
    )
    assert ReplyPendingRow.seen_for("m-1") is True
    # 用户放弃后重启监控，同一条消息也不能再冒出来
    row = ReplyPendingRow.list_dicts()[0]
    ReplyPendingRow.decide(row["id"], "dismissed")
    assert ReplyPendingRow.list_dicts() == []
    assert ReplyPendingRow.seen_for("m-1") is True
    assert ReplyPendingRow.seen_for("m-2") is False
    assert ReplyPendingRow.seen_for("") is False


def test_pending_resolve_open_when_i_replied_manually(tmp_db):
    """我方已发出新消息（如在 BOSS 里手动回复）后，挂着的待确认回复自动了结。"""
    ReplyPendingRow.create(
        boss_id="b1", job_uid="j1", hr_name="张女士", company="某科技",
        text="好的", outcome="continue", last_mid="m-1",
    )
    assert len(ReplyPendingRow.list_dicts()) == 1
    assert ReplyPendingRow.resolve_open("b1") == 1
    assert ReplyPendingRow.list_dicts() == []
    # 已了结的不影响 seen_for 判断
    assert ReplyPendingRow.seen_for("m-1") is True


def test_conversations_hides_blocked_by_default(tmp_db):
    """已拉黑的会话默认不在消息页出现，明确按「已拉黑」筛选时才可见。"""
    _record("b1", [(True, "聊聊岗位")], hours_ago=1)
    assert len(ChatMessageRow.conversations()) == 1
    ChatStatusRow.mark("b1", "blocked", source="auto")
    assert ChatMessageRow.conversations() == []
    assert ChatMessageRow.conversations(status="无状态") == []
    blocked = ChatMessageRow.conversations(status="已拉黑")
    assert [c["boss_id"] for c in blocked] == ["b1"]


def test_conversations_fall_back_to_message_company(tmp_db):
    """会话没关联到岗位（如 HR 主动沟通）时，公司/职位用消息里记录的兜底。"""
    ChatMessageRow.record_new(
        [ChatMessage(mid="m1", from_hr=True, text="你好，我们这个岗位在招")],
        job_uid="",
        boss_id="b9",
        hr_name="王先生",
        company="某某科技",
        job_title="渗透测试工程师",
    )
    items = ChatMessageRow.conversations()
    assert len(items) == 1
    assert items[0]["company"] == "某某科技"
    assert items[0]["title"] == "渗透测试工程师"


def test_system_cards_not_recorded_and_purged(tmp_db):
    """BOSS 系统卡片（竞争力 PK 报告）不是 HR 说话：不入库，历史脏数据可清理。"""
    fresh = ChatMessageRow.record_new(
        [
            ChatMessage(mid="m1", from_hr=True, text="你好，方便聊聊吗"),
            ChatMessage(
                mid="m2",
                from_hr=True,
                text="你与该职位竞争者PK情况 共 20 人投递，你超过 80% 竞争者",
            ),
        ],
        job_uid="",
        boss_id="b1",
        hr_name="张女士",
    )
    assert [m.mid for m in fresh] == ["m1"]
    _record("b2", [(True, "竞争者PK情况 共 30 人投递")], hours_ago=1)
    assert ChatMessageRow.known_boss_ids() == {"b1", "b2"}
    assert ChatMessageRow.purge_system_cards() == 1
    assert ChatMessageRow.known_boss_ids() == {"b1"}


# --- 未入库会话同步 ---


def test_known_boss_ids(tmp_db):
    """监控按「库里没有这个 HR 的消息」补读会话：打招呼后的会话能被认出来。"""
    assert ChatMessageRow.known_boss_ids() == set()
    _record("b1", [(False, "你好，看了你的简历很感兴趣")], hours_ago=1)
    _record("b2", [(True, "什么时候方便聊聊")], hours_ago=2)
    assert ChatMessageRow.known_boss_ids() == {"b1", "b2"}


# --- 跟进提醒 ---


def _record(boss_id: str, texts: list[tuple[bool, str]], hours_ago: float) -> None:
    rows = []
    for i, (from_hr, text) in enumerate(texts, 1):
        row = ChatMessageRow(
            mid=f"{boss_id}-{i}",
            boss_id=boss_id,
            job_uid="",
            hr_name="张女士",
            from_hr=from_hr,
            text=text,
            created_at=datetime.now(UTC) - timedelta(hours=hours_ago) + timedelta(minutes=i),
        )
        rows.append(row)
    with models_pkg.db_session() as session:
        for row in rows:
            session.add(row)
        session.commit()


def test_follow_up_only_for_my_last_message(tmp_db):
    _record("b1", [(True, "你好"), (False, "期待沟通")], hours_ago=72)
    _record("b2", [(False, "已回复"), (True, "好的，明天聊")], hours_ago=72)
    _record("b3", [(False, "久等了")], hours_ago=1)
    ChatStatusRow.mark("b1", "hr_rejected", source="manual")

    items = ChatMessageRow.conversations(follow_up_hours=48)
    marks = {i["boss_id"]: i["follow_up"] for i in items}
    assert marks["b1"] is False  # 会话已结束的不提醒
    assert marks["b2"] is False  # 最后一条是 HR 发的，在等 HR
    assert marks["b3"] is False  # 还没到 48 小时


def test_follow_up_hours_zero_disables(tmp_db):
    _record("b1", [(False, "期待沟通")], hours_ago=72)
    items = ChatMessageRow.conversations(follow_up_hours=0)
    assert items[0]["follow_up"] is False
