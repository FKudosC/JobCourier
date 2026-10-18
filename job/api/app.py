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
from job.apply import knowledge
from job.apply.assistant import ApplyError
from job.apply.profile import ApplyProfile, learned_values
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
from job.models.application import ApplicationRow, ApplyAliasRow, ApplyLearnedRow
from job.models.blacklist import CompanyBlacklistRow
from job.models.chat import ChatMessageRow, ChatStatusRow
from job.models.company import CompanyRow
from job.models.job import JobRow
from job.models.pending import ReplyPendingRow
from job.models.search import DEFAULT_MIN_SCORE, LIST_FIELDS, SearchConfigRow
from job.models.setting import (
    ApplyAutoSettings,
    ApplyLocateSettings,
    ApplyProfileSettings,
    ApplyVisionSettings,
    AutoReplySettings,
    CaptchaSettings,
    LlmSettings,
    TailorSettings,
    TelemetrySettings,
)
from job.utils import log_exception
from job.utils.llm import explain
from job.utils.resume import IMAGE_SUFFIXES, ResumePdf

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
        "resume_size": _file_size(str(config.get("resume_path") or "")),
        "resume_text": str(config.get("resume_text") or ""),
        "photo_path": str(config.get("photo_path") or ""),
        "photo_size": _file_size(str(config.get("photo_path") or "")),
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
        "captcha": captcha_bundle(),
        "locate": locate_bundle(),
        "vision": vision_bundle(),
        "auto": auto_bundle(),
        "telemetry": telemetry_bundle(),
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
    return "*" * 12 if (key or "").strip() else ""


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


def captcha_bundle() -> dict:
    """验证码识别配置：开关与识图模型（留空用大模型服务里的主模型）。"""
    settings = CaptchaSettings.load()
    return {"enabled": settings.enabled, "model": settings.model}


def locate_bundle() -> dict:
    """表单定位兜底配置：默认关闭，开着也只处理几个栏位。"""
    settings = ApplyLocateSettings.load()
    return {
        "enabled": settings.enabled,
        "max_fields": settings.max_fields,
        "timeout": settings.timeout,
    }


def vision_bundle() -> dict:
    """表单视觉核对配置：默认开启，但没配大模型就自动跳过。"""
    settings = ApplyVisionSettings.load()
    return {
        "enabled": settings.enabled,
        "model": settings.model,
        "timeout": settings.timeout,
        "max_tiles": settings.max_tiles,
        "mask_sensitive": settings.mask_sensitive,
    }


def auto_bundle() -> dict:
    """网申填表配置：文件代传与学习开关；交付口径决定填完怎么把缺项交回给人。"""
    settings = ApplyAutoSettings.load()
    return {
        "handoff": settings.handoff,
        "upload_files": settings.upload_files,
        "learn_web": settings.learn_web,
        "learn_ai": settings.learn_ai,
    }


