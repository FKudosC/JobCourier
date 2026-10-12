"""投递漏斗统计：从本地库做只读聚合，给数据页展示。

统计诚实规则：响应时效的中位数必须报告右删失（还在等的会话不计入分母），
样本量太小的分组不下结论（比率置空），转化率的分母是已投递而不是全部岗位。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlmodel import col, select

# 评分分档（左闭右开，最后一档闭）
SCORE_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("60 以下", 0, 60),
    ("60–69", 60, 70),
    ("70–79", 70, 80),
    ("80–89", 80, 90),
    ("90–100", 90, 101),
)
# 每档至少这么多已投递岗位才报面试率，样本太小不下结论
MIN_BUCKET_FOR_RATE = 5


def pct(value: int, total: int) -> str:
    """条宽百分比；零值为 0（前端条形自带 min_width 兜底）。"""
    if total <= 0 or value <= 0:
        return "0"
    return f"{value / total * 100:.2f}%"


def _db():
    from job.models import db_session

    return db_session()


def _today_midnight_utc() -> datetime:
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC)


def funnel() -> dict[str, int]:
    """岗位漏斗：看过 → 合适 → 已投递 → 有面试 → 已面试 → 通过；含被拒与婉拒。

    沟通状态只有 BOSS 直聘在使用（自动回复目前仅支持 BOSS）。
    """
    from job.models.chat import ChatStatusRow
    from job.models.job import JobRow

    with _db() as session:
        rows = session.exec(select(JobRow)).all()
        total = len(rows)
        applied = sum(1 for r in rows if r.applied)
        # 漏斗每级必须是上一级的子集：投递过的岗位一定过了「合适」这道门，
        # 即便投递后被自动评分补判为低分（``suitable`` 翻成 False），也仍算判为合适。
        suitable = sum(1 for r in rows if r.suitable or r.applied)
        status_rows = session.exec(
            select(ChatStatusRow.status, func.count()).group_by(ChatStatusRow.status)
        ).all()
    counts = {status: count for status, count in status_rows}
    interviews = sum(counts.get(s, 0) for s in ChatStatusRow.INTERVIEWS)
    return {
        "total": total,
        "suitable": suitable,
        "applied": applied,
        "interviews": interviews,
        "interview_done": counts.get("done", 0),
        "passed": counts.get("passed", 0),
        "rejected": counts.get("hr_rejected", 0),
        "declined": counts.get("declined", 0),
    }


def daily_applied(days: int = 14) -> list[dict]:
    """最近 ``days`` 天的每日投递量（老的不在前）；没有投递的日期补零。"""
    from job.models.job import JobRow

    since = _today_midnight_utc() - timedelta(days=days - 1)
    stmt = (
        select(JobRow.created_at)
        .where(col(JobRow.applied).is_(True), col(JobRow.created_at) >= since)
        .order_by(col(JobRow.created_at))
    )
    with _db() as session:
        created = session.exec(stmt).all()
    by_date: dict[str, int] = {}
    for value in created:
        day = value.replace(tzinfo=value.tzinfo or UTC).astimezone().strftime("%m-%d")
        by_date[day] = by_date.get(day, 0) + 1
    today = datetime.now().astimezone()
    return [
        {"day": (today - timedelta(days=days - 1 - offset)).strftime("%m-%d"),
         "count": by_date.get((today - timedelta(days=days - 1 - offset)).strftime("%m-%d"), 0)}
        for offset in range(days)
    ]


def status_distribution() -> list[dict]:
    """沟通状态分布：含「无状态」（有会话但还没标记过状态）。"""
    from job.models.chat import ChatMessageRow, ChatStatusRow

    with _db() as session:
        rows = session.exec(
            select(ChatStatusRow.status, func.count()).group_by(ChatStatusRow.status)
        ).all()
        total_chats = int(
            session.exec(select(func.count(func.distinct(col(ChatMessageRow.boss_id))))).one()
        )
    counts = {status: count for status, count in rows}
    items = [
        {"label": ChatStatusRow.LABELS[status], "count": count}
        for status, count in counts.items()
        if count and status in ChatStatusRow.LABELS
    ]
    if unmarked := max(0, total_chats - sum(counts.values())):
        items.insert(0, {"label": "无状态", "count": unmarked})
    return items


def response_times() -> dict:
    """HR 首次响应时长：我发出第一条消息 → HR 首次回复，中位数（小时）。

    右删失声明：还没收到回复的会话不计入中位数，但单独报数——
    只用已回复样本算出来的等待时长会偏短（幸存者偏差）。
    HR 先开口的会话不存在「我的等待」，不计入。
    """
    from job.models.chat import ChatMessageRow

    stmt = select(ChatMessageRow).order_by(col(ChatMessageRow.created_at), col(ChatMessageRow.mid))
    with _db() as session:
        rows = session.exec(stmt).all()
    by_boss: dict[str, list[ChatMessageRow]] = {}
    for row in rows:
        by_boss.setdefault(row.boss_id, []).append(row)

    hours: list[float] = []
    waiting = 0
    for messages in by_boss.values():
        first_mine = next((m for m in messages if not m.from_hr), None)
        if first_mine is None:
            continue  # HR 先开口的会话，没有「我的等待」
        first_hr = next(
            (m for m in messages if m.from_hr and m.text and m.created_at > first_mine.created_at),
            None,
        )
        if first_hr is None:
            waiting += 1
            continue
        hours.append((first_hr.created_at - first_mine.created_at).total_seconds() / 3600)

    if not hours:
        return {"median": None, "replied": 0, "waiting": waiting}
    hours.sort()
    n = len(hours)
    median = hours[n // 2] if n % 2 else (hours[n // 2 - 1] + hours[n // 2]) / 2
    return {"median": round(median, 1), "replied": n, "waiting": waiting}


def score_buckets() -> list[dict]:
    """评分校准：每个分数段的岗位数与「有面试」比率。

    比率分母是该分数段已投递的岗位；样本不足 ``MIN_BUCKET_FOR_RATE`` 的置空。
    """
    from job.models.chat import ChatMessageRow, ChatStatusRow
    from job.models.job import JobRow

    with _db() as session:
        scored = session.exec(
            select(JobRow.uid, JobRow.match_score, JobRow.applied).where(
                col(JobRow.match_score).is_not(None)
            )
        ).all()
        links = session.exec(
            select(ChatMessageRow.job_uid, ChatMessageRow.boss_id)
        ).all()
    chat_bosses: dict[str, str] = {}
    for job_uid, boss_id in links:
        if job_uid and boss_id:
            chat_bosses.setdefault(job_uid, boss_id)
    statuses = ChatStatusRow.statuses()

    out = []
    for label, low, high in SCORE_BUCKETS:
        jobs = [uid for uid, score, _ in scored if low <= (score or 0) < high]
        applied_jobs = [
            uid for uid, score, applied in scored if low <= (score or 0) < high and applied
        ]
        interviewed = sum(
            1
            for uid in applied_jobs
            if statuses.get(chat_bosses.get(uid, ""), "") in ChatStatusRow.INTERVIEWS
        )
        rate = (
            round(interviewed / len(applied_jobs) * 100)
            if len(applied_jobs) >= MIN_BUCKET_FOR_RATE
            else None
        )
        out.append(
            {
                "bucket": label,
                "jobs": len(jobs),
                "applied": len(applied_jobs),
                "interviewed": interviewed,
                "rate": rate,
            }
        )
    return out
