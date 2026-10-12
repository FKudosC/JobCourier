"""待确认回复：开启「发送前确认」后，AI 生成的回复先到这里，同意后才真正发出。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlmodel import Field, SQLModel, col, select


def _now() -> datetime:
    return datetime.now(UTC)


class ReplyPendingRow(SQLModel, table=True):
    """一条 AI 生成、等人确认的回复；自动回复运行中同意它会由程序自动发出。"""

    __tablename__ = "reply_pending"  # pyright: ignore[reportAssignmentType]

    # 状态：pending 待确认 / approved 已同意待发送 / dismissed 已放弃 / sent 已发出
    STATUS_LABELS: ClassVar[dict[str, str]] = {
        "pending": "待确认",
        "approved": "已同意",
        "dismissed": "已放弃",
        "sent": "已发出",
    }
    OPEN: ClassVar[frozenset[str]] = frozenset({"pending", "approved"})

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    boss_id: str = Field(index=True, description="HR 的加密 ID")
    job_uid: str = Field(default="", description="关联岗位主键（job.uid）")
    hr_name: str = Field(default="", description="HR 称呼，自动发送时用于找到会话")
    company: str = Field(default="", description="公司名，自动发送时用于找到会话")
    # 同一会话可能积压多条待确认，只发最新一条
    text: str = Field(default="", description="AI 生成的回复正文")
    # 触发这条回复的 HR 消息 ID：重启后据此认出已处理过的消息，不再重复建
    last_mid: str = Field(default="", index=True, description="触发消息的 BOSS 消息 ID")
    outcome: str = Field(default="continue", description="AI 判断的会话走向：continue / declined")
    send_resume: bool = Field(default=False, description="同意后发送时是否顺带发附件简历")
    status: str = Field(default="pending", index=True, description="状态，见 STATUS_LABELS")
    created_at: datetime = Field(default_factory=_now, index=True, description="生成时间（UTC）")
    updated_at: datetime = Field(default_factory=_now, description="最后更新时间（UTC）")

    @classmethod
    def create(
        cls,
        *,
        boss_id: str,
        job_uid: str,
        hr_name: str,
        company: str,
        text: str,
        outcome: str,
        send_resume: bool = False,
        last_mid: str = "",
    ) -> ReplyPendingRow:
        """为一次 AI 回复建待确认记录；该会话已有待确认的先作废，避免重复。"""
        from job.models import db_session

        with db_session() as session:
            for old in session.exec(
                select(cls).where(
                    cls.boss_id == boss_id,
                    col(cls.status).in_(cls.OPEN),
                )
            ).all():
                old.status = "dismissed"
                old.updated_at = _now()
                session.add(old)
            row = cls(
                boss_id=boss_id,
                job_uid=job_uid,
                hr_name=hr_name,
                company=company,
                text=text,
                outcome=outcome,
                send_resume=send_resume,
                last_mid=last_mid,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    @classmethod
    def seen_for(cls, last_mid: str) -> bool:
        """这条 HR 消息是否已经建过待确认回复（任何状态）。

        监控重启会清空内存里的已处理集合，不查库的话同一条消息会反复
        生成新的待确认回复，用户处理过又冒出来。
        """
        if not last_mid:
            return False
        from job.models import db_session

        with db_session() as session:
            return (
                session.exec(select(cls).where(cls.last_mid == last_mid)).first()
                is not None
            )

    @classmethod
    def resolve_open(cls, boss_id: str) -> int:
        """我方已经发出新消息（手动回复或自动发送）后，把未关闭的待确认回复了结。

        用户在 BOSS 里手动回过话时，待确认的旧回复继续挂着只会误导；
        自动发送路径先 mark_sent 再入库，不会走到这里。
        """
        from job.models import db_session

        with db_session() as session:
            rows = session.exec(
                select(cls).where(
                    cls.boss_id == boss_id,
                    col(cls.status).in_(cls.OPEN),
                )
            ).all()
            for row in rows:
                row.status = "dismissed"
                row.updated_at = _now()
                session.add(row)
            session.commit()
            return len(rows)

    @classmethod
    def decide(cls, row_id: str, status: str) -> bool:
        """人工决定一条待确认回复：approved / dismissed；记录不存在或已关闭返回 False。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, row_id)
            if row is None or row.status not in cls.OPEN:
                return False
            row.status = status
            row.updated_at = _now()
            session.add(row)
            session.commit()
            return True

    @classmethod
    def mark_sent(cls, row_id: str, *, failed: bool = False) -> None:
        """自动发送后的落账：成功置 sent，失败退回 pending 让人再决定。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, row_id)
            if row is None:
                return
            row.status = "failed" if failed else "sent"
            row.updated_at = _now()
            session.add(row)
            session.commit()

    @classmethod
    def approved(cls) -> list[ReplyPendingRow]:
        """已同意、等自动回复发出的记录（旧的在前）。"""
        from job.models import db_session

        stmt = (
            select(cls)
            .where(col(cls.status) == "approved")
            .order_by(col(cls.created_at), col(cls.id))
        )
        with db_session() as session:
            return list(session.exec(stmt).all())

    @classmethod
    def list_dicts(cls, *, status: str = "") -> list[dict[str, Any]]:
        """待确认列表给消息页展示：``status`` 为空取未关闭的（pending + approved）。"""
        from job.models import db_session
        from job.models.job import JobRow

        stmt = select(cls).order_by(col(cls.created_at).desc(), col(cls.id).desc())
        if status:
            stmt = stmt.where(col(cls.status) == status)
        else:
            stmt = stmt.where(col(cls.status).in_(cls.OPEN))
        with db_session() as session:
            rows = session.exec(stmt.limit(50)).all()
            jobs = {
                j.uid: j
                for j in session.exec(
                    select(JobRow).where(
                        col(JobRow.uid).in_([r.job_uid for r in rows if r.job_uid])
                    )
                ).all()
            }
        return [
            {
                "id": r.id,
                "boss_id": r.boss_id,
                "job_uid": r.job_uid,
                "hr_name": r.hr_name,
                "company": r.company or (jobs[r.job_uid].company if r.job_uid in jobs else ""),
                "title": jobs[r.job_uid].title if r.job_uid in jobs else "",
                "text": r.text,
                "outcome": r.outcome,
                "send_resume": r.send_resume,
                "status": r.status,
                "status_label": cls.STATUS_LABELS.get(r.status, r.status),
                "created_at": r.created_at.replace(tzinfo=UTC)
                .astimezone()
                .strftime("%m-%d %H:%M"),
            }
            for r in rows
        ]
