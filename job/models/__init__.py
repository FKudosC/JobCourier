"""本地 SQLite：路径、引擎、初始化与轻量迁移。"""

from __future__ import annotations

import importlib

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from job.paths import USER_DATA_DIR
from job.utils import get_logger

DATA_DIR = USER_DATA_DIR
DB_PATH = DATA_DIR / "job-courier.db"
_engine = None

# 模型自动补列时遇到的、需要人工处理的告警（如 NOT NULL 又无默认值）。
# 由 ``/api/runtime`` 暴露给前端，方便在界面上提示「数据库需手动迁移」。
_MIGRATION_WARNINGS: list[str] = []

log = get_logger("db")


def migration_warnings() -> list[str]:
    """返回本次启动过程中未能自动修复的迁移告警。"""
    return list(_MIGRATION_WARNINGS)


def reset_engine() -> None:
    """测试用：释放并清空引擎缓存。"""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
    _MIGRATION_WARNINGS.clear()


def get_engine():
    """懒加载引擎；首次调用时建库建表。"""
    global _engine
    if _engine is not None:
        return _engine

    # 有表模型的模块都要先导入，``SQLModel.metadata`` 才知道这些表。
    # ``job.models.stats`` 只做 SQL 聚合、没有 table=True 的模型，故不在此列。
    for mod in (
        "job.models.job",
        "job.models.search",
        "job.models.setting",
        "job.models.chat",
        "job.models.company",
        "job.models.blacklist",
        "job.models.pending",
    ):
        importlib.import_module(mod)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        """开启 WAL 与忙等待：并发读写下不再动不动 database is locked。"""
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    SQLModel.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _ensure_indexes(_engine)
    _backfill_salary_monthly(_engine)
    return _engine


def _sql_literal(value: object) -> str | None:
    """把 Python 默认值转成 SQL 字面量；转不了就返回 None（视为没有可用默认值）。"""
    if value is None or callable(value):
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _add_missing_columns(engine) -> None:
    """给旧库补上模型新增的列。

    ``create_all`` 只会建缺失的表，不会给已存在的表加列。不补的话，模型里新增字段后
    读那张表就会报 ``no such column``，整个应用起不来——这里逐列比对补齐，省得每加一个
    字段就手写一次 ALTER TABLE。
    """
    from sqlalchemy import inspect
    from sqlalchemy import text as sql

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing:
            continue  # 整张表都缺，交给 create_all 建
        have = {c["name"] for c in inspector.get_columns(table.name)}
        statements: list[str] = []
        for column in table.columns:
            if column.name in have:
                continue
            default = getattr(column.default, "arg", None)
            literal = _sql_literal(default)
            if not column.nullable and literal is None:
                warning = (
                    f"{table.name}.{column.name} 缺失且无默认值，需手动迁移数据库"
                )
                _MIGRATION_WARNINGS.append(warning)
                log.error("自动迁移跳过了 NOT NULL 且无默认值的列：%s", warning)
                continue
            ddl = (
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                f"{column.type.compile(dialect=engine.dialect)}"
            )
            if not column.nullable:
                ddl += " NOT NULL"
            if literal is not None:
                ddl += f" DEFAULT {literal}"
            statements.append(ddl)
        if not statements:
            continue
        # 同一张表的补列放在一个事务里，避免中途失败留下半套结构
        with engine.begin() as conn:
            for ddl in statements:
                conn.execute(sql(ddl))
            log.info("已为表 %s 补上 %d 个新列", table.name, len(statements))


def _ensure_indexes(engine) -> None:
    """给已存在的表补建模型声明但库里没有的索引（ALTER 加列不会自动建索引）。"""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing:
            continue  # 新表由 create_all 连索引一起建
        have = {i["name"] for i in inspector.get_indexes(table.name)}
        columns = {c["name"] for c in inspector.get_columns(table.name)}
        for index in table.indexes:
            if index.name in have:
                continue
            # 索引涉及的列可能因 NOT NULL 无默认值而被跳过（见 _add_missing_columns），
            # 缺列时建索引会报 no such column，这里直接跳过。
            if any(col.name not in columns for col in index.columns):
                continue
            with engine.begin() as conn:
                index.create(conn)


def _backfill_salary_monthly(engine) -> None:
    """旧库补列后，把已有岗位的估算月薪算一遍（只补空值，一次成本极低）。"""
    from sqlalchemy import text as sql

    from job.boss.salary import monthly_estimate

    with engine.begin() as conn:
        rows = conn.execute(
            sql(
                "SELECT uid, salary FROM job "
                "WHERE salary_monthly IS NULL AND salary <> ''"
            )
        ).fetchall()
        for uid, salary in rows:
            value = monthly_estimate(salary)
            if value is not None:
                conn.execute(
                    sql("UPDATE job SET salary_monthly = :value WHERE uid = :uid"),
                    {"value": value, "uid": uid},
                )
        if rows:
            log.info("已回填 %d 条岗位的估算月薪", len(rows))


def db_session() -> Session:
    """短生命周期 Session（`with db_session() as s:`）。"""
    return Session(get_engine())


def init_db() -> None:
    """建表，并确保有求职配置。"""
    from job.models.search import SearchConfigRow

    SearchConfigRow.ensure()
