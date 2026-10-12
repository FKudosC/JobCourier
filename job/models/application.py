"""网申助手：企业官网表单投递任务，以及按域名缓存的字段映射。

很多公司在 BOSS 直聘上并不挂 HR，简历发不出去，只能去企业官网的表单里填一遍。
这里把「哪个岗位要在哪个网址网申」记成一条任务，并把最近一次扫描出来的
字段对照表一并存下——表单填到一半关掉，下次打开还能接着改。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

from sqlmodel import Field, SQLModel, select

from job.models.job import JobRow


def _now() -> datetime:
    return datetime.now(UTC)


def _local_iso(value: datetime | None) -> str:
    """库里的 UTC 时间 → 带时区的本地时间文本。"""
    if value is None:
        return ""
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone().isoformat(timespec="seconds")


def _loads(raw: str) -> list[dict[str, Any]]:
    """字段对照表 JSON → list；损坏或为空时返回空列表。"""
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def domain_of(url: str) -> str:
    """取网址域名（去掉 www. 前缀），字段映射按它复用。"""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class ApplicationRow(SQLModel, table=True):
    """一条网申任务：一个岗位对应企业官网的一个表单页。"""

    __tablename__ = "application"  # pyright: ignore[reportAssignmentType]

    # 任务状态：待网申 / 已填表（等确认）/ 已提交 / 需人工处理
    STATUS_LABELS: ClassVar[dict[str, str]] = {
        "pending": "待网申",
        "filled": "已填表",
        "submitted": "已提交",
        "failed": "需人工",
    }

    uid: str = Field(primary_key=True, description="与岗位 uid 一致：一个岗位一条网申任务")
    job_uid: str = Field(default="", index=True, description="关联的岗位 uid")
    platform: str = Field(default="boss", description="来源平台")
    title: str = ""
    company: str = ""
    salary: str = ""
    location: str = ""
    url: str = Field(default="", description="网申地址（企业官网的表单页）")
    domain: str = Field(default="", index=True, description="网申地址域名，字段映射按它复用")
    status: str = Field(default="pending", index=True, description="状态，见 STATUS_LABELS")
    note: str = Field(default="", description="最近一次操作的说明或报错")
    fields_json: str = Field(default="[]", description="最近一次字段对照表（JSON 数组）")
    submitted_at: datetime | None = Field(default=None, description="提交时间（UTC）")
    created_at: datetime = Field(default_factory=_now, index=True, description="建立时间（UTC）")
    updated_at: datetime = Field(default_factory=_now, description="最后更新时间（UTC）")

    def to_dict(self) -> dict[str, Any]:
        """转为前端字典。"""
        return {
            "uid": self.uid,
            "jobUid": self.job_uid,
            "platform": self.platform,
            "title": self.title,
            "company": self.company,
            "salary": self.salary,
            "location": self.location,
            "url": self.url,
            "domain": self.domain,
            "status": self.status,
            "statusLabel": self.STATUS_LABELS.get(self.status, self.status),
            "note": self.note,
            "fields": _loads(self.fields_json),
            "submittedAt": _local_iso(self.submitted_at),
            "createdAt": _local_iso(self.created_at),
            "updatedAt": _local_iso(self.updated_at),
        }

    @classmethod
    def create(cls, job_uid: str, url: str = "") -> ApplicationRow:
        """为岗位建网申任务（已有则更新网址）；岗位信息从岗位表快照一份。"""
        from job.models import db_session

        with db_session() as session:
            job = session.get(JobRow, job_uid)
            row = session.get(cls, job_uid)
            if row is None:
                row = cls(uid=job_uid, job_uid=job_uid)
            if job is not None:
                row.platform = job.platform or row.platform or "boss"
                row.title = job.title or row.title
                row.company = job.company or row.company
                row.salary = job.salary or row.salary
                row.location = job.location or row.location
            if url:
                row.url = url
                row.domain = domain_of(url)
            row.updated_at = _now()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    @classmethod
    def get_dict(cls, uid: str) -> dict[str, Any] | None:
        """按主键查询。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
        return None if row is None else row.to_dict()

    @classmethod
    def create_standalone(cls, url: str) -> ApplicationRow:
        """独立网申任务：不依赖岗位库，uid 自造，公司 / 岗位名等页面检测后回填。"""
        from uuid import uuid4

        from job.models import db_session

        with db_session() as session:
            row = cls(uid=f"apply:{uuid4().hex}", job_uid="", platform="web")
            row.url = url
            row.domain = domain_of(url)
            row.updated_at = _now()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    @classmethod
    def set_meta(
        cls,
        uid: str,
        *,
        title: str = "",
        company: str = "",
        salary: str = "",
        location: str = "",
    ) -> dict[str, Any] | None:
        """回填页面检测到的公司 / 岗位信息；已有值不覆盖（用户手改的优先）。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
            if row is None:
                return None
            for field, value in (
                ("title", title),
                ("company", company),
                ("salary", salary),
                ("location", location),
            ):
                text = (value or "").strip()
                if text and not getattr(row, field):
                    setattr(row, field, text)
            row.updated_at = _now()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.to_dict()

    @classmethod
    def list_dicts(cls, *, status: str = "") -> list[dict[str, Any]]:
        """网申任务列表，新加的在前；``status`` 非空时只看该状态。"""
        from job.models import db_session

        stmt = select(cls).order_by(cls.created_at.desc(), cls.uid.desc())
        if status:
            stmt = stmt.where(cls.status == status)
        with db_session() as session:
            rows = session.exec(stmt).all()
        return [row.to_dict() for row in rows]

    @classmethod
    def count(cls, *, status: str = "") -> int:
        """任务数。"""
        from sqlalchemy import func

        from job.models import db_session

        stmt = select(func.count()).select_from(cls)
        if status:
            stmt = stmt.where(cls.status == status)
        with db_session() as session:
            return int(session.exec(stmt).one())

    @classmethod
    def set_url(cls, uid: str, url: str) -> dict[str, Any] | None:
        """改写网申地址。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
            if row is None:
                return None
            row.url = url
            row.domain = domain_of(url)
            row.updated_at = _now()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.to_dict()

    @classmethod
    def save_fields(cls, uid: str, fields: list[dict[str, Any]], *, note: str = "") -> dict[str, Any] | None:
        """存下字段对照表（扫描或填表后调用）。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
            if row is None:
                return None
            row.fields_json = json.dumps(fields, ensure_ascii=False)
            if note:
                row.note = note
            row.updated_at = _now()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.to_dict()

    @classmethod
    def set_status(cls, uid: str, status: str, *, note: str = "") -> dict[str, Any] | None:
        """改状态；``submitted`` 会记一次提交时间。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
            if row is None:
                return None
            row.status = status
            if note:
                row.note = note
            if status == "submitted":
                row.submitted_at = _now()
            row.updated_at = _now()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.to_dict()

    @classmethod
    def delete_by_uid(cls, uid: str) -> bool:
        """删除一条，不存在返回 False。"""
        from job.models import db_session

        with db_session() as session:
            row = session.get(cls, uid)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True


