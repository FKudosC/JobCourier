"""API 运行时：内核单例、事件总线与后台流程的控制器。

与内核的关系保持不变：本模块只做装配与转发——把求职配置装进引擎、把内核
回调（日志、步骤）广播给前端，投递/回复/查企业的全部逻辑仍在 ``job/boss``。
"""

from __future__ import annotations

import asyncio
import contextlib
import webbrowser
from collections import deque
from datetime import datetime

from patchright.async_api import Error as PlaywrightError
from pydantic_ai.exceptions import AgentRunError
from sqlalchemy.exc import SQLAlchemyError

from job.apply.assistant import ApplyAssistant
from job.boss.chat import ChatReplier, ChatResponder
from job.boss.company import (
    CompanyChecker,
    CompanyPage,
    CompanyPrefetcher,
    CompanyReviewer,
    CompanySearch,
)
from job.boss.filters import Defaults, KeywordFilter, PaceProfile
from job.boss.jobs import Job, JobScraper
from job.boss.resume_tailor import ResumeTailor, load_saved_resume, save_custom_resume
from job.boss.review import JobReviewer
from job.boss.salary import SalaryRange
from job.boss.session import BossSession
from job.models.company import CompanyRow
from job.models.job import JobRow
from job.models.search import SearchConfigRow
from job.models.setting import AutoReplySettings, BrowserSettings, LlmSettings, TailorSettings
from job.utils import log_exception

# 投递结束状态 → 状态文案与日志文案（与原工作台一致）
_STATUS = {
    "done": "已完成",
    "stopped": "已停止",
    "need_login": "需要登录",
    "limit": "今日已达上限",
}
_FINISHED = {
    "done": "所选城市的岗位都看完了",
    "stopped": "已停止投递",
    "need_login": "需要登录 BOSS，请在浏览器里登录后重新开始",
    "limit": "今日投递次数已用完，明天再来",
}
# 日志最多保留条数
_MAX_LOG = 200
# 自动定制简历的生成并发：大模型调用慢，最多同时 2 份
_TAILOR_SEM = asyncio.Semaphore(2)
# AI 分析并发数
_ANALYZE_CONCURRENCY = 4
# 批量查企业的并发数：每家要开浏览器读工商信息 + 搜索 + 大模型，限流防打爆
_COMPANY_CONCURRENCY = 2


class ApiError(Exception):
    """业务校验失败：路由层转成 HTTP 400，前端弹 toast。"""


class EventBus:
    """向所有已连接的 WebSocket 客户端广播事件。"""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)


