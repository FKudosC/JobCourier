"""JobCourier API：全部 REST 路由 + WebSocket 状态/日志流 + 前端静态伺服。

业务逻辑全部在内核（``job/boss``、``job/models``），这里只做装配、校验与转发。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import APIError
from patchright.async_api import Error as PlaywrightError
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.exceptions import AgentRunError

from job.api.net import DEV_PORT
from job.api.runtime import ApiError, runtime
from job.apply.assistant import ApplyError
from job.apply.profile import ApplyProfile
from job.boss.filters import (
    City,
    Education,
    Experience,
    Funding,
    JobType,
    Pace,
    PaceProfile,
    ReplyPaceProfile,
    Salary,
    Scale,
)
from job.boss.keywords import KeywordAgent
from job.boss.resume_tailor import ResumeTailor, load_saved_resume, save_custom_resume
from job.models import init_db, migration_warnings
from job.models import stats as stats_mod
from job.models.application import ApplicationRow
from job.models.blacklist import CompanyBlacklistRow
from job.models.chat import ChatMessageRow, ChatStatusRow
from job.models.company import CompanyRow
from job.models.job import JobRow
from job.models.pending import ReplyPendingRow
from job.models.search import DEFAULT_MIN_SCORE, LIST_FIELDS, SearchConfigRow
from job.models.setting import (
    ApplyProfileSettings,
    AutoReplySettings,
    LlmSettings,
    TailorSettings,
)
from job.utils import log_exception
from job.utils.llm import explain
from job.utils.resume import ResumePdf

# 开发模式：只有跑 ``vite dev``（5173 端口）时才需要跨源白名单与接口文档。
# 生产由后端同源伺服前端静态文件（8000 端口），既不需要 CORS，也不该暴露文档。
DEV_MODE = os.environ.get("JOB_COURIER_DEV") == "1"
_DEV_ORIGINS = [f"http://localhost:{DEV_PORT}", f"http://127.0.0.1:{DEV_PORT}"]

# 只允许打开自家招聘平台的链接，避免 ``/api/open-link`` 变成任意 URL 跳板
_ALLOWED_LINK_HOSTS = ("zhipin.com",)

app = FastAPI(
    title="JobCourier API",
    docs_url="/api/docs" if DEV_MODE else None,
    openapi_url="/api/openapi.json" if DEV_MODE else None,
)
if DEV_MODE:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(ApiError)
async def _api_error_handler(_request, exc: ApiError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception):
    """兜底：任何没被业务捕获的异常都记日志，并给前端一个可读提示。"""
    log_exception(f"api {request.method} {request.url.path}", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务内部错误：{type(exc).__name__}，请查看后台日志"},
    )


def _now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M")


def _download(text: str, filename: str, media_type: str) -> Response:
    """附件下载：ASCII 名 + RFC5987 编码的原名兜底（中文名也不再乱码）。"""
    return Response(
        content=text,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


# ---------------------------------------------------------------------------
# 运行时：快照、投递、回复、浏览器
# ---------------------------------------------------------------------------


@app.get("/ping", include_in_schema=False)
def ping() -> dict:
    """就绪探测：启动脚本等前端可访问后自动开浏览器。"""
    return {"ok": True}


@app.get("/api/runtime")
def get_runtime() -> dict:
    # 页面加载时重新读一次累计投递数：它是持久化数据，重启服务后不该显示为 0
    runtime.refresh_stored_count()
    return {
        **runtime.snapshot(),
        "logs": list(runtime.logs),
        "db_warnings": migration_warnings(),
    }


@app.post("/api/apply/start")
async def apply_start() -> dict:
    runtime.begin_apply()
    return runtime.snapshot()


@app.post("/api/apply/stop")
async def apply_stop() -> dict:
    runtime.scraper.request_stop()
    runtime.push_log("正在停止，处理完当前岗位后结束")
    runtime.boss_state = "停止中"
    runtime.publish_status()
    return runtime.snapshot()


@app.post("/api/reply/start")
async def reply_start() -> dict:
    runtime.begin_reply()
    return runtime.snapshot()


@app.post("/api/reply/stop")
async def reply_stop() -> dict:
    runtime.responder.request_stop()
    runtime.push_log("正在停止自动回复")
    runtime.reply_state = "停止中"
    runtime.publish_status()
    return runtime.snapshot()


@app.post("/api/browser/close")
async def browser_close() -> dict:
    await runtime.close_browser()
    return runtime.snapshot()


@app.post("/api/browser/show")
async def browser_show() -> dict:
    await runtime.show_browser()
    return runtime.snapshot()


class BackgroundIn(BaseModel):
    enabled: bool


@app.put("/api/browser/background")
async def browser_background(body: BackgroundIn) -> dict:
    await runtime.toggle_background(body.enabled)
    return runtime.snapshot()


class OpenLinkIn(BaseModel):
    url: str


def _checked_open_url(raw: str) -> str:
    """校验待打开链接：只放行 https 且属于受信任招聘平台的地址。"""
    url = (raw or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ApiError("只允许打开 https 链接")
    host = parsed.hostname.lower()
    if not any(host == h or host.endswith(f".{h}") for h in _ALLOWED_LINK_HOSTS):
        raise ApiError("只允许打开招聘平台的岗位链接")
    return url


@app.post("/api/open-link")
def open_link(body: OpenLinkIn) -> dict:
    runtime.open_link(_checked_open_url(body.url))
    return {"ok": True}


@app.delete("/api/logs")
def clear_logs() -> dict:
    runtime.clear_logs()
    return runtime.snapshot()


@app.websocket("/ws")
async def ws_channel(websocket: WebSocket) -> None:
    """状态与日志流：连上先发全量快照，之后逐条转发事件。"""
    await websocket.accept()
    queue = runtime.bus.subscribe()
    try:
        # 首帧快照同样要带上持久化的累计投递数
        runtime.refresh_stored_count()
        await websocket.send_json(
            {
                "type": "snapshot",
                **runtime.snapshot(),
                "logs": list(runtime.logs),
                "db_warnings": migration_warnings(),
            }
        )
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        runtime.bus.unsubscribe(queue)


# ---------------------------------------------------------------------------
# 求职配置
# ---------------------------------------------------------------------------


def _salary_value(raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, value)


def _pace_view(label: str, params: dict) -> dict:
    custom = Pace.code(label) == PaceProfile.CUSTOM
    text = PaceProfile.model_validate(params).describe()
    return {
        "label": label,
        "params": params,
        "custom": custom,
        "hint": text if custom else f"{text}；选择「自定义」可自行调整",
    }


def _reply_pace_view(label: str, params: dict) -> dict:
    custom = Pace.code(label) == ReplyPaceProfile.CUSTOM
    text = ReplyPaceProfile.model_validate(params).describe()
    return {
        "label": label,
        "params": params,
        "custom": custom,
        "hint": text if custom else f"{text}。选择「自定义」可自行调整",
    }


def config_bundle() -> dict:
    """配置中心全量数据（与原 ConfigState.on_load 对应）。"""
    config = SearchConfigRow.load()
    query = str(config.get("query") or "")
    keywords = [k for k in (config.get("keywords") or []) if k] or (
        [query] if query else []
    )
    reply = AutoReplySettings.load()
    tailor = TailorSettings.load()
    return {
        "query": query,
        "keywords": keywords,
        "cities": list(config.get("cities") or []),
        "experience": list(config.get("experience") or []),
        "education": list(config.get("education") or []),
        "funding": list(config.get("funding") or []),
        "scale": list(config.get("scale") or []),
        "job_type_label": JobType.label(str(config.get("job_type") or "")),
        "salary_label": Salary.label(str(config.get("salary") or "")),
        "pace": _pace_view(Pace.label(str(config.get("pace") or "")), dict(config.get("pace_params") or {})),
        "scam_block": bool(config.get("scam_block")),
        "resume_match": bool(config.get("resume_match")),
        "score_filter": bool(config.get("score_filter")),
        "min_score": int(config.get("min_score", DEFAULT_MIN_SCORE)),
        "salary_min": f"{float(config.get('salary_min') or 0):g}",
        "salary_max": f"{float(config.get('salary_max') or 0):g}",
        "resume_path": str(config.get("resume_path") or ""),
        "resume_text": str(config.get("resume_text") or ""),
        "tailor": {"enabled": tailor.enabled, "min_score": tailor.min_score},
        "reply": {
            "enabled": reply.enabled,
            "prompt": reply.prompt,
            "confirm_before_send": reply.confirm_before_send,
            "follow_up_hours": str(reply.follow_up_hours),
            "block_proactive_hr": reply.block_proactive_hr,
            "pace": _reply_pace_view(
                Pace.label(reply.pace), reply.pace_profile.model_dump()
            ),
        },
        "apply_profile": apply_profile_bundle(),
        "llm": llm_bundle(),
        "blacklist": CompanyBlacklistRow.list_dicts(),
        "options": {
            "cities": City.labels(),
            "job_type": JobType.labels(),
            "salary": Salary.labels(),
            "experience": Experience.labels(skip_unlimited=True),
            "education": Education.labels(skip_unlimited=True),
            "funding": Funding.labels(skip_unlimited=True),
            "scale": Scale.labels(skip_unlimited=True),
            "pace": Pace.labels(),
        },
    }


def _mask_key(key: str) -> str:
    """API Key 只回传掩码，明文留在本机数据库，不随接口回给浏览器。"""
    key = key or ""
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:3]}{'•' * 6}{key[-4:]}"


def _resolve_api_key(provided: str, *, base_url: str, current: LlmSettings) -> str:
    """前端回传的是掩码（或留空）时沿用已存的 Key；换了服务地址则视为没填。"""
    raw = (provided or "").strip()
    if raw and raw != _mask_key(current.api_key):
        return raw
    return "" if base_url.strip() != current.base_url.strip() else current.api_key


def llm_bundle() -> dict:
    settings = LlmSettings.load()
    return {
        "base_url": settings.base_url,
        "api_key": _mask_key(settings.api_key),
        "key_set": bool(settings.api_key),
        "model": settings.model,
        "models": settings.models,
        "ready": settings.ready,
    }


@app.get("/api/config")
def get_config() -> dict:
    init_db()
    return config_bundle()


class SearchConfigIn(BaseModel):
    query: str = ""
    keywords: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    experience: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    funding: list[str] = Field(default_factory=list)
    scale: list[str] = Field(default_factory=list)
    job_type_label: str = "全职"
    salary_label: str = "不限"
    pace_label: str = Pace.default_label
    pace_params: dict[str, float] = Field(default_factory=dict)
    scam_block: bool = False
    resume_match: bool = False
    score_filter: bool = False
    min_score: int = DEFAULT_MIN_SCORE
    salary_min: str = "0"
    salary_max: str = "0"
    resume_path: str = ""
    resume_text: str = ""


@app.put("/api/config/search")
def put_search_config(body: SearchConfigIn) -> dict:
    """保存岗位筛选表单（校验规则与原 ConfigState 各 setter 一致）。"""
    if not 0 <= body.min_score <= 100:
        raise ApiError("请输入 0–100 之间的整数")
    code = Pace.code(body.pace_label)
    try:
        if code == PaceProfile.CUSTOM:
            PaceProfile.model_validate(body.pace_params)
            pace_params = body.pace_params
        else:
            # 预设档把明细刷成预设值（与原 ConfigState.set_pace 一致）
            pace_params = PaceProfile.preset(code).model_dump()
    except ValidationError:
        raise ApiError("请输入 0–600 之间的数字") from None
    try:
        salary_min = float(body.salary_min or 0)
        salary_max = float(body.salary_max or 0)
    except (TypeError, ValueError):
        raise ApiError("请输入数字（K / 月），0 表示不限") from None
    SearchConfigRow.save(
        query=body.query,
        job_type=JobType.code(body.job_type_label),
        salary=Salary.code(body.salary_label),
        pace=code,
        pace_params=pace_params,
        scam_block=body.scam_block,
        resume_match=body.resume_match,
        score_filter=body.score_filter,
        min_score=body.min_score,
        salary_min=max(0.0, salary_min),
        salary_max=max(0.0, salary_max),
        resume_path=body.resume_path,
        resume_text=body.resume_text,
        **{f: getattr(body, f) for f in LIST_FIELDS},
    )
    return {"ok": True}


class ListIn(BaseModel):
    field: str
    value: str


def _mutate_list(field: str, value: str, *, add: bool) -> dict:
    if field not in LIST_FIELDS:
        raise ApiError(f"不支持的字段：{field}")
    value = str(value or "").strip()
    items = list(SearchConfigRow.load().get(field) or [])
    if add:
        if value and value not in items:
            items = [*items, value]
    else:
        items = [x for x in items if x != value]
    SearchConfigRow.save(**{field: items})
    return {"items": items}


@app.post("/api/config/list-item")
def add_list_item(body: ListIn) -> dict:
    return _mutate_list(body.field, body.value, add=True)


@app.delete("/api/config/list-item/{field}/{value}")
def remove_list_item(field: str, value: str) -> dict:
    return _mutate_list(field, value, add=False)


class KeywordGenerateOut(BaseModel):
    keywords: list[str]


@app.post("/api/config/keywords/generate", response_model=KeywordGenerateOut)
async def generate_keywords() -> KeywordGenerateOut:
    """用 AI 根据简历研判生成岗位搜索关键词（替换现有列表，与原逻辑一致）。"""
    config = SearchConfigRow.load()
    resume = str(config.get("resume_text") or "").strip()
    if not resume:
        raise ApiError("请先在下方上传简历或填写简历内容")
    llm = LlmSettings.load()
    if not llm.ready:
        raise ApiError("请先在「AI 服务」里开通大模型")
    try:
        words = await KeywordAgent(resume=resume, llm=llm).generate()
    except AgentRunError as exc:
        raise ApiError(f"AI 生成失败：{explain(exc)}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"AI 生成失败：{log_exception('api', exc)}") from exc
    SearchConfigRow.save(keywords=words)
    return KeywordGenerateOut(keywords=words)


class EnabledIn(BaseModel):
    enabled: bool


@app.put("/api/config/scam-block")
def put_scam_block(body: EnabledIn) -> dict:
    """骗子岗位识别开关（工作台用，单独切换）。"""
    SearchConfigRow.save(scam_block=body.enabled)
    return {"ok": True}


@app.put("/api/config/resume-text")
def put_resume_text(body: dict[str, Any]) -> dict:
    SearchConfigRow.save(resume_text=str(body.get("text") or ""))
    return {"ok": True}


@app.post("/api/config/resume/upload")
async def upload_resume(file: UploadFile = File(...)) -> dict:
    """上传简历：存到数据目录并解析文本；支持 PDF / docx / 图片（本地 OCR）。"""
    data = await file.read()
    name = file.filename or "resume.pdf"
    try:
        text = ResumePdf.extract_text(name, data)
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    path = str(ResumePdf.save(name, data))
    SearchConfigRow.save(resume_path=path, resume_text=text)
    hint = f"已识别简历内容，共 {len(text)} 字" if text else "未能识别简历文字，请直接粘贴简历内容"
    return {"resume_path": path, "resume_text": text, "hint": hint}


class TailorIn(BaseModel):
    enabled: bool
    min_score: int


@app.put("/api/config/tailor")
def put_tailor(body: TailorIn) -> dict:
    if not 0 <= body.min_score <= 100:
        raise ApiError("请输入 0–100 之间的整数")
    TailorSettings(enabled=body.enabled, min_score=body.min_score).save()
    return {"ok": True}


class ReplyConfigIn(BaseModel):
    enabled: bool | None = None
    prompt: str | None = None
    confirm_before_send: bool | None = None
    follow_up_hours: str | None = None
    block_proactive_hr: bool | None = None
    reply_pace_label: str | None = None
    reply_pace_params: dict[str, float] | None = None


@app.put("/api/config/reply")
def put_reply_config(body: ReplyConfigIn) -> dict:
    """保存自动回复设置（校验与原 ConfigState 的 reply setters 一致）。"""
    current = AutoReplySettings.load()
    enabled = current.enabled if body.enabled is None else body.enabled
    if enabled and not LlmSettings.load().ready:
        raise ApiError("请先在「AI 服务」里开通大模型")
    if enabled and not (SearchConfigRow.load().get("resume_text") or "").strip():
        raise ApiError("请先在「简历配置」里上传简历或填写简历内容")
    prompt = current.prompt if body.prompt is None else body.prompt
    confirm = (
        current.confirm_before_send
        if body.confirm_before_send is None
        else body.confirm_before_send
    )
    block = (
        current.block_proactive_hr
        if body.block_proactive_hr is None
        else body.block_proactive_hr
    )
    pace_label = (
        current.pace if body.reply_pace_label is None else Pace.code(body.reply_pace_label)
    )
    pace_params = (
        current.pace_profile.model_dump()
        if body.reply_pace_params is None
        else body.reply_pace_params
    )
    if body.reply_pace_params is not None:
        if Pace.code(body.reply_pace_label or "") != ReplyPaceProfile.CUSTOM:
            # 预设档把明细刷成预设值（与原 set_reply_pace 一致）
            pace_params = ReplyPaceProfile.preset(
                Pace.code(body.reply_pace_label or "")
            ).model_dump()
        else:
            try:
                ReplyPaceProfile.model_validate(body.reply_pace_params)
            except ValidationError:
                raise ApiError("请输入有效数字，停止时间需晚于开始时间") from None
    hours = current.follow_up_hours
    if body.follow_up_hours is not None:
        raw = (body.follow_up_hours or "").strip()
        try:
            hours = int(float(raw))
        except (TypeError, ValueError):
            hours = 48
        hours = max(0, min(720, hours))
    AutoReplySettings(
        enabled=enabled,
        prompt=prompt,
        pace=pace_label,
        pace_params=pace_params,
        confirm_before_send=confirm,
        follow_up_hours=hours,
        block_proactive_hr=block,
    ).save()
    return {"ok": True}


@app.post("/api/config/reply-prompt/reset")
def reset_reply_prompt() -> dict:
    AutoReplySettings.load()
    current = AutoReplySettings.load()
    AutoReplySettings(
        enabled=current.enabled,
        prompt=AutoReplySettings.DEFAULT_PROMPT,
        pace=current.pace,
        pace_params=current.pace_profile.model_dump(),
        confirm_before_send=current.confirm_before_send,
        follow_up_hours=current.follow_up_hours,
        block_proactive_hr=current.block_proactive_hr,
    ).save()
    return {"prompt": AutoReplySettings.DEFAULT_PROMPT}


# --- 黑名单 ---


@app.get("/api/blacklist")
def get_blacklist() -> list[dict]:
    return CompanyBlacklistRow.list_dicts()


class BlacklistIn(BaseModel):
    name: str


@app.post("/api/blacklist")
def add_blacklist(body: BlacklistIn) -> list[dict]:
    name = body.name.strip()
    if not name:
        raise ApiError("请输入公司名或岗位关键词")
    CompanyBlacklistRow.add(name=name, reason="手动添加")
    return CompanyBlacklistRow.list_dicts()


@app.delete("/api/blacklist/{key}")
def remove_blacklist(key: str) -> list[dict]:
    if not CompanyBlacklistRow.remove(key):
        raise ApiError("移出失败，请稍后再试")
    return CompanyBlacklistRow.list_dicts()


# --- AI 服务 ---


class LlmIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


@app.put("/api/llm")
def put_llm(body: LlmIn) -> dict:
    current = LlmSettings.load()
    base_url = body.base_url
    api_key = _resolve_api_key(body.api_key, base_url=base_url, current=current)
    # 换服务后旧模型列表不再适用（与原 set_base_url 一致）
    if base_url.strip() != current.base_url.strip():
        LlmSettings(
            base_url=base_url, api_key=api_key, model="", models=[]
        ).save()
    else:
        LlmSettings(
            base_url=base_url, api_key=api_key, model=body.model, models=current.models
        ).save()
    return llm_bundle()


@app.post("/api/llm/fetch-models")
async def fetch_models(body: LlmIn) -> dict:
    current = LlmSettings.load()
    api_key = _resolve_api_key(body.api_key, base_url=body.base_url, current=current)
    settings = LlmSettings(base_url=body.base_url, api_key=api_key, model=body.model)
    if not settings.api_key:
        raise ApiError("请先填写 API Key")
    try:
        models = await settings.fetch_models()
    except APIError as exc:
        raise ApiError(f"获取失败，请检查接口地址和 API Key：{exc.message}") from exc
    if models:
        current_model = body.model if body.model in models else models[0]
        LlmSettings(
            base_url=body.base_url, api_key=api_key, model=current_model, models=models
        ).save()
    return {"models": models}


@app.post("/api/llm/test")
async def test_llm(body: LlmIn) -> dict:
    current = LlmSettings.load()
    api_key = _resolve_api_key(body.api_key, base_url=body.base_url, current=current)
    settings = LlmSettings(base_url=body.base_url, api_key=api_key, model=body.model)
    if not settings.ready:
        raise ApiError("请先填写 API Key 并选择模型")
    try:
        reply = await settings.ping()
    except APIError as exc:
        raise ApiError(f"连接失败：{exc.message}") from exc
    except Exception as exc:  # noqa: BLE001 网络层之外的意外也要给出可读提示
        raise ApiError(f"连接失败：{log_exception('api', exc)}") from exc
    preview = reply[:20] if reply else "（空回复）"
    return {"preview": preview}


# ---------------------------------------------------------------------------
# 岗位管理
# ---------------------------------------------------------------------------


@app.get("/api/jobs")
def list_jobs(
    analysis: str = JobRow.ANALYSIS_FILTERS[0],
    suitable: str = JobRow.SUITABLE_FILTERS[1],
    search: str = "",
    focus: str = "",
    sort: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    init_db()
    if sort not in JobRow.SORTS:  # 未知排序按默认（新入库优先）处理
        sort = ""
    filters: dict[str, Any] = {
        "search": search,
        "analysis": analysis,
        "suitable": suitable,
        "uid": focus,
    }
    total = JobRow.count(**filters)
    page_size = max(1, page_size)
    max_page = max(1, -(-total // page_size))
    page = min(max(1, page), max_page)
    rows = JobRow.list_dicts(
        **filters, sort=sort, limit=page_size, offset=(page - 1) * page_size
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "rows": rows,
        "analysis_options": list(JobRow.ANALYSIS_FILTERS),
        "suitable_options": list(JobRow.SUITABLE_FILTERS),
    }


@app.get("/api/jobs/{uid}")
def job_detail(uid: str) -> dict:
    job = JobRow.get_dict(uid)
    if job is None:
        raise HTTPException(404, "岗位不存在或已删除")
    return job


@app.delete("/api/jobs/{uid}")
def delete_job(uid: str) -> dict:
    JobRow.delete_by_uid(uid)
    return {"ok": True}


class UidsIn(BaseModel):
    uids: list[str]


@app.post("/api/jobs/delete-many")
def delete_many(body: UidsIn) -> dict:
    count = JobRow.delete_many(list(body.uids))
    return {"deleted": count}


@app.post("/api/jobs/{uid}/applied")
def mark_applied(uid: str) -> dict:
    if not JobRow.set_applied(uid, reason="已在原平台手动投递"):
        raise ApiError("记账失败，请刷新后重试")
    return {"ok": True}


@app.post("/api/jobs/analyze")
async def analyze_jobs(body: UidsIn) -> dict:
    progress = runtime.analyze_jobs(list(body.uids))
    return progress


class CompanyScanIn(BaseModel):
    """批量查企业入参：``uids`` 为空表示全部岗位；``only_missing`` 只补没查过的。"""

    uids: list[str] = Field(default_factory=list)
    only_missing: bool = False


@app.post("/api/jobs/company-scan")
async def company_scan_jobs(body: CompanyScanIn) -> dict:
    """批量查企业背景：去重后逐家查询，跳过查过的（见 ``Runtime.company_scan``）。"""
    return runtime.company_scan(list(body.uids), only_missing=body.only_missing)


@app.get("/api/jobs/export")
def export_jobs(uids: str = "") -> Response:
    ids = [u for u in uids.split(",") if u]
    text, _count = JobRow.to_json(ids or None)
    return _download(text, f"jobs-{_now_stamp()}.json", "application/json")


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


@app.get("/api/messages/conversations")
def conversations(search: str = "", status: str = "全部") -> dict:
    init_db()
    ChatMessageRow.purge_system_cards()
    hours = AutoReplySettings.load().follow_up_hours
    if status == "待跟进":
        # 待跟进是本地筛选：先取全部会话，再只留超时未回的
        items = [
            c
            for c in ChatMessageRow.conversations(search, "", follow_up_hours=hours)
            if c.get("follow_up")
        ]
    else:
        items = ChatMessageRow.conversations(search, status, follow_up_hours=hours)
    return {
        "conversations": items,
        "pending": ReplyPendingRow.list_dicts(),
        "status_filters": ["全部", "无状态", "所有面试", "待跟进", *ChatStatusRow.LABELS.values()],
        "status_choices": list(ChatStatusRow.CHOICES),
    }


@app.get("/api/messages/{boss_id}/chat")
def chat_messages(boss_id: str) -> list[dict]:
    return ChatMessageRow.list_for_boss(boss_id)


@app.put("/api/messages/{boss_id}/status")
def set_chat_status(boss_id: str, body: dict[str, Any]) -> dict:
    label = str(body.get("label") or "")
    status = next((k for k, v in ChatStatusRow.LABELS.items() if v == label), "")
    if status:
        ChatStatusRow.mark(boss_id, status)
    else:
        ChatStatusRow.clear(boss_id)
    return {"ok": True}


@app.post("/api/messages/pending/{row_id}/approve")
def approve_pending(row_id: str) -> dict:
    if not ReplyPendingRow.decide(row_id, "approved"):
        raise ApiError("这条回复已处理，请刷新")
    return {"ok": True}


@app.post("/api/messages/pending/{row_id}/dismiss")
def dismiss_pending(row_id: str) -> dict:
    ReplyPendingRow.decide(row_id, "dismissed")
    return {"ok": True}


@app.get("/api/messages/export")
def export_messages(boss_ids: str = "") -> Response:
    ids = [b for b in boss_ids.split(",") if b]
    text, _count = ChatMessageRow.to_json(ids or None)
    return _download(text, f"chats-{_now_stamp()}.json", "application/json")


# ---------------------------------------------------------------------------
# 数据看板
# ---------------------------------------------------------------------------


@app.get("/api/stats")
def get_stats() -> dict:
    init_db()
    funnel = stats_mod.funnel()
    total = funnel.get("total") or 0
    applied = funnel.get("applied") or 0
    applied_rate = "—" if total == 0 else f"{applied / total * 100:.0f}%"
    interviews = funnel.get("interviews") or 0
    interview_rate = "—" if applied == 0 else f"{interviews / applied * 100:.0f}%"

    daily = stats_mod.daily_applied()
    peak = max((d["count"] for d in daily), default=0)
    daily_out = [
        {
            **d,
            "pct_str": stats_mod.pct(d["count"], peak),
            "max": d["count"] == peak and d["count"] > 0,
        }
        for d in daily
    ]
    dist = stats_mod.status_distribution()
    dist_total = sum(d["count"] for d in dist)
    status_out = [{**d, "pct_str": stats_mod.pct(d["count"], dist_total)} for d in dist]

    response = stats_mod.response_times()
    waiting = int(response.get("waiting") or 0)
    replied = int(response.get("replied") or 0)
    if replied + waiting == 0:
        note = "发出第一条消息后开始统计"
    elif waiting == 0:
        note = f"基于 {replied} 个已回复的会话"
    else:
        note = f"基于 {replied} 个已回复的会话；{waiting} 个还在等，未计入中位数"
    median = response.get("median")
    median_label = "暂无已回复样本" if median is None else f"{median} 小时"

    buckets = [
        {
            **b,
            "rate_label": f"{b['rate']}%" if b["rate"] is not None else "样本不足",
            "has_rate": b["rate"] is not None and b["rate"] > 0,
        }
        for b in stats_mod.score_buckets()
    ]
    return {
        "funnel": funnel,
        "applied_rate": applied_rate,
        "interview_rate": interview_rate,
        "daily": daily_out,
        "daily_total": sum(d["count"] for d in daily),
        "status_dist": status_out,
        "response": {"median_label": median_label, "note": note},
        "buckets": buckets,
    }


# ---------------------------------------------------------------------------
# 查企业
# ---------------------------------------------------------------------------


def _company_view(job_uid: str, company: str) -> dict:
    job = JobRow.get_dict(job_uid)
    brand_id = str(job.get("brandId") or "") if job else ""
    report = (CompanyRow.get_dict(brand_id) if brand_id else None) or {}
    name = str(report.get("name") or company)
    return {
        "job_uid": job_uid,
        "company": company,
        "report": report,
        "blacklisted": CompanyBlacklistRow.blocked(brand_id, name) is not None,
    }


@app.get("/api/company/{job_uid}")
def get_company(job_uid: str, company: str = "") -> dict:
    init_db()
    return _company_view(job_uid, company)


@app.post("/api/company/{job_uid}/check")
async def check_company(job_uid: str) -> dict:
    report = await runtime.company_check(job_uid)
    view = _company_view(job_uid, str(report.get("name") or ""))
    return view


class CompanyBlacklistIn(BaseModel):
    brand_id: str = ""
    name: str


@app.post("/api/company/toggle-blacklist")
def toggle_company_blacklist(body: CompanyBlacklistIn) -> dict:
    """把当前公司拉黑 / 移出黑名单（与原 CompanyState.toggle_blacklist 一致）。"""
    brand_id, name = body.brand_id, body.name
    if CompanyBlacklistRow.blocked(brand_id, name) is not None:
        key = brand_id or f"name:{name}"
        removed = CompanyBlacklistRow.remove(key)
        if not removed and brand_id:
            removed = CompanyBlacklistRow.remove(f"name:{name}")
        if removed:
            return {"blacklisted": False, "message": f"已把「{name}」移出黑名单"}
        raise ApiError("移出失败，请稍后再试")
    if CompanyBlacklistRow.add(brand_id=brand_id, name=name, reason="手动拉黑"):
        return {"blacklisted": True, "message": f"已拉黑「{name}」，之后投递会自动跳过它的岗位"}
    raise ApiError("公司信息不足，无法拉黑")


# ---------------------------------------------------------------------------
# 简历工坊
# ---------------------------------------------------------------------------


@app.get("/api/tailor/{job_uid}")
def tailor_view(job_uid: str) -> dict:
    """打开工坊：带出岗位信息；之前生成过的简历直接展示。"""
    init_db()
    job = JobRow.get_dict(job_uid)
    if job is None:
        raise HTTPException(404, "岗位不存在或已删除")
    title = str(job.get("title") or "")
    company = str(job.get("company") or "")
    salary = str(job.get("salary") or "")
    platform = str(job.get("platform") or "boss")
    saved = load_saved_resume(platform, job_uid)
    return {
        "job_uid": job_uid,
        "job_label": f"{title} · {company} · {salary or '薪资未知'}",
        "content": saved or "",
        "issues": [],
    }


@app.post("/api/tailor/{job_uid}/generate")
async def tailor_generate(job_uid: str) -> dict:
    job = JobRow.get_dict(job_uid)
    if job is None:
        raise HTTPException(404, "岗位不存在或已删除")
    config = SearchConfigRow.load()
    resume = str(config.get("resume_text") or "").strip()
    if not resume:
        raise ApiError("请先在「配置中心 → 简历配置」上传简历")
    llm = LlmSettings.load()
    if not llm.ready:
        raise ApiError("请先在配置中心开通「AI 服务」")
    tailor = ResumeTailor(
        resume=resume,
        title=str(job.get("title") or ""),
        company=str(job.get("company") or ""),
        salary=str(job.get("salary") or ""),
        description=str(job.get("description") or ""),
        requirement=str(config.get("ai_requirement") or ""),
        llm=llm,
    )
    try:
        result = await tailor.generate()
    except AgentRunError as exc:
        raise ApiError(f"AI 调用失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"生成失败：{log_exception('api', exc)}") from exc
    platform = str(job.get("platform") or "boss")
    save_custom_resume(platform, job_uid, result.content)
    return {"content": result.content, "issues": result.issues}


@app.get("/api/tailor/{job_uid}/export")
def tailor_export(job_uid: str, content: str = "") -> Response:
    text = content
    if not text:
        job = JobRow.get_dict(job_uid)
        if job is None:
            raise HTTPException(404, "岗位不存在或已删除")
        platform = str(job.get("platform") or "boss")
        saved = load_saved_resume(platform, job_uid) or ""
        text = saved
    if not text:
        raise ApiError("还没有生成结果")
    return _download(text, f"resume-{_now_stamp()}.txt", "text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# 网申助手
# ---------------------------------------------------------------------------


def apply_profile_bundle() -> dict:
    """网申资料：配置中心填的结构化字段 + 简历正文兜底。"""
    settings = ApplyProfileSettings.load()
    resume = str(SearchConfigRow.load().get("resume_text") or "")
    profile = ApplyProfile.from_sources(settings, resume)
    return {
        "labels": ApplyProfileSettings.LABELS,
        "values": {
            key: str(getattr(settings, key, "") or "") for key in ApplyProfileSettings.LABELS
        },
        "extra": dict(settings.extra),
        "ready": settings.ready,
        "entries": [entry.to_dict() for entry in profile.entries],
    }


@app.get("/api/config/apply-profile")
def get_apply_profile() -> dict:
    init_db()
    return apply_profile_bundle()


class ApplyProfileIn(BaseModel):
    # 键为字段名（自定义补充字段用 ``extra:名称``），只更新传上来的键
    values: dict[str, str] = Field(default_factory=dict)


@app.put("/api/config/apply-profile")
def put_apply_profile(body: ApplyProfileIn) -> dict:
    """保存网申资料：没传的字段保持原样，空的补充字段直接删掉。"""
    data = ApplyProfileSettings.load().model_dump()
    for key, raw in body.values.items():
        text = str(raw or "").strip()
        if key in ApplyProfileSettings.LABELS:
            data[key] = text
        elif key.startswith("extra:"):
            name = key.removeprefix("extra:").strip()
            if not name:
                continue
            if text:
                data["extra"][name] = text
            else:
                data["extra"].pop(name, None)
    ApplyProfileSettings(**data).save()
    return apply_profile_bundle()


def _apply_url(raw: str) -> str:
    """校验网申地址：只放行 http(s)，别把浏览器指到本地文件之类的地址上。"""
    url = (raw or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ApiError("网申地址要以 http:// 或 https:// 开头")
    return url


async def _apply_call(what: str, call: Callable[[], Awaitable[dict]]) -> dict:
    """把网申流程里的异常统一翻成可读提示（路由层转成 HTTP 400）。"""
    try:
        return await call()
    except ApplyError as exc:
        raise ApiError(str(exc)) from exc
    except (RuntimeError, PlaywrightError) as exc:
        raise ApiError(f"{what}失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 兜底给出可读提示
        raise ApiError(f"{what}失败：{log_exception('apply', exc)}") from exc


@app.get("/api/apply/tasks")
def apply_tasks(status: str = "") -> list[dict]:
    """网申任务列表，新加的在前；``status`` 非空时只看该状态。"""
    init_db()
    return ApplicationRow.list_dicts(status=status)


class ApplyTaskIn(BaseModel):
    job_uid: str
    url: str = ""


@app.post("/api/apply/task")
async def apply_open(body: ApplyTaskIn) -> dict:
    """建/取网申任务并打开企业官网，扫一遍表单返回字段对照表。"""
    uid = (body.job_uid or "").strip()
    if not uid:
        raise ApiError("缺少岗位 uid")
    url = _apply_url(body.url)
    runtime.push_log(f"网申：正在打开 {url or '上次填过的网申地址'}…")
    return await _apply_call("打开网申页面", lambda: runtime.apply_assistant.open_task(uid, url))


class ApplyUrlIn(BaseModel):
    url: str = ""


@app.put("/api/apply/task/{uid}/url")
def apply_set_url(uid: str, body: ApplyUrlIn) -> dict:
    """改网申地址：岗位里没有官网链接时，由用户在网申页手填。"""
    init_db()
    saved = ApplicationRow.set_url(uid, _apply_url(body.url))
    if saved is None:
        raise HTTPException(404, "网申任务不存在")
    return saved


@app.post("/api/apply/task/{uid}/scan")
async def apply_scan(uid: str) -> dict:
    """重扫当前页面：官网表单常在填了一栏之后才展开下一栏。"""
    return await _apply_call("扫描表单", lambda: runtime.apply_assistant.scan(uid))


class ApplyFillIn(BaseModel):
    # 前端改过的行：``[{"index": 3, "key": "name", "filled": "张三"}]``
    fields: list[dict[str, Any]] = Field(default_factory=list)


@app.post("/api/apply/task/{uid}/fill")
async def apply_fill(uid: str, body: ApplyFillIn) -> dict:
    """把对照表填进页面；只填不提交，提交按钮永远留给用户点。"""
    result = await _apply_call("填写表单", lambda: runtime.apply_assistant.fill(uid, body.fields))
    outcome = result.get("outcome") or {}
    runtime.push_log(
        f"网申填表：已填 {outcome.get('filled', 0)} 项，"
        f"跳过 {outcome.get('skipped', 0)} 项，"
        f"待手动 {outcome.get('failed', 0)} 项"
    )
    return result


@app.post("/api/apply/task/{uid}/submit")
async def apply_submit(uid: str) -> dict:
    """标记为已提交（用户在浏览器里自己点了提交之后），并关掉这个标签页。"""
    saved = await _apply_call("标记提交", lambda: runtime.apply_assistant.submit(uid))
    runtime.push_log(f"网申已完成：{saved.get('title') or saved.get('uid') or ''}")
    return saved


@app.delete("/api/apply/task/{uid}")
async def apply_delete(uid: str) -> dict:
    if not await runtime.apply_assistant.delete(uid):
        raise HTTPException(404, "网申任务不存在")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 前端静态伺服（放最后注册，避免遮挡 /api 与 /ws）
# ---------------------------------------------------------------------------

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

if (FRONTEND_DIST / "index.html").is_file():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa(spa_path: str) -> FileResponse:
        candidate = (FRONTEND_DIST / spa_path).resolve()
        # 目录穿越防护：解析后的路径必须仍在 dist 之内
        if spa_path and candidate.is_file() and candidate.is_relative_to(FRONTEND_DIST.resolve()):
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