class ApplyMappingRow(SQLModel, table=True):
    """按域名记住的字段映射：同一个官网的表单下次直接套用，不用再让 AI 猜一遍。"""

    __tablename__ = "apply_mapping"  # pyright: ignore[reportAssignmentType]

    key: str = Field(primary_key=True, description="``域名::字段签名``")
    domain: str = Field(default="", index=True)
    label: str = Field(default="", description="字段标签，仅供排查")
    value: str = Field(default="", description="上次填的内容")
    source: str = Field(default="", description="来源：profile / resume / ai / manual")
    updated_at: datetime = Field(default_factory=_now)

    @staticmethod
    def make_key(domain: str, signature: str) -> str:
        return f"{domain}::{signature}"

    @classmethod
    def lookup(cls, domain: str, signatures: list[str]) -> dict[str, ApplyMappingRow]:
        """取该域名下已记住的映射，键为字段签名。"""
        from job.models import db_session

        if not domain or not signatures:
            return {}
        keys = [cls.make_key(domain, sig) for sig in signatures]
        with db_session() as session:
            rows = session.exec(select(cls).where(cls.key.in_(keys))).all()
        return {row.key.removeprefix(f"{domain}::"): row for row in rows}

    @classmethod
    def remember(cls, domain: str, items: list[dict[str, Any]]) -> int:
        """记下一批映射；``items`` 每项含 ``signature`` / ``label`` / ``value`` / ``source``。"""
        from job.models import db_session

        if not domain or not items:
            return 0
        saved = 0
        with db_session() as session:
            for item in items:
                signature = str(item.get("signature") or "")
                value = str(item.get("value") or "")
                if not signature or not value:
                    continue
                key = cls.make_key(domain, signature)
                row = session.get(cls, key) or cls(key=key, domain=domain)
                row.label = str(item.get("label") or row.label)
                row.value = value
                row.source = str(item.get("source") or row.source)
                row.updated_at = _now()
                session.add(row)
                saved += 1
            session.commit()
        return saved

    @classmethod
    def forget_domain(cls, domain: str) -> int:
        """清掉某个域名下记住的映射，返回删除条数。"""
        from job.models import db_session

        if not domain:
            return 0
        with db_session() as session:
            rows = session.exec(select(cls).where(cls.domain == domain)).all()
            for row in rows:
                session.delete(row)
            session.commit()
        return len(rows)