class Runtime:
    """一次应用生命周期的共享运行时：内核单例、界面快照与后台流程。"""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.session = BossSession()
        self.scraper = JobScraper(self.session)
        self.prefetcher = CompanyPrefetcher(self._make_company_checker)
        self.responder = ChatResponder(self.session, self.prefetcher)
        self.apply_assistant = ApplyAssistant(self.session)
        self.scraper.company_prefetcher = self.prefetcher
        self.scraper.auto_resume_hook = self._auto_resume_for
        # 界面快照（与原工作台字段一一对应）
        self.boss_state = "空闲"
        self.busy = False
        self.reply_busy = False
        self.reply_state = "未开启"
        self.reply_count = 0
        self.recv_count = 0
        self.reply_skip_count = 0
        self.browser_background = False
        self.stored_count = 0
        self.session_count = 0
        self.dup_count = 0
        self.skip_count = 0
        self.target_cities = ""
        # 运行日志（新的在前）
        self.logs: deque[dict] = deque(maxlen=_MAX_LOG)
        # 分析进度：{analyzing, analyzed, total}
        self.analyze_state: dict = {"analyzing": False, "analyzed": 0, "total": 0}
        # 批量查企业进度：{running, done, total, name}
        self.company_state: dict = {"running": False, "done": 0, "total": 0, "name": ""}

    # --- 基础设施 ---

    def _make_company_checker(self) -> CompanyChecker:
        llm = LlmSettings.load()
        return CompanyChecker(
            CompanyPage(self.session),
            CompanySearch(),
            CompanyReviewer(llm=llm) if llm.ready else None,
        )

    def push_log(self, text: str, level: str = "info") -> None:
        """追加一条运行日志并广播。"""
        now = datetime.now().astimezone().strftime("%H:%M:%S")
        self.logs.appendleft({"time": now, "level": level, "text": text})
        self.bus.publish({"type": "log", "level": level, "text": text, "time": now})

    def publish_status(self) -> None:
        self.bus.publish({"type": "status", **self.snapshot()})

    def clear_logs(self) -> None:
        """清空运行日志并广播，让所有已连接页面同步清空。"""
        self.logs.clear()
        self.bus.publish({"type": "logs_cleared"})

    def snapshot(self) -> dict:
        return {
            "boss_state": self.boss_state,
            "busy": self.busy,
            "reply_busy": self.reply_busy,
            "reply_state": self.reply_state,
            "reply_count": self.reply_count,
            "recv_count": self.recv_count,
            "reply_skip_count": self.reply_skip_count,
            "browser_background": self.browser_background,
            "stored_count": self.stored_count,
            "session_count": self.session_count,
            "dup_count": self.dup_count,
            "skip_count": self.skip_count,
            "target_cities": self.target_cities,
            "analyze": dict(self.analyze_state),
            "company": dict(self.company_state),
        }

    def refresh_stored_count(self) -> None:
        self.stored_count = JobRow.count_applied()

    # --- 投递 ---

    def begin_apply(self) -> None:
        """校验配置并启动投递后台任务；校验失败抛 ``ApiError``，不打日志不置状态。"""
        if self.busy:
            return
        config = SearchConfigRow.load()
        queries = [k.strip() for k in (config.get("keywords") or []) if k and k.strip()]
        if not queries:
            primary = str(config.get("query") or "").strip()
            queries = [primary] if primary else []
        if not queries:
            raise ApiError("请先在配置中心「AI 生成」或手动填写岗位关键词")
        pace = PaceProfile.from_config(config)
        keywords = KeywordFilter.from_config(config)
        reviewer = JobReviewer.from_config(config, LlmSettings.load())
        low, high = float(config.get("salary_min") or 0), float(
            config.get("salary_max") or 0
        )
        salary_range = SalaryRange.monthly(low, high) if low > 0 or high > 0 else None
        resume = str(config.get("resume_text") or "").strip()
        if config.get("resume_match") and not resume:
            raise ApiError("已开启简历技术匹配，请先在「简历配置」上传简历")
        if reviewer and not reviewer.llm.ready:
            raise ApiError("已开启 AI 筛选，请先在配置中心开通「AI 服务」")
        cities = [c for c in (config.get("cities") or []) if c] or list(Defaults.CITIES)
        targets = self.scraper.build_targets(config)
        if not targets:
            raise ApiError("所选城市不可用，请检查目标城市设置")

        self.busy = True
        self.boss_state = "准备中"
        self.session_count = 0
        self.dup_count = 0
        self.skip_count = 0
        self.target_cities = "、".join(cities)
        self.refresh_stored_count()
        self.push_log(f"开始自动投递：{'、'.join(queries)} · {self.target_cities}")
        self.push_log(f"投递节奏：{pace.describe()}")
        if not self.session.is_open:
            self.push_log("正在打开浏览器…")
        if reviewer:
            self.push_log(f"已开启：{'、'.join(reviewer.checks)}")
        self.publish_status()
        asyncio.create_task(
            self._run_apply(config, queries, pace, keywords, reviewer, salary_range, targets)
        )

    async def _run_apply(
        self,
        config: dict,
        queries: list[str],
        pace: PaceProfile,
        keywords: KeywordFilter,
        reviewer,
        salary_range,
        targets: list[tuple[str, str]],
    ) -> None:
        async def on_job(_job) -> None:
            self.session_count += 1
            self.refresh_stored_count()
            self.publish_status()

        async def on_log(level: str, text: str) -> None:
            if level == "dup":
                self.dup_count += 1
            elif level in ("skip", "manual"):
                self.skip_count += 1
            self.push_log(text, level)

        try:
            _jobs, state = await self.scraper.search(
                targets,
                pace=pace,
                keywords=keywords,
                reviewer=reviewer,
                salary_range=salary_range,
                queries=queries,
                on_job=on_job,
                on_log=on_log,
            )
            self.refresh_stored_count()
            self.boss_state = _STATUS.get(state, "已完成")
            self.push_log(
                f"{_FINISHED.get(state, state)}：本次投递 {self.session_count} 条，"
                f"重复 {self.dup_count} 条，跳过 {self.skip_count} 条，"
                f"累计投递 {self.stored_count} 条",
                "warn" if state in ("need_login", "limit") else "info",
            )
            self.busy = False
            self.publish_status()
            await self.auto_score(config)
        except (RuntimeError, PlaywrightError, SQLAlchemyError) as exc:
            self.boss_state = "出错"
            self.push_log(str(exc), "warn")
            self.busy = False
            self.publish_status()
        except Exception as exc:  # noqa: BLE001 兜底：漏网的异常不能把 busy 卡死
            self.boss_state = "出错"
            self.push_log(f"意外错误：{log_exception('boss', exc)}", "warn")
            self.busy = False
            self.publish_status()

    async def auto_score(self, config: dict) -> None:
        """投递结束后自动评分：把没来得及评分的已投递岗位补一遍 AI 五维评分。"""
        llm = LlmSettings.load()
        if not llm.ready:
            return
        reviewer = JobReviewer.from_config(config, llm)
        uids = JobRow.unscored_uids()
        if not uids:
            return
        self.push_log(f"自动评分开始：{len(uids)} 个岗位待评分")
        semaphore = asyncio.Semaphore(4)

        async def score(uid: str) -> bool:
            row = JobRow.get_dict(uid)
            if row is None:
                return False
            if not str(row.get("description") or "").strip():
                return False
            job = Job.model_validate(
                {k: row[k] for k in ("title", "company", "salary", "description")}
            )
            try:
                async with semaphore:
                    verdict = await reviewer.check(job)
            except AgentRunError:
                return False
            return JobRow.set_verdict(
                uid,
                suitable=verdict.match,
                reason=verdict.reason,
                score=verdict.score,
                detail=verdict.detail,
            )

        results = await asyncio.gather(*(score(uid) for uid in uids))
        ok = sum(results)
        self.push_log(f"自动评分完成：成功 {ok} 个，失败 {len(uids) - ok} 个")

    # --- 自动回复 ---

    def begin_reply(self) -> None:
        """校验并启动自动回复后台任务；校验失败抛 ``ApiError``。"""
        if self.reply_busy:
            return
        settings = AutoReplySettings.load()
        llm = LlmSettings.load()
        config = SearchConfigRow.load()
        resume = str(config.get("resume_text") or "").strip()
        if not settings.enabled:
            raise ApiError("请先在「求职配置 → 自动回复」开启自动回复")
        if not llm.ready:
            raise ApiError("请先在配置中心开通「AI 服务」")
        if not resume:
            raise ApiError("请先在「简历配置」上传简历")
        pace = settings.pace_profile
        replier = ChatReplier(prompt=settings.prompt, resume=resume, llm=llm)
        self.reply_busy = True
        self.reply_state = "回复中"
        self.reply_count = 0
        self.recv_count = 0
        self.reply_skip_count = 0
        self.push_log(f"开始自动回复：{pace.describe()}")
        if settings.confirm_before_send:
            self.push_log("已开启发送前确认：AI 的回复会先进入「消息」页待你确认")
        if not self.session.is_open:
            self.push_log("正在打开浏览器…")
        self.publish_status()
        asyncio.create_task(
            self._run_reply(
                replier,
                pace=pace,
                keywords=KeywordFilter.from_config(config),
                reviewer=JobReviewer.from_config(config, llm),
                confirm=settings.confirm_before_send,
                block_proactive_hr=settings.block_proactive_hr,
            )
        )

    async def _run_reply(self, replier: ChatReplier, **kwargs) -> None:
        async def on_log(level: str, text: str) -> None:
            if level == "reply" and text.startswith("回复 "):
                self.reply_count += 1
            elif level == "recv":
                self.recv_count += 1
            elif level == "skip" and "新岗位" not in text:
                self.reply_skip_count += 1
            self.push_log(text, level)
            self.publish_status()

        try:
            state = await self.responder.run(replier, on_log=on_log, **kwargs)
            if state == "need_login":
                self.push_log("需要登录 BOSS，请在浏览器里登录后重新开始", "warn")
            self.push_log(f"自动回复已停止：本次回复 {self.reply_count} 条")
            self.reply_state = "需要登录" if state == "need_login" else "未开启"
            self.reply_busy = False
            self.publish_status()
        except (RuntimeError, PlaywrightError, SQLAlchemyError) as exc:
            self.reply_state = "出错"
            self.push_log(str(exc), "warn")
            self.reply_busy = False
            self.publish_status()
        except Exception as exc:  # noqa: BLE001 兜底：漏网的异常不能把 reply_busy 卡死
            self.reply_state = "出错"
            self.push_log(f"意外错误：{log_exception('reply', exc)}", "warn")
            self.reply_busy = False
            self.publish_status()

    # --- 浏览器 ---

    async def close_browser(self) -> None:
        self.scraper.request_stop()
        self.responder.request_stop()
        await self.session.close(keep_closed=True)
        self.boss_state = "空闲"
        self.busy = False
        self.reply_state = "未开启"
        self.reply_busy = False
        self.push_log("浏览器已关闭；再点「查看浏览器」或重新开始任务即可打开")
        self.publish_status()

    async def show_browser(self) -> None:
        self.session.resume()
        try:
            if not self.session.is_open:
                self.push_log("正在打开浏览器…")
                await self.session.open()
            await self.session.set_background(False)
            self.push_log("浏览器窗口已调到屏幕前；想继续后台运行就打开「后台静默运行」开关")
        except (RuntimeError, PlaywrightError) as exc:
            self.push_log(f"打开浏览器失败：{exc}", "warn")
            raise ApiError(f"打开浏览器失败：{exc}") from exc
        self.publish_status()

    async def toggle_background(self, value: bool) -> None:
        BrowserSettings(background=value).save()
        self.browser_background = value
        if self.session.is_open:
            await self.session.set_background(value)
            self.push_log(
                "浏览器窗口已移出屏幕，继续后台运行；想看时再切一次这个开关"
                if value
                else "浏览器窗口已调回屏幕，查看完可再打开开关移回后台",
            )
        else:
            self.push_log(
                "已开启后台静默模式：浏览器窗口移出屏幕"
                if value
                else "已关闭后台静默模式：想看浏览器时用「查看浏览器」调出",
            )
        self.publish_status()

    # --- 高分岗位自动定制简历（投递后台钩子） ---

    async def _auto_resume_for(self, job, verdict, log) -> None:
        settings = TailorSettings.load()
        if not settings.enabled or verdict.score is None or verdict.score < settings.min_score:
            return
        if not verdict.match:
            return
        config = SearchConfigRow.load()
        resume = str(config.get("resume_text") or "").strip()
        llm = LlmSettings.load()
        if not resume or not llm.ready:
            return
        uid = JobRow.uid_for(job)
        if load_saved_resume(job.platform, uid) is not None:
            return
        async with _TAILOR_SEM:
            if load_saved_resume(job.platform, uid) is not None:
                return
            try:
                result = await ResumeTailor(
                    resume=resume,
                    title=job.title,
                    company=job.company,
                    salary=job.salary,
                    description=job.description,
                    requirement=str(config.get("ai_requirement") or ""),
                    llm=llm,
                ).generate()
            except Exception as exc:  # noqa: BLE001 后台任务失败只记日志
                await log(f"自动定制简历失败（{job.title}）：{log_exception('tailor', exc)}", "warn")
                return
        save_custom_resume(job.platform, uid, result.content)
        if result.issues:
            await log(
                f"已自动生成定制简历（{job.title}），校验未全过：{'；'.join(result.issues[:2])}",
                "warn",
            )
        else:
            await log(f"已自动生成定制简历（{job.title}），在岗位详情里查看", "info")

    # --- AI 分析选中岗位 ---

    def analyze_jobs(self, uids: list[str]) -> dict:
        """后台批量 AI 分析，进度走事件总线；返回初始进度快照。"""
        if self.analyze_state["analyzing"] or not uids:
            raise ApiError("没有可分析的岗位或分析正在进行")
        llm = LlmSettings.load()
        if not llm.ready:
            raise ApiError("请先在配置中心开通「AI 服务」")
        reviewer = JobReviewer.from_config(SearchConfigRow.load(), llm)
        if reviewer is None:
            raise ApiError("请先在配置中心开启「简历技术匹配」或「骗子岗位识别」")
        self.analyze_state = {"analyzing": True, "analyzed": 0, "total": len(uids)}
        self.publish_status()
        asyncio.create_task(self._analyze_task(uids, reviewer))
        return dict(self.analyze_state)

    async def _analyze_task(self, uids: list[str], reviewer) -> None:
        limit = asyncio.Semaphore(_ANALYZE_CONCURRENCY)

        async def analyze(uid: str) -> bool:
            row = JobRow.get_dict(uid)
            if row is None:
                return False
            job = Job.model_validate(
                {k: row[k] for k in ("title", "company", "salary", "description")}
            )
            try:
                async with limit:
                    verdict = await reviewer.check(job)
            except AgentRunError:
                return False
            self.analyze_state["analyzed"] += 1
            self.publish_status()
            return JobRow.set_verdict(
                uid,
                suitable=verdict.match,
                reason=verdict.reason,
                score=verdict.score,
                detail=verdict.detail,
            )

        results = await asyncio.gather(*(analyze(uid) for uid in uids))
        done = sum(results)
        self.analyze_state = {"analyzing": False, "analyzed": len(uids), "total": len(uids)}
        if done == len(uids):
            self.push_log(f"已完成 {done} 个岗位的 AI 分析")
        else:
            self.push_log(f"分析完成 {done} 个，失败 {len(uids) - done} 个", "warn")
        self.bus.publish({"type": "analyze_done", "ok": done, "failed": len(uids) - done})
        self.publish_status()

    # --- 查企业 ---

    async def company_check(self, job_uid: str) -> dict:
        """查企业：公司主页 → 网上搜索 → AI 评估；步骤走事件总线，返回报告。"""
        job = JobRow.get_dict(job_uid)
        if job is None:
            raise ApiError("岗位不存在或已删除")
        checker = self._make_company_checker()

        async def on_step(text: str) -> None:
            self.bus.publish({"type": "company_step", "job_uid": job_uid, "step": text})

        await on_step("正在打开浏览器…")
        try:
            brand_id = await checker.check(job_uid, on_step)
        except (RuntimeError, PlaywrightError, AgentRunError, SQLAlchemyError) as exc:
            raise ApiError(str(exc) or "查询失败，请稍后重试") from exc
        except Exception as exc:  # noqa: BLE001 兜底给出可读提示
            raise ApiError(f"意外错误：{log_exception('company', exc)}") from exc
        report = CompanyRow.get_dict(brand_id) or {}
        self.bus.publish(
            {"type": "company_done", "job_uid": job_uid, "report": report}
        )
        return report

    def company_scan(self, uids: list[str]) -> dict:
        """批量查企业：把所选岗位去重成公司，后台逐家查询；返回初始进度快照。

        只查传进来的岗位，不再扫全库补历史数据：新岗位在入库时已由
        ``CompanyPrefetcher`` 自动查过。7 天内查过的公司跳过。
        """
        if self.company_state["running"]:
            raise ApiError("企业查询正在进行，请等它跑完")
        if not uids:
            raise ApiError("请先选择要查询的岗位")
        targets = self._company_targets(uids)
        if not targets:
            raise ApiError("这些岗位的公司都查过了，无需重复查询")
        self.company_state = {"running": True, "done": 0, "total": len(targets), "name": ""}
        self.push_log(f"开始批量查企业：共 {len(targets)} 家公司")
        self.publish_status()
        asyncio.create_task(self._company_scan_task(targets))
        return dict(self.company_state)

    @classmethod
    def _company_targets(cls, uids: list[str]) -> list[dict[str, str]]:
        """岗位 → 去重后的公司清单；7 天内查过的公司跳过。"""
        sources: list[dict[str, str]] = []
        for uid in uids:
            job = JobRow.get_dict(uid)
            if job is None:
                continue
            sources.append(
                {
                    "platform": str(job.get("platform") or "boss"),
                    "brand_id": str(job.get("brandId") or ""),
                    "name": str(job.get("company") or "").strip(),
                    "link": str(job.get("link") or ""),
                }
            )
        seen: dict[str, dict[str, str]] = {}
        for source in sources:
            name, brand_id = source["name"], source["brand_id"]
            key = brand_id or (f"name:{name}" if name else "")
            if not key or key in seen:
                continue
            seen[key] = source
        fresh = CompanyRow.fresh(seen, days=CompanyPrefetcher.FRESH_DAYS)
        return [t for key, t in seen.items() if key not in fresh]

    async def _company_scan_task(self, targets: list[dict[str, str]]) -> None:
        checker = self._make_company_checker()
        limit = asyncio.Semaphore(_COMPANY_CONCURRENCY)

        async def scan(target: dict[str, str]) -> None:
            name = target["name"] or target["brand_id"]
            async with limit:
                try:
                    await checker.check_company(
                        platform=target["platform"],
                        brand_id=target["brand_id"],
                        name=target["name"],
                        link=target["link"],
                    )
                except (RuntimeError, PlaywrightError, AgentRunError, SQLAlchemyError) as exc:
                    self.push_log(f"查企业失败（{name}）：{exc}", "warn")
                except Exception as exc:  # noqa: BLE001 后台任务，异常只记日志
                    self.push_log(f"查企业意外失败（{name}）：{log_exception('company', exc)}", "warn")
                finally:
                    self.company_state["done"] += 1
                    self.company_state["name"] = name
                    self.publish_status()

        try:
            await asyncio.gather(*(scan(t) for t in targets))
        finally:
            done, total = self.company_state["done"], self.company_state["total"]
            self.company_state["running"] = False
            self.company_state["name"] = ""
            self.push_log(f"批量查企业完成：{done}/{total} 家")
            self.bus.publish({"type": "company_batch_done", "done": done, "total": total})
            self.publish_status()

    # --- 打开外部链接 ---

    def open_link(self, url: str) -> None:
        if url:
            webbrowser.open(url)


# 单例：整个应用共享一个运行时
runtime = Runtime()
