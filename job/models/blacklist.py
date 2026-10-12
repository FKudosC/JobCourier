"""公司黑名单：查企业判为高风险的公司自动沉淀，手动也可添加；投递预筛直接跳过。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import ClassVar

from sqlmodel import Field, SQLModel, col, select


def _now() -> datetime:
    return datetime.now(UTC)


class CompanyBlacklistRow(SQLModel, table=True):
    """一条黑名单记录：按公司 ID 或公司名屏蔽（手动添加的没有公司 ID）。

    来源 ``source``：``ai`` 查企业评估为高风险时自动加入，``manual`` 用户手动加入。
    """

    __tablename__ = "company_blacklist"  # pyright: ignore[reportAssignmentType]

    SOURCE_LABELS: ClassVar[dict[str, str]] = {
        "ai": "查企业评估",
        "manual": "手动添加",
        "hr": "主动联系拉黑",
    }

    brand_id: str = Field(primary_key=True, description="BOSS 公司 ID；手动按名字添加时为空")
    name: str = Field(index=True, description="公司名（简称），黑名单匹配以它兜底")
    reason: str = Field(default="", description="拉黑原因，如 AI 评估结论")
    source: str = Field(default="manual", description="来源，见 SOURCE_LABELS")
    created_at: datetime = Field(default_factory=_now, description="拉黑时间（UTC）")

    @classmethod
    def add(
        cls, *, brand_id: str = "", name: str = "", reason: str = "", source: str = "manual"
    ) -> bool:
        """加入黑名单；两者都为空时不加，返回是否新增。"""
        if not brand_id and not name.strip():
            return False
        from job.models import db_session

        key = brand_id or f"name:{name.strip()}"
        with db_session() as session:
            session.merge(
                cls(brand_id=key, name=name.strip() or brand_id, reason=reason, source=source)
            )
            session.commit()
        return True

    @classmethod
    def remove(cls, brand_id: str) -> bool:
        """移出黑名单；不存在返回 False。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, brand_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    @classmethod
    def blocked(cls, brand_id: str, company: str) -> str | None:
        """这家公司是否在黑名单：命中返回原因（空原因给默认文案），没命中返回 None。

        优先按公司 ID 匹配；手动添加的记录没有公司 ID，按公司名精确匹配兜底。
        """
        from job.models import db_session

        with db_session() as session:
            if brand_id and (row := session.get(cls, brand_id)):
                return row.reason or "黑名单公司"
            if name := company.strip():
                stmt = select(cls).where(col(cls.name) == name)
                if row := session.exec(stmt.limit(1)).first():
                    return row.reason or "黑名单公司"
        return None

    @classmethod
    def list_dicts(cls) -> list[dict]:
        """全部黑名单（新的在前），给配置页管理用。"""
        from job.models import db_session

        with db_session() as session:
            rows = session.exec(
                select(cls).order_by(col(cls.created_at).desc(), col(cls.brand_id).desc())
            ).all()
        return [
            {
                "brand_id": r.brand_id,
                "name": r.name,
                "reason": r.reason,
                "source": r.source,
                "source_label": cls.SOURCE_LABELS.get(r.source, r.source),
                "created_at": r.created_at.replace(tzinfo=UTC).astimezone().strftime("%Y-%m-%d"),
            }
            for r in rows
        ]

    @classmethod
    def names(cls, brand_ids: Iterable[str] | None = None) -> dict[str, str]:
        """公司 ID（或 name: 前缀键）→ 原因，投递循环里整表载入缓存用。"""
        from job.models import db_session

        with db_session() as session:
            rows = session.exec(select(cls)).all()
        return {r.brand_id: r.reason or "黑名单公司" for r in rows}

    @classmethod
    def entry_names(cls) -> list[str]:
        """全部黑名单词条的公司名（用于匹配岗位名称）。"""
        from job.models import db_session

        with db_session() as session:
            rows = session.exec(select(cls)).all()
        return [r.name for r in rows if r.name]
