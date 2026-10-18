"""网申助手内核：在任意企业官网的表单里，把简历上的信息填进去。

与 ``job/boss`` 的区别在于目标站点不固定：BOSS 的选择器可以写死，
企业官网的表单千变万化，只能「先扫、再匹配、最后填」——

* :mod:`job.apply.profile`  —— 把网申资料 + 简历整理成一份可检索的键值集；
* :mod:`job.apply.scanner`  —— 扫描页面（含 iframe）里的所有表单控件；
* :mod:`job.apply.matcher`  —— 规则优先、AI 兜底，把资料对应到表单字段；
* :mod:`job.apply.filler`   —— 按控件类型填值（含上传简历附件）；
* :mod:`job.apply.safety`   —— 字段分类、数据最小化、重复模块倒序等安全规则；
* :mod:`job.apply.vision`   —— 把扫出来的栏位画在截图上，请多模态模型核对多没多少没少；
* :mod:`job.apply.assistant`—— 串起「打开 → 扫描 → 填表 → 等确认 → 提交」。

刻意不做的一件事：不自动提交。企业官网的表单提交后撤不回来，还有验证码、
登录态等只有人能处理的门槛，所以提交这一步永远由用户点。
"""

from __future__ import annotations

from job.apply import safety
from job.apply.assistant import ApplyAssistant, ApplyError
from job.apply.filler import FillOutcome, FormFiller
from job.apply.matcher import FieldMapping, FieldMatcher
from job.apply.profile import ApplyProfile, ProfileEntry, resume_facts
from job.apply.scanner import FormField, FormScanner
from job.apply.vision import VisionChecker, VisionResult

__all__ = [
    "ApplyAssistant",
    "ApplyError",
    "ApplyProfile",
    "FieldMapping",
    "FieldMatcher",
    "FillOutcome",
    "FormField",
    "FormFiller",
    "FormScanner",
    "ProfileEntry",
    "resume_facts",
    "safety",
    "VisionChecker",
    "VisionResult",
]
