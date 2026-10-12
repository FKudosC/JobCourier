"""职位表模型与增删查改。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import or_
from sqlmodel import Field, SQLModel, col, select

from job.boss.salary import monthly_estimate

if TYPE_CHECKING:
    from job.boss.jobs import Job


def _now() -> datetime:
    return datetime.now(UTC)


def _loads_detail(raw: str) -> dict[str, Any]:
    """评分明细 JSON → dict；损坏或为空时返回空 dict。"""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _local_iso(value: datetime) -> str:
    """库里的 UTC 时间 → 带时区的本地时间文本。"""
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone().isoformat(timespec="seconds")


class JobRow(SQLModel, table=True):
    """看过的岗位：合适的投递，不合适的也记下原因，下次遇到直接跳过。"""

    __tablename__ = "job"  # pyright: ignore[reportAssignmentType]

    # 高匹配分数线
    HIGH_MATCH: ClassVar[int] = 80
    # 分析状态筛选项（第一项为不筛选）
    ANALYSIS_FILTERS: ClassVar[tuple[str, ...]] = (
        "全部分析状态",
        "已分析",
        "未分析",
        f"高匹配（≥{HIGH_MATCH}）",
    )
    # 是否合适筛选项（第一项为不筛选）
    SUITABLE_FILTERS: ClassVar[tuple[str, ...]] = ("全部", "合适", "不合适")
    # 列表排序：空串为默认（新入库优先）；评分与薪资各自升降序，二者互斥（前端单选）
    SORTS: ClassVar[tuple[str, ...]] = (
        "score_desc",
        "score_asc",
        "salary_desc",
        "salary_asc",
    )
    # 待手动投递的原因前缀：自动投递走不通时降级，岗位页据此给「打开平台/我已投递」按钮
    MANUAL_PENDING_PREFIX: ClassVar[str] = "待手动投递"

    uid: str = Field(primary_key=True, description="主键：BOSS 用裸 ID，其他平台带平台前缀")
    platform: str = Field(default="boss", index=True, description="来源平台（当前仅 BOSS 直聘）")
    job_id: str | None = Field(default=None, index=True)
    link: str = Field(default="", index=True)
    title: str = ""
    salary: str = ""
    company: str = ""
    brand_id: str = Field(default="", description="BOSS 公司 ID，公司主页 /gongsi/{brand_id}.html")
    location: str = ""
    experience: str = ""
    education: str = ""
    description: str = ""
    hr_name: str = ""
    hr_title: str = ""
    address: str = ""
    match_score: int | None = Field(
        default=None, index=True, description="匹配度 0–100；未分析为空"
    )
    salary_monthly: float | None = Field(
        default=None,
        index=True,
        description="按薪资文案折算的估算月薪（元 / 月），仅用于排序；认不出为空",
    )
    # 五维评分明细与硬性缺口（Verdict.detail 的 JSON）；未分析或旧数据为空
    score_detail: str = Field(default="", description="评分明细 JSON：维度得分与硬性缺口")
    suitable: bool = Field(default=True, description="是否合适")
    applied: bool = Field(default=False, description="是否已投递（点过「立即沟通」）")
    reason: str = Field(default="", description="合适或不合适的原因，如「外包公司」")
    created_at: datetime = Field(default_factory=_now, index=True, description="首次入库时间（UTC）")
    updated_at: datetime = Field(default_factory=_now, description="最后更新时间（UTC）")

    @property
    def result(self) -> str:
        """处理结果：已投递 / 已沟通过 / 不合适。

        「已投递」是既成事实，优先于事后的 AI 评分：投递后补评分给出低分，
        只更新分数与原因，不再把结果改写成「不合适」。
        """
        if self.applied:
            return "已投递"
        if not self.suitable:
            return "不合适"
        return "已沟通过"

    def to_dict(self) -> dict[str, Any]:
        """转为前端表格字典。"""
        score = self.match_score
        created = self.created_at.replace(tzinfo=self.created_at.tzinfo or UTC)
        detail = _loads_detail(self.score_detail)
        return {
            "uid": self.uid,
            "title": self.title,
            "salary": self.salary,
            "company": self.company,
            "brandId": self.brand_id,
            "location": self.location,
            "link": self.link,
            "hrName": self.hr_name,
            "hrTitle": self.hr_title,
            "address": self.address,
            "description": self.description,
            "experience": self.experience,
            "education": self.education,
            "matchStatus": "未分析" if score is None else f"{score} 分",
            "matchHigh": score is not None and score >= self.HIGH_MATCH,
            "scoreDetail": detail.get("dimensions", []),
            "hardGaps": detail.get("hard_gaps", []),
            "result": self.result,
            "suitable": self.suitable,
            "applied": self.applied,
            "manualPending": self.reason.startswith(self.MANUAL_PENDING_PREFIX),
            "reason": self.reason,
            "createdAt": created.astimezone().strftime("%Y-%m-%d %H:%M"),
        }

    @staticmethod
    def uid_for(job: Job) -> str:
        """根据抓取结果生成稳定主键：优先职位 ID，否则用链接。"""
        return job.job_id or job.link or f"{job.company}|{job.title}|{job.salary}"

    @classmethod
    def record(
        cls,
        job: Job,
        *,
        suitable: bool = True,
        reason: str = "",
        score: int | None = None,
        detail: str | None = None,
        applied: bool = False,
    ) -> JobRow:
        """按 uid 插入或更新判断结果；没给新匹配度 ``score`` 时保留已有的。"""
        from job.models import db_session

        row = cls(
            **job.model_dump(),
            uid=cls.uid_for(job),
            match_score=score,
            salary_monthly=monthly_estimate(job.salary),
            suitable=suitable,
            applied=applied,
            reason=reason,
        )
        if detail is not None:
            row.score_detail = detail
        with db_session() as session:
            if existing := session.get(cls, row.uid):
                row.created_at = existing.created_at
                row.brand_id = row.brand_id or existing.brand_id
                if score is None:
                    row.match_score = existing.match_score
                if detail is None:
                    row.score_detail = existing.score_detail
                if row.salary_monthly is None:
                    row.salary_monthly = existing.salary_monthly
            merged = session.merge(row)
            session.commit()
            session.refresh(merged)
            return merged

    @classmethod
    def find_duplicate(cls, job: Job) -> JobRow | None:
        """库里的同一岗位：职位 ID 相同，或招聘标题、公司、HR 都相同。"""
        from job.models import db_session

        with db_session() as session:
            if row := session.get(cls, cls.uid_for(job)):
                return row
            stmt = select(cls).where(
                col(cls.title) == job.title,
                col(cls.company) == job.company,
                col(cls.hr_name) == job.hr_name,
            )
            return session.exec(stmt.limit(1)).first()

    @classmethod
    def _apply_filters(
        cls,
        stmt,
        search: str,
        analysis: str,
        suitable: str,
        uid: str = "",
        platform: str = "",
    ):
        """按岗位名 / 公司模糊搜，并按分析状态、是否合适、平台筛选；给了 ``uid`` 只看这一个岗位。"""
        if uid:
            stmt = stmt.where(col(cls.uid) == uid)
        if q := search.strip():
            like = f"%{q}%"
            stmt = stmt.where(
                or_(col(cls.title).like(like), col(cls.company).like(like))
            )
        if platform:
            stmt = stmt.where(col(cls.platform) == platform)
        _, analyzed, pending, high = cls.ANALYSIS_FILTERS
        score = col(cls.match_score)
        conditions = {
            analyzed: score.is_not(None),
            pending: score.is_(None),
            high: score >= cls.HIGH_MATCH,
        }
        if analysis in conditions:
            stmt = stmt.where(conditions[analysis])
        _, yes, no = cls.SUITABLE_FILTERS
        if suitable in (yes, no):
            # 「合适」把已投递也算进去：投递过的岗位即便事后被补评分判低，也仍在候选里，
            # 否则会出现「已投递但不合适」这种自相矛盾的展示口径。
            if suitable == yes:
                stmt = stmt.where(
                    or_(col(cls.suitable).is_(True), col(cls.applied).is_(True))
                )
            else:
                stmt = stmt.where(
                    col(cls.suitable).is_(False), col(cls.applied).is_(False)
                )
        return stmt

    @classmethod
    def count(
        cls,
        *,
        search: str = "",
        analysis: str = "",
        suitable: str = "",
        uid: str = "",
        platform: str = "",
    ) -> int:
        """统计岗位数；可按关键字、分析状态、是否合适、平台与主键筛选。"""
        from sqlalchemy import func

        from job.models import db_session

        with db_session() as session:
            stmt = select(func.count()).select_from(cls)
            stmt = cls._apply_filters(stmt, search, analysis, suitable, uid, platform)
            return int(session.exec(stmt).one())

    @classmethod
    def count_applied(cls) -> int:
        """累计投递数。"""
        from sqlalchemy import func

        from job.models import db_session

        stmt = select(func.count()).select_from(cls).where(col(cls.applied).is_(True))
        with db_session() as session:
            return int(session.exec(stmt).one())

    @classmethod
    def unscored_uids(cls, limit: int = 60) -> list[str]:
        """合适但还没有评分的岗位主键（投递结束后的自动评分用，含看过的）。"""
        from job.models import db_session

        stmt = (
            select(cls.uid)
            .where(
                col(cls.suitable).is_(True),
                col(cls.match_score).is_(None),
            )
            .order_by(col(cls.created_at).desc())
            .limit(limit)
        )
        with db_session() as session:
            return list(session.exec(stmt).all())

    @classmethod
    def company_source(cls) -> list[dict[str, str]]:
        """全部岗位的公司归属（批量查企业用）：平台、公司键字段与链接。"""
        from job.models import db_session

        with db_session() as session:
            rows = session.exec(select(cls)).all()
        return [
            {
                "uid": row.uid,
                "platform": row.platform or "boss",
                "company": row.company or "",
                "brand_id": row.brand_id or "",
                "link": row.link or "",
            }
            for row in rows
        ]

    @classmethod
    def list_dicts(
        cls,
        *,
        search: str = "",
        analysis: str = "",
        suitable: str = "",
        uid: str = "",
        platform: str = "",
        sort: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """分页列表。默认新入库优先；``sort`` 为评分 / 薪资的升降序（见 ``SORTS``）。"""
        from job.models import db_session

        with db_session() as session:
            stmt = cls._apply_filters(select(cls), search, analysis, suitable, uid, platform)
            if sort in ("salary_desc", "salary_asc"):
                # 入库时就把薪资折算成 salary_monthly，这里直接走 SQL 排序 + 分页，
                # 不再全量取回内存；识别不出月薪的始终排在最后。
                money = col(cls.salary_monthly)
                direction = money.desc() if sort == "salary_desc" else money.asc()
                stmt = stmt.order_by(
                    money.is_(None),
                    direction,
                    col(cls.created_at).desc(),
                    col(cls.uid).desc(),
                )
                rows = session.exec(stmt.offset(offset).limit(limit)).all()
                return cls._with_risk([r.to_dict() for r in rows])
            score = col(cls.match_score)
            if sort == "score_desc":
                stmt = stmt.order_by(
                    score.is_(None), score.desc(), col(cls.created_at).desc(), col(cls.uid).desc()
                )
            elif sort == "score_asc":
                stmt = stmt.order_by(
                    score.is_(None), score.asc(), col(cls.created_at).desc(), col(cls.uid).desc()
                )
            else:
                stmt = stmt.order_by(col(cls.created_at).desc(), col(cls.uid).desc())
            rows = session.exec(stmt.offset(offset).limit(limit)).all()
        return cls._with_risk([r.to_dict() for r in rows])

    @staticmethod
    def _with_risk(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """补上公司查询得到的风险等级 risk / riskLabel（没查过为空）。"""
        from job.models.company import CompanyRow

        risks = CompanyRow.risks(i["brandId"] for i in items)
        for item in items:
            risk = risks.get(item["brandId"], "")
            item["risk"], item["riskLabel"] = risk, CompanyRow.RISK_LABELS.get(risk, "")
        return items

    @classmethod
    def to_json(cls, uids: list[str] | None = None) -> tuple[str, int]:
        """导出 JSON 文本与条数：``uids`` 为空导出全部，否则只导出这些岗位（新入库优先）。

        每个岗位带全部表字段（时间为带时区的本地时间）与处理结果 ``result``。
        """
        from job.models import db_session

        stmt = select(cls).order_by(col(cls.created_at).desc(), col(cls.uid).desc())
        if uids:
            stmt = stmt.where(col(cls.uid).in_(uids))
        with db_session() as session:
            rows = session.exec(stmt).all()
        items = [
            {
                **{k: _local_iso(v) if isinstance(v, datetime) else v for k, v in row.model_dump().items()},
                "result": row.result,
            }
            for row in rows
        ]
        return json.dumps(items, ensure_ascii=False, indent=2), len(items)

    @classmethod
    def get_dict(cls, uid: str) -> dict[str, Any] | None:
        """按主键查询。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
        return None if row is None else cls._with_risk([row.to_dict()])[0]

    @classmethod
    def count_today(cls, platform: str = "boss") -> int:
        """今天（本地时区）在指定平台投递的岗位数。"""
        from sqlalchemy import func

        from job.models import db_session

        midnight = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        since = midnight.astimezone(UTC)
        stmt = (
            select(func.count())
            .select_from(cls)
            .where(
                col(cls.applied).is_(True),
                col(cls.platform) == platform,
                col(cls.created_at) >= since,
            )
        )
        with db_session() as session:
            return int(session.exec(stmt).one())

    @classmethod
    def set_verdict(
        cls,
        uid: str,
        *,
        suitable: bool,
        reason: str,
        score: int | None,
        detail: str | None = None,
    ) -> bool:
        """写回 AI 判断：是否合适、原因与匹配度（为空时保留原匹配度）；岗位不存在返回 False。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
            if row is None:
                return False
            row.suitable = suitable
            row.reason = reason
            if score is not None:
                row.match_score = score
            if detail is not None:
                row.score_detail = detail
            row.updated_at = _now()
            session.add(row)
            session.commit()
            return True

    @classmethod
    def set_applied(cls, uid: str, *, reason: str = "") -> bool:
        """手动记账：在原平台投递后点「我已投递」；岗位不存在返回 False。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
            if row is None:
                return False
            row.applied = True
            if reason:
                row.reason = reason
            row.updated_at = _now()
            session.add(row)
            session.commit()
            return True

    @classmethod
    def set_description(cls, uid: str, description: str) -> bool:
        """补写职位描述，其余字段不变；岗位不存在返回 False。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
            if row is None:
                return False
            row.description = description
            row.updated_at = _now()
            session.add(row)
            session.commit()
            return True

    @classmethod
    def set_brand_id(cls, uid: str, brand_id: str) -> bool:
        """补写公司 ID，其余字段不变；岗位不存在返回 False。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
            if row is None:
                return False
            row.brand_id = brand_id
            session.add(row)
            session.commit()
            return True

    @classmethod
    def delete_by_uid(cls, uid: str) -> bool:
        """删除一条，不存在则 False。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    @classmethod
    def delete_many(cls, uids: list[str]) -> int:
        """批量删除，返回实际删除数。"""
        from job.models import db_session

        deleted = 0
        with db_session() as session:
            for uid in uids:
                row = session.get(cls, uid)
                if row is None:
                    continue
                session.delete(row)
                deleted += 1
            session.commit()
        return deleted