def telemetry_bundle() -> dict:
    """链路追踪配置：默认全关，开启后每次模型调用产出一条可回放的 span。"""
    settings = TelemetrySettings.load()
    return {
        "enabled": settings.enabled,
        "exporter": settings.exporter,
        "endpoint": settings.endpoint,
        "content": settings.content,
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
    photo_path: str = ""


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
        photo_path=body.photo_path,
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


@app.post("/api/config/photo/upload")
async def upload_photo(file: UploadFile = File(...)) -> dict:
    """上传生活照：只保存原件（不做 OCR），供网申表单自动上传。"""
    data = await file.read()
    name = file.filename or "photo.jpg"
    if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
        raise ApiError("生活照请上传图片（jpg / png）")
    path = str(ResumePdf.save_photo(name, data))
    SearchConfigRow.save(photo_path=path)
    return {"photo_path": path}


def _file_size(path: str) -> int:
    """本地文件字节数；文件不在了就返回 0，界面据此不显示大小。"""
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


# 简历 / 寸照的预览只放行这两个目录，别的一律挡掉（防目录穿越）。
_PREVIEW_DIRS = (ResumePdf.DIR, ResumePdf.PHOTO_DIR)


@app.get("/api/config/file")
def get_config_file(path: str = "") -> FileResponse:
    """预览上传过的简历 / 寸照：图片、PDF 直接交给浏览器显示。"""
    if not path:
        raise HTTPException(404, "文件不存在")
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise HTTPException(404, "文件不存在") from exc
    if not resolved.is_file():
        raise HTTPException(404, "文件不存在")
    if not any(resolved.is_relative_to(root.resolve()) for root in _PREVIEW_DIRS):
        raise HTTPException(403, "不允许访问该路径")
    return FileResponse(resolved)


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


class CaptchaIn(BaseModel):
    enabled: bool = False
    model: str = ""


@app.put("/api/config/captcha")
def put_captcha(body: CaptchaIn) -> dict:
    """保存验证码识别设置：开关 + 识图模型。"""
    CaptchaSettings(enabled=body.enabled, model=body.model.strip()).save()
    return captcha_bundle()


class LocateIn(BaseModel):
    enabled: bool = False
    # 护栏：一次扫描最多几个栏位、每个栏位多少秒，越界一律夹到安全区间
    max_fields: int = 3
    timeout: int = 15


@app.put("/api/config/locate")
def put_locate(body: LocateIn) -> dict:
    """保存表单定位兜底设置：默认关闭，个数与超时都夹在安全区间里。"""
    ApplyLocateSettings(
        enabled=body.enabled,
        max_fields=min(10, max(1, int(body.max_fields))),
        timeout=min(60, max(5, int(body.timeout))),
    ).save()
    return locate_bundle()


class VisionIn(BaseModel):
    enabled: bool = True
    model: str = ""
    # 护栏：一次核对多少秒、最多甩给模型几屏截图，越界一律夹到安全区间
    timeout: int = 40
    max_tiles: int = 4
    mask_sensitive: bool = True


@app.put("/api/config/vision")
def put_vision(body: VisionIn) -> dict:
    """保存表单视觉核对设置：默认开启，超时与分片数都夹在安全区间里。"""
    ApplyVisionSettings(
        enabled=body.enabled,
        model=body.model.strip(),
        timeout=min(120, max(5, int(body.timeout))),
        max_tiles=min(12, max(1, int(body.max_tiles))),
        mask_sensitive=body.mask_sensitive,
    ).save()
    return vision_bundle()


class AutoIn(BaseModel):
    # 交付口径：只认 loose / balanced / strict，别的值一律退回 balanced
    handoff: str = "balanced"
    upload_files: bool = True
    # 学不学：手填的那几条 / 认可的 AI 推算，各一个开关
    learn_web: bool = True
    learn_ai: bool = True


@app.put("/api/config/auto")
def put_auto(body: AutoIn) -> dict:
    """保存网申填表设置：交付口径夹在安全枚举里。"""
    level = body.handoff.strip()
    ApplyAutoSettings(
        handoff=level if level in ("loose", "balanced", "strict") else "balanced",
        upload_files=body.upload_files,
        learn_web=body.learn_web,
        learn_ai=body.learn_ai,
    ).save()
    return auto_bundle()


class TelemetryIn(BaseModel):
    enabled: bool = False
    # 只认 console / otlp 两种导出方式，别的值一律退回 console
    exporter: str = "console"
    endpoint: str = ""
    content: bool = False


@app.put("/api/config/telemetry")
def put_telemetry(body: TelemetryIn) -> dict:
    """保存链路追踪设置。TracerProvider 全局只装一次，改完需重启服务才生效。"""
    TelemetrySettings(
        enabled=body.enabled,
        exporter="otlp" if body.exporter.strip() == "otlp" else "console",
        endpoint=body.endpoint.strip(),
        content=body.content,
    ).save()
    return telemetry_bundle()


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
    """批量查企业入参：要查的岗位 ID 列表。"""

    uids: list[str] = Field(default_factory=list)


@app.post("/api/jobs/company-scan")
async def company_scan_jobs(body: CompanyScanIn) -> dict:
    """批量查企业背景：去重后逐家查询，跳过 7 天内查过的（见 ``Runtime.company_scan``）。"""
    return runtime.company_scan(list(body.uids))


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
    """网申资料：配置中心填的结构化字段 + 简历正文兜底 + 本机学会的字段。"""
    settings = ApplyProfileSettings.load()
    resume = str(SearchConfigRow.load().get("resume_text") or "")
    profile = ApplyProfile.from_sources(settings, resume, learned=learned_values())
    learned_rows = _dedupe_learned(ApplyLearnedRow.rows())
    labels = dict(ApplyProfileSettings.LABELS)
    # 取值用 profile 的：配置里没填、从简历正文兜底捞出来的那份也要带上，
    # 否则对照表里换一条资料时，值会被悄悄清空
    values = {key: str(value or "") for key, value in profile.values().items()}
    for key in ApplyProfileSettings.LABELS:
        values.setdefault(key, "")
    return {
        "labels": labels,
        "values": values,
        "ready": settings.ready,
        # 界面按组渲染 100+ 个字段，分组信息由后端给，前端不再各存一份。
        "groups": [
            {"title": title, "keys": [key for key, _ in fields]}
            for title, fields in ApplyProfileSettings.GROUPS
        ],
        "entries": [entry.to_dict() for entry in profile.entries],
        # 本机学会的字段：在别家官网手工补过、资料库里本来没有的那几条。
        "learned": _learned_for_display(learned_rows),
        # 信息树：分组 → 字段 → 别名，给配置中心的「树形」视图用（和上面的
        # labels / values 同源，只是把「这条打哪来、还认下过哪些叫法」一并摆出来）。
        "tree": _apply_tree(settings, profile, learned_rows),
    }


@app.get("/api/config/apply-profile")
def get_apply_profile() -> dict:
    init_db()
    return apply_profile_bundle()


def _apply_tree(
    settings: ApplyProfileSettings,
    profile: ApplyProfile,
    learned_rows: list[dict],
) -> dict:
    """信息树：分组 → 字段 → 别名，给配置中心的「树形」视图用。

    和 :func:`apply_profile_bundle` 的 labels / values 同源，多出来的是三样东西：
    每条资料的**来源**（资料 / 简历 / 推算 / 记忆 / 自定义）、**推算依据**，以及
    这条认下过的**别名**（共指项）。字段本身是叶子，可在树上就地改；别名可以
    手动合并 / 撤销——合并结果交给 :func:`job.apply.knowledge.canonical_key`，
    这样「专业类别 / 专业类 / major_category」不会在库里存成三条。
    """
    entries = {entry.key: entry for entry in profile.entries}
    # 待归并：内置别名表认不出、用户也还没合并过的那些键（官网怪叫法）
    candidates = (
        list(ApplyProfileSettings.LABELS)
        + [str(row.get("key") or "") for row in learned_rows]
    )
    orphans = set(knowledge.orphan_keys(candidates))
    merged = knowledge.user_aliases()

    def leaf(key: str, label: str, value: str, source: str, note: str, *, kind: str) -> dict:
        """一个字段节点。"""
        canonical = knowledge.canonical_key(key) or key
        aliases = [
            {"text": alias, "builtin": alias in knowledge.KEY_ALIASES.get(canonical, ())}
            for alias in knowledge.aliases_of(key)
        ]
        return {
            "key": key,
            "label": label or key,
            "value": str(value or ""),
            "filled": bool(str(value or "").strip()),
            "source": source,
            "note": note,
            "aliases": aliases,
            "canonical": canonical,
            # 待归并：这条的叫法内置表认不出、用户也没合并过，树上标一下请人指认
            "orphan": key in orphans and canonical == key,
            "kind": kind,
            # 这条的值是本机在别的官网学到 / 模型推出来的（不是资料里写的）
            "memory": False,
            # 这条的值是模型推的、用户还没核对过（树上标「待核对」）
            "pending": False,
        }

    # 学到 / 推出来的那批按规范键归拢：一个规范键只留最近的一条（手填的压过推算的）。
    # 官网上「身高(厘米)」和内置的「身高」是同一件事，同义归并后键会撞在一起；各长一片
    # 叶子的话，库里就会并排出现两条同值资料。
    memory: dict[str, dict] = {}
    for row in learned_rows:
        leaf_row = _leaf_from_learned(row)
        current = memory.get(leaf_row["canonical"])
        if current is None or (current["source"] == "AI推算" and leaf_row["source"] != "AI推算"):
            memory[leaf_row["canonical"]] = leaf_row

    builtin_keys = set(ApplyProfileSettings.LABELS)
    # 内置分组里已经有值的那几个规范键：AI 推理不必再为它们另起一片叶子
    settled: set[str] = set()

    groups: list[dict] = []
    for title, fields in ApplyProfileSettings.GROUPS:
        rows = []
        for key, label in fields:
            entry = entries.get(key)
            value = entry.value if entry else ""
            source = entry.source if entry else "未填"
            note = entry.note if entry else ""
            canonical = knowledge.canonical_key(key) or key
            memo = memory.get(canonical)
            node = leaf(key, label, value, source, note, kind="field")
            if memo is not None and node["source"] == "记忆":
                # 这条的值不是资料里写的，是本机学到 / 模型推的。资料组装时一律写成
                # 「记忆」，这里按学到的那条还原来源，模型推的还得留个「待核对」。
                _absorb(node, memo)
            if memo is not None and not node["filled"]:
                # 资料里空着，但本机在别的官网学到过：并到同一个叶子上，不再单独长一片。
                # 这样「身高」在库里只有一条，填的时候也还是同一个键。
                node["value"] = memo["value"]
                node["filled"] = True
                _absorb(node, memo)
            if node["filled"]:
                settled.add(canonical)
            rows.append(node)
        groups.append({"title": title, "kind": "group", "fields": rows})

    def learned_leaves(rows: list[dict], skip: set[str]) -> list[dict]:
        """学到的一批摊成叶子，同一个规范键只留一条；``skip`` 里的规范键上面已经并过了。

        ``learned_rows`` 已按时间倒序，所以同一个键留下的是最近学到的那条。
        """
        out: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            leaf_row = _leaf_from_learned(row)
            canonical = leaf_row["canonical"]
            if canonical in seen or canonical in skip:
                continue
            seen.add(canonical)
            out.append(leaf_row)
        return out

    # 内置字典里有的键，值已经并进上面那片叶子了，不再重复长一片
    learned = learned_leaves(
        [row for row in learned_rows if row.get("source") != "AI推算"],
        skip=builtin_keys,
    )
    if learned:
        groups.append({"title": "本机学会", "kind": "learned", "fields": learned})

    # AI 推理只留「库里还空着」的那些：已经有值（资料写的或本机学到的）就不必再核对了
    inferred = learned_leaves(
        [row for row in learned_rows if row.get("source") == "AI推算"],
        skip=settled,
    )
    if inferred:
        groups.append({"title": "AI 推理（待核对）", "kind": "inferred", "fields": inferred})

    return {
        "groups": groups,
        "merges": dict(merged),
        "orphans": sorted(orphans),
    }


def _leaf_from_learned(row: dict) -> dict:
    """把一条「本机学会的字段」摊成树上的叶子节点。"""
    key = str(row.get("key") or "")
    # 官网上那一栏叫什么，不等于这条资料叫什么。学到「身高(厘米)」时树上该按内置的
    # 「身高」显示——否则同一件事会在树上并排出现「身高」和「身高(厘米)」两条。
    canonical = knowledge.canonical_key(key) or key
    aliases = [
        {"text": alias, "builtin": alias in knowledge.KEY_ALIASES.get(canonical, ())}
        for alias in knowledge.aliases_of(key)
    ]
    return {
        "key": key,
        "label": ApplyProfileSettings.LABELS.get(canonical) or str(row.get("label") or "") or key,
        "value": str(row.get("value") or ""),
        "filled": True,
        "source": str(row.get("source") or "手动"),
        "note": str(row.get("basis") or ""),
        "aliases": aliases,
        "canonical": canonical,
        "orphan": False,
        "kind": "learned",
        # 这一栏整枝就是「待核对」，叶子本身不用再标一遍
        "pending": False,
        "domain": str(row.get("domain") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


# 学到的那条没写「依据」时，按来源给一句对得上的兜底说明
_MEMORY_NOTE = {
    "AI推算": "模型按你已有的资料推的，还没核对过",
    "手动": "在别的官网手工填过，本机记下来了",
}


def _absorb(node: dict, memo: dict) -> None:
    """把「本机学到的那条」的来龙去脉挪到内置字段这片叶子上（就地改 ``node``）。

    学到的东西本来就该归到它那条资料上，别再另长一片叶子——否则「身高」在库里
    会有两条。来源、依据、站点、时间都得跟着过来，用户才知道这条打哪来、能不能删；
    模型推的还要挂个「待核对」。
    """
    node["source"] = memo["source"]
    node["note"] = memo["note"] or _MEMORY_NOTE.get(memo["source"], "本机记下来了")
    node["memory"] = True
    node["pending"] = memo["source"] == "AI推算"
    node["forget_key"] = memo["key"]
    node["domain"] = memo.get("domain", "")
    node["updated_at"] = memo.get("updated_at", "")


def _dedupe_learned(rows: list[dict]) -> list[dict]:
    """同一个规范键只留一条。

    官网上那栏叫「身高(厘米)」，库里叫 ``height``，同一件事；两条并排摆着就是重复。
    ``rows`` 已按更新时间倒序，所以留下的是最近那条，手填的压过模型推的。
    """
    best: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        raw = str(row.get("key") or "")
        canonical = knowledge.canonical_key(raw) or raw
        old = best.get(canonical)
        if old is None:
            best[canonical] = row
            order.append(canonical)
        elif old.get("source") == "AI推算" and row.get("source") != "AI推算":
            best[canonical] = row
    return [best[canonical] for canonical in order]


def _learned_for_display(rows: list[dict]) -> list[dict]:
    """配置中心里「本机学会」那一栏该列什么。

    资料库里本来就有这一栏的（身高、婚姻状况……）已经并回内置那片叶子上了，界面上
    就在它自己的分组里显示成「记忆」来源、模型推的还挂着「待核对」，这里再列一遍
    纯属重复——同一件事在同一屏里出现两次，用户会以为库里存了两条。

    留在这儿的只有内置字典里没有的键（官网自定的怪栏位）：那些在分组里没有归宿，
    只能单列一区，删也只能在这儿删。
    """
    out: list[dict] = []
    for row in _dedupe_learned(rows):
        raw = str(row.get("key") or "")
        canonical = knowledge.canonical_key(raw) or raw
        if canonical in ApplyProfileSettings.LABELS:
            continue
        out.append(row)
    return out


class ApplyAliasIn(BaseModel):
    """一条「这两个叫法指的是同一条资料」的合并关系。"""

    alias: str = ""
    key: str = ""


@app.post("/api/config/apply-alias")
def merge_apply_alias(body: ApplyAliasIn) -> dict:
    """把 ``alias`` 并到 ``key`` 上：之后归并、填写都认这一条。"""
    init_db()
    if not ApplyAliasRow.merge(body.alias, body.key):
        raise ApiError("这两条不能合并（叫法为空、彼此相同，或已经反向合并过）")
    knowledge.set_user_aliases(None)
    return apply_profile_bundle()


@app.delete("/api/config/apply-alias/{alias}")
def unmerge_apply_alias(alias: str) -> dict:
    """撤销一条手动合并关系。"""
    init_db()
    ApplyAliasRow.unmerge(alias)
    knowledge.set_user_aliases(None)
    return apply_profile_bundle()


class ApplyProfileIn(BaseModel):
    # 键为字段名，只更新传上来的键
    values: dict[str, str] = Field(default_factory=dict)


@app.put("/api/config/apply-profile")
def put_apply_profile(body: ApplyProfileIn) -> dict:
    """保存网申资料：没传的字段保持原样。"""
    data = ApplyProfileSettings.load().model_dump()
    for key, raw in body.values.items():
        text = str(raw or "").strip()
        if key in ApplyProfileSettings.LABELS:
            data[key] = text
    ApplyProfileSettings(**data).save()
    return apply_profile_bundle()


@app.delete("/api/config/apply-learned")
def clear_apply_learned() -> dict:
    """清空「本机学会的字段」：整表删掉，下次重新学。"""
    ApplyLearnedRow.clear()
    return apply_profile_bundle()


@app.delete("/api/config/apply-learned/{key}")
def forget_apply_learned(key: str) -> dict:
    """忘掉学到的某一条：用户觉得这条不该被记下来时用。"""
    ApplyLearnedRow.forget(key)
    return apply_profile_bundle()


class ApplyLearnConfirmIn(BaseModel):
    """「这条模型推得对，留下」的确认。"""

    key: str = ""


@app.post("/api/config/apply-learned/confirm")
def confirm_apply_learned(body: ApplyLearnConfirmIn) -> dict:
    """核对过一条 AI 推理：推得对的走这里，推得不对的走删除。

    确认之后树上那片叶子的「待核对」就撤掉了，来源从「AI推算」变成「手动」——
    对这条负责的从模型换成用户本人。
    """
    key = (body.key or "").strip()
    if not key:
        raise ApiError("缺少要确认的字段名")
    if not ApplyLearnedRow.confirm(key):
        raise ApiError("这条已经不在库里了，或者本来就不是模型推的")
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
    job_uid: str = ""
    url: str = ""


@app.post("/api/apply/task")
async def apply_open(body: ApplyTaskIn) -> dict:
    """建/取网申任务并打开企业官网，扫一遍表单返回字段对照表。

    不带 ``job_uid`` 时按独立任务处理：用户只给网址，公司 / 岗位名从页面自己认。
    """
    uid = (body.job_uid or "").strip()
    url = _apply_url(body.url)
    if not uid and not url:
        raise ApiError("请先填写企业官网的网申地址")
    runtime.push_log(f"网申：正在打开 {url or '上次填过的网申地址'}…")
    return await _apply_call(
        "打开网申页面", lambda: runtime.apply_assistant.open_task(uid, url, defer=True)
    )


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
    return await _apply_call(
        "扫描表单", lambda: runtime.apply_assistant.scan(uid, defer=True)
    )


class ApplyFillIn(BaseModel):
    # 前端改过的行：``[{"index": 3, "key": "name", "filled": "张三"}]``
    fields: list[dict[str, Any]] = Field(default_factory=list)
    # 用户在「AI 推理（请核对）」里删掉的行序号：不填进页面，也不回流进资料库
    rejected: list[int] = Field(default_factory=list)


@app.post("/api/apply/task/{uid}/confirm")
async def apply_confirm(uid: str) -> dict:
    """在对照表上确认清单：把任务往前推一关，之后填表 / 重启都能从这一关接着跑。"""
    saved = await _apply_call("确认清单", lambda: runtime.apply_assistant.confirm(uid))
    runtime.push_log(f"网申已确认：{saved.get('title') or saved.get('uid') or ''}")
    return saved


@app.post("/api/apply/task/{uid}/fill")
async def apply_fill(uid: str, body: ApplyFillIn) -> dict:
    """把对照表填进页面；只填不提交，提交按钮永远留给用户点。"""
    result = await _apply_call(
        "填写表单",
        lambda: runtime.apply_assistant.fill(uid, body.fields, body.rejected, defer=True),
    )
    if result.get("pending"):
        # 填表链（逐栏写 + AI 接手补全）少说一分钟，挂后台跑，跑完广播刷新面板
        runtime.push_log("网申填表：已开始往浏览器里填，填完会自动刷新")
        return result
    outcome = result.get("outcome") or {}
    runtime.push_log(
        f"网申填表：已填 {outcome.get('filled', 0)} 项，"
        f"跳过 {outcome.get('skipped', 0)} 项，"
        f"待手动 {outcome.get('failed', 0)} 项"
    )
    if outcome.get("flagged"):
        runtime.push_log(f"网申填表：官网提示 {outcome['flagged']} 项需要核对")
    learned = result.get("inferred") or []
    if learned:
        runtime.push_log(f"网申学习：认可 {len(learned)} 条 AI 推算")
    return result


@app.post("/api/apply/task/{uid}/submit")
async def apply_submit(uid: str) -> dict:
    """标记为已提交（用户在浏览器里自己点了提交之后），并关掉这个标签页。"""
    saved = await _apply_call("标记提交", lambda: runtime.apply_assistant.submit(uid))
    runtime.push_log(f"网申已完成：{saved.get('title') or saved.get('uid') or ''}")
    return saved


@app.post("/api/apply/task/{uid}/reopen")
async def apply_reopen(uid: str) -> dict:
    """撤销「已提交」：误点之后退回上一步，接着填表或重新提交。"""
    saved = await _apply_call("撤销提交", lambda: runtime.apply_assistant.reopen(uid))
    runtime.push_log(f"网申已撤销提交：{saved.get('title') or saved.get('uid') or ''}")
    return saved


@app.post("/api/apply/task/{uid}/captcha")
async def apply_captcha(uid: str) -> dict:
    """让 AI 读图识别验证码并填进输入框；识别不出就如实回报，请用户手动填。"""
    bundle = await _apply_call("识别验证码", lambda: runtime.apply_assistant.solve_captcha(uid))
    captcha = bundle.get("captcha") or {}
    runtime.push_log(f"验证码识别：{captcha.get('message') or ''}")
    return {"captcha": captcha, "task": bundle.get("task") or {}}


@app.post("/api/apply/task/{uid}/learn")
async def apply_learn(uid: str) -> dict:
    """学习手填内容：把官网页面和本机资料库逐栏比一遍，学新的、纠错的。"""
    bundle = await _apply_call("学习手填内容", lambda: runtime.apply_assistant.learn_now(uid))
    added = bundle.get("added") or []
    fixed = bundle.get("fixed") or []
    if added or fixed:
        runtime.push_log(f"网申学习：新学 {len(added)} 条、纠正 {len(fixed)} 条")
    return bundle


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
