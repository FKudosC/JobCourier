"""字段匹配：把「表单上的字段」对应到「我有哪些资料」。

三档优先级，越靠前越可信：

1. **历史映射** —— 同一个官网的表单上次填过，直接沿用（confidence 0.95）；
2. **关键词规则** —— 字段文字命中中英关键词表（confidence 0.9）；
3. **AI 兜底** —— 规则没命中的字段交给人模型判断，未开通 AI 服务就静默跳过。

一条硬规矩：只认「资料里真有值」的键，绝不凭空编造。猜不准的字段留空，
由用户在字段对照表里手动补——填错比留空麻烦得多。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import ClassVar

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from job.apply import knowledge, safety
from job.apply.profile import ApplyProfile
from job.apply.scanner import FormField, normalize
from job.models.application import ApplyMappingRow, ApplyRejectRow
from job.models.setting import ApplyProfileSettings, LlmSettings
from job.utils import exception_text, log, telemetry
from job.utils.llm import MAX_OUTPUT_TOKENS, OUTPUT_RETRIES, extract_json

# 字段键 → 关键词；命中任意一个即算对应上。英文关键词统一小写（比对前会归一化）。
# 顺序只影响「同长度关键词」的优先级，正常命中靠的是「关键词更长者优先」。
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 基本信息
    ("name", ("姓名", "名字", "真实姓名", "fullname", "yourname", "name")),
    ("name_pinyin", ("姓名拼音", "拼音", "pinyin", "namepinyin")),
    ("english_name", ("英文名", "英文姓名", "englishname")),
    ("former_name", ("曾用名", "原名", "formername")),
    ("gender", ("性别", "gender", "sex")),
    ("birth", ("出生年月", "出生日期", "出生时间", "生日", "dateofbirth", "birthday", "birth", "dob")),
    ("birth_place", ("出生地", "出生地点", "birthplace")),
    ("nation", ("民族", "ethnicity", "ethnic")),
    ("nationality", ("国籍", "nationality")),
    ("political", ("政治面貌", "党派", "politicalstatus", "political")),
    ("party_time", ("入党时间", "入团时间", "入党年月", "partytime")),
    ("marital", ("婚姻状况", "婚姻状态", "婚否", "maritalstatus", "marriage")),
    ("id_type", ("证件类型", "证件种类", "idtype")),
    ("id_card", ("身份证号码", "身份证号", "身份证", "证件号码", "idcard", "idnumber")),
    ("id_expiry", ("身份证有效期", "证件有效期", "idexpiry")),
    ("hukou", ("户籍所在地", "户籍地址", "户口所在地", "户口地址", "户籍", "户口", "hukou", "household")),
    ("hukou_type", ("户口性质", "户籍性质", "户口类型", "hukoutype")),
    ("native_place", ("籍贯", "祖籍", "nativeplace")),
    ("origin_place", ("生源地", "生源", "originplace")),
    ("height", ("身高", "height")),
    ("weight", ("体重", "weight")),
    ("blood_type", ("血型", "bloodtype")),
    ("health", ("健康状况", "身体状况", "健康", "health")),
    ("religion", ("宗教信仰", "宗教", "religion")),
    # 联系方式
    ("phone", ("手机号码", "手机号", "联系电话", "联系方式", "手机", "电话", "mobile", "phone", "tel")),
    ("email", ("电子邮箱", "电子邮件", "邮箱", "email", "mail")),
    ("wechat", ("微信号", "微信", "wechat", "weixin")),
    ("qq", ("qq号", "qq")),
    ("city", ("现居城市", "所在城市", "居住城市", "当前城市", "现居地", "城市", "city")),
    ("address", ("通讯地址", "联系地址", "详细地址", "家庭住址", "现住址", "住址", "地址", "address")),
    ("postal", ("邮政编码", "邮编", "postalcode", "zipcode", "zip")),
    ("current_location", ("目前所在地", "当前所在地", "现在所在地", "currentlocation")),
    # 教育背景
    ("school", ("毕业学校", "毕业院校", "就读学校", "就读院校", "学校", "院校", "母校", "school", "university", "college")),
    ("school_type", ("学校类型", "院校类型", "办学层次", "schooltype")),
    ("college_name", ("学院", "院系", "faculty")),
    ("major", ("所学专业", "专业名称", "专业", "major", "specialty")),
    ("major_category", ("专业类别", "专业大类", "专业类", "majorcategory", "majorclass")),
    ("discipline", ("学科门类", "学科类别", "学位授予门类", "discipline")),
    ("major_direction", ("专业方向", "研究方向", "majorfield")),
    # 「学位名称」问的是带学科门类的全称（工学学士学位），排在「学位」前面先认领；
    # 两栏都命中时按关键词更长的取胜，这里靠更长的关键词把这一栏抢过来。
    ("degree_full", ("学位名称", "学位全称", "所获学位名称", "授予学位名称", "degreefull")),
    ("degree_name", ("所获学位", "授予学位", "学位", "degreename")),
    ("degree", ("最高学历", "学历", "文化程度", "degree", "education")),
    ("study_mode", ("学习形式", "学习方式", "就读形式", "studymode")),
    ("train_mode", ("培养方式", "培养类型", "trainmode")),
    ("unified_enrollment", ("是否统招", "统招", "unifiedenrollment")),
    ("graduate_year", ("毕业年份", "毕业时间", "毕业年月", "graduationyear", "graduation")),
    ("start_year", ("入学时间", "入学年月", "enrollmentdate")),
    ("schooling_length", ("学制", "修业年限", "schoolinglength")),
    ("gpa", ("平均绩点", "学业成绩", "平均分", "绩点", "gradepoint", "gpa")),
    ("rank", ("专业排名", "年级排名", "排名", "rank")),
    ("cet", ("英语四六级", "英语等级", "英语水平", "cet6", "cet4", "四级", "六级", "cet")),
    ("mandarin", ("普通话等级", "普通话", "mandarin")),
    ("computer_level", ("计算机等级", "计算机水平", "computerlevel")),
    ("second_major", ("辅修专业", "第二专业", "双学位", "辅修", "secondmajor", "minor")),
    ("courses", ("主修课程", "主要课程", "courses")),
    ("thesis", ("毕业论文", "毕业设计", "论文题目", "thesis")),
    ("advisor", ("指导教师", "导师", "advisor", "supervisor")),
    ("awards", ("获奖情况", "获奖经历", "获奖", "奖项", "荣誉", "awards", "honors")),
    ("scholarship", ("奖学金", "scholarship")),
    ("grant", ("国家助学金", "助学金", "grant")),
    ("school_position", ("在校职务", "学生干部", "班干部", "schoolposition")),
    ("student_union", ("学生会", "团委", "studentunion")),
    ("student_id", ("学号", "studentid")),
    ("class_name", ("班级", "classname")),
    # 求职意向
    ("expected_salary", ("期望薪资", "期望月薪", "期望工资", "薪资要求", "expectedsalary", "salaryexpectation")),
    ("available", ("到岗时间", "可到岗", "入职时间", "最快到岗", "available", "onboard")),
    (
        "expected_city",
        (
            "期望城市",
            "意向城市",
            "期望工作地",
            "期待工作地点",
            "期望工作地点",
            "意向工作地点",
            "期望工作城市",
            "意向工作城市",
            "工作地点",
            "expectedcity",
        ),
    ),
    ("expected_position", ("期望职位", "意向岗位", "应聘岗位", "目标岗位", "expectedposition")),
    ("expected_industry", ("期望行业", "意向行业", "expectedindustry")),
    ("job_type", ("求职类型", "工作性质", "jobtype")),
    ("current_status", ("求职状态", "目前状态", "在职状态", "currentstatus")),
    ("accept_travel", ("接受出差", "是否出差", "出差", "businesstravel")),
    ("accept_relocate", ("接受外派", "是否接受外派", "服从调剂", "调剂", "relocate")),
    ("internship_duration", ("实习时长", "可实习时间", "internshipduration")),
    ("internship_days", ("每周实习", "实习天数", "internshipdays")),
    # 经历与能力
    ("work_years", ("工作年限", "从业年限", "yearsofexperience", "workyears")),
    ("current_company", ("目前公司", "现任公司", "当前单位", "currentcompany")),
    ("current_position", ("目前职位", "现任职位", "当前岗位", "currentposition")),
    ("leave_reason", ("离职原因", "辞职原因", "leavingreason")),
    ("work_experience", ("工作经历", "工作履历", "workexperience")),
    ("internship", ("实习经历", "实习经验", "internshipexperience")),
    ("projects", ("项目经历", "项目经验", "projects")),
    ("research", ("科研经历", "科研项目", "科研", "research")),
    ("competition", ("竞赛经历", "参赛经历", "竞赛获奖", "竞赛", "competition")),
    ("volunteer", ("志愿服务", "志愿者经历", "volunteer")),
    ("social_practice", ("社会实践", "实践经历", "socialpractice")),
    ("part_time", ("兼职经历", "兼职经验", "parttime")),
    ("exchange", ("交换经历", "海外经历", "留学经历", "exchange")),
    ("club", ("社团经历", "社团活动", "校园经历", "clubs")),
    ("training", ("培训经历", "培训经验", "training")),
    ("skills", ("专业技能", "技能特长", "skills")),
    ("strengths", ("个人优势", "个人特长", "优势", "特长", "strengths")),
    ("language_skill", ("语言能力", "外语能力", "languageskill")),
    ("certificates", ("资格证书", "职业资格", "证书", "certificates")),
    ("driver_license", ("驾驶证", "驾照", "driverlicense")),
    ("publications", ("论文发表", "发表论文", "科研成果", "publications")),
    ("patent", ("发明专利", "专利", "patent")),
    ("software_copyright", ("软件著作权", "软著", "softwarecopyright")),
    # 紧急联系人
    ("emergency_name", ("紧急联系人姓名", "紧急联系人", "联系人姓名", "emergencycontact", "emergencyname")),
    ("emergency_relation", ("与本人关系", "紧急联系人关系", "relationship", "relation")),
    ("emergency_phone", ("紧急联系人电话", "紧急联系电话", "紧急联系人手机", "emergencyphone")),
    ("family_member", ("家庭成员", "家庭情况", "家属", "family")),
    # 其他信息
    ("referral_code", ("内推码", "推荐码", "内推", "referralcode", "referral")),
    ("source_channel", ("信息来源", "招聘渠道", "获知渠道", "sourcechannel")),
    ("is_fresh_graduate", ("应届毕业生", "毕业生类型", "毕业生类别", "应届生类别", "应届类别", "是否应届", "应届生", "留学归国", "应届", "freshgraduate")),
    ("relative_in_company", ("亲属任职", "是否有亲属", "亲属在本公司", "relativeincompany")),
    ("noncompete", ("竞业限制", "竞业协议", "noncompete")),
    ("archive_location", ("档案所在地", "档案存放", "档案", "archive")),
    ("hobby", ("兴趣爱好", "爱好", "兴趣", "hobbies", "interests")),
    ("self_intro", ("自我介绍", "自我评价", "个人简介", "个人介绍", "自我描述", "selfintroduction", "introduction", "aboutme")),
    ("career_plan", ("职业规划", "职业目标", "careerplan")),
    ("github", ("github", "gitee", "码云", "代码仓库", "开源项目")),
    ("portfolio", ("个人主页", "个人网站", "作品集", "博客", "portfolio", "website", "homepage", "blog")),
    ("linkedin", ("领英", "linkedin")),
    ("dingtalk", ("钉钉", "dingtalk")),
)

# 通用词（姓名 / 电话 / 邮箱 / 地址 / 城市）容易被「紧急联系人姓名」这类专属栏借走：
# 标签里带着专属栏的标记词时直接放弃，宁可留空交给 AI 或手动，也别把本人信息填进
# 家人的栏里。专属栏自己有值时会命中更长的关键词，轮不到这里兜底。
_VETO: dict[str, tuple[str, ...]] = {
    "name": (
        "联系人",
        "emergency",
        "guardian",
        "家长",
        "父亲",
        "母亲",
        "监护人",
        "推荐人",
        "导师",
        "配偶",
        # 银行类控件常把 name 写成 bankName / bankCard：别让「…name」把本人姓名骗进去
        "bank",
        "银行",
        "卡号",
    ),
    "phone": ("紧急", "联系人", "emergency", "guardian", "家长", "父亲", "母亲", "监护人", "推荐人", "配偶"),
    "email": ("紧急", "联系人", "emergency", "推荐人"),
    "address": ("紧急", "户籍", "户口"),
    "city": ("紧急", "生源", "户籍"),
}

# 交给模型的控件类型：整张表都过一遍模型，它比关键词表更认得出「这一栏到底问什么」。
# ``div`` 是组件库不给每个选项配 input 时整组单选 / 多选的壳，只在这类组上才交给模型。
_AI_TYPES = frozenset(
    {
        "text",
        "textarea",
        "email",
        "tel",
        "url",
        "search",
        "number",
        "select-one",
        "select",
        "radio",
        "checkbox",
        "div",
    }
)


def askable(field: FormField) -> bool:
    """这一栏该不该单独问一次模型（逐栏推理与整表规划的入口判据）。

    文件栏、隐藏栏、提交按钮都不问：它们的取值不来自「问一句话」——文件要的是本机
    真有一份文件，提交按钮永远留给人点。组件库的 Select / Cascader 渲染成一个带
    ``role`` 的 ``div``：有选项的才当一栏问，纯版式容器（区块标题、说明文字）不问。
    """
    if field.hidden or field.is_file:
        return False
    if field.type not in _AI_TYPES:
        return False
    return field.type != "div" or bool(field.options)


# 本机已经能按确定规则推出来的那几栏（学历 ↔ 学位、专业 → 专业类 / 学科门类、
# 毕业年份 → 应届身份、学科门类 + 学位 → 学位名称）。这几栏模型再推一遍时可信度
# 给高一档；白名单本身不再是闸门——只要模型说得出依据，别的栏也允许推，只是要
# 单列一区请用户核对，别当成资料原文。
_DERIVABLE = frozenset(
    {"degree", "degree_name", "degree_full", "major_category", "discipline", "is_fresh_graduate"}
)

# 文件上传栏：认领的是「本机有没有这份文件」，不是资料里的一行字，所以单独一张表。
# 顺序有讲究——先认「生活照 / 证件照」，再认「简历 / 附件」，免得「简历照片」被当成简历。
_FILE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("photo", ("生活照", "证件照", "近照", "照片", "photo", "portrait", "avatar")),
    ("resume", ("简历", "履历", "附件", "resume", "curriculumvitae")),
)

# 文件栏认领到之后占位的「值」：真正要传的文件由填表环节按 key 去本机找。
# 显示名要和「简历配置」里那个上传按钮一致（本机只留一份寸照），免得对照表上
# 一边写「上传寸照」、一边又冒出「生活照」。
_FILE_VALUES: dict[str, str] = {"resume": "简历原件", "photo": "寸照"}

# 旧版本把「来源」写进了记住的那一列（资料 / 简历 / AI / 手动 / 沿用），
# 这类记录还原出来只会让下拉里冒出「资料」这种怪选项，直接作废重判。
_STALE_KEYS = frozenset({"资料", "简历", "AI", "手动", "沿用"})

# 下拉里的占位项：不是真选项，选中它等于没填。
_PLACEHOLDER_OPTION = re.compile(r"^(所属|请选择|请选取|全部|不限|请选择省份|请选择城市)")

# 资料里的说法和官网选项经常对不上：说的是同一件事，字面却不一样。这里按「意思」
# 分组，按原值找不到时，再拿同组的其它说法去试。只列真正同义的写法，不做推断。
_OPTION_EQUIVALENTS: tuple[tuple[str, ...], ...] = (
    ("随时到岗", "立即到岗", "马上到岗", "随时", "一周内", "一周以内", "7天内"),
    ("一个月内到岗", "一个月内", "一个月以内", "30天内"),
    ("三个月内到岗", "三个月内", "三个月以内"),
    ("三个月以上到岗", "三个月以上"),
    ("半年内到岗", "半年内"),
    ("面议", "薪资面议", "待遇面议"),
)

# 学历栏：资料里写「本科」，官网的选项常排成「大学 / 大学本科 / 大专」这类说法，
# 字面比不出来。按学位层次归到同一档，只在这栏的选项里确实排着这种写法时才用。
_DEGREE_OPTION_ALIASES: dict[str, tuple[str, ...]] = {
    "博士": ("博士研究生", "博士"),
    "硕士": ("硕士研究生", "硕士", "研究生"),
    "本科": ("大学本科", "本科", "大学", "全日制本科", "本科生"),
    "专科": ("大学专科", "专科", "大专", "高职高专", "高职", "专科生"),
    "高中": ("普通高中", "高中"),
}

# 薪资区间：官网的选项多是「8001～10000」这种数字区间，资料里常写「8-10K」。
# 必须整串都是区间才算数——否则「2024-10」这种日期也会被读成金额。
_SALARY_RANGE = re.compile(
    r"(\d+(?:\.\d+)?)\s*([kK千万]?)\s*[-~～—–至到]\s*(\d+(?:\.\d+)?)\s*([kK千万]?)"
)
_SALARY_BELOW = re.compile(r"(\d+(?:\.\d+)?)\s*([kK千万]?)\s*以下")
_SALARY_ABOVE = re.compile(r"(\d+(?:\.\d+)?)\s*([kK千万]?)\s*以上")
_SALARY_SCALE = {"k": 1000, "千": 1000, "万": 10000}

# 行政区划后缀：把「浙江省杭州市西湖区」拆成省 / 市 / 区三段。
_ADMIN_RE = re.compile(r".+?(?:特别行政区|自治区|自治州|自治县|自治旗|地区|省|市|县|区|旗)")


@dataclass
class FieldMapping:
    """一个字段的匹配结果。"""

    key: str = ""
    value: str = ""
    source: str = ""
    confidence: float = 0.0


def _salary_scale(unit: str) -> int:
    """金额单位换算：「K / 千」按千算，「万」按万算，没有单位就是原值。"""
    return _SALARY_SCALE.get((unit or "").strip().lower(), 1)


def salary_bounds(text: str) -> tuple[float, float] | None:
    """把「8-10K」「8001～10000」「1000以下」「15001以上」读成一个金额区间。

    整串读不出区间就返回 ``None``——宁可交给用户手动选，也不猜。
    """
    raw = re.sub(r"[\s,，元¥￥]", "", (text or "").strip())
    if not raw:
        return None
    if match := _SALARY_RANGE.fullmatch(raw):
        low_unit, high_unit = match.group(2), match.group(4)
        low = float(match.group(1)) * _salary_scale(low_unit or high_unit)
        high = float(match.group(3)) * _salary_scale(high_unit or low_unit)
        return (low, high) if low <= high else (high, low)
    if match := _SALARY_BELOW.fullmatch(raw):
        return (0.0, float(match.group(1)) * _salary_scale(match.group(2)))
    if match := _SALARY_ABOVE.fullmatch(raw):
        return (float(match.group(1)) * _salary_scale(match.group(2)), float("inf"))
    return None


def _align_text(value: str, options: list[str]) -> str:
    """按字面挑：完全相等 > 互相包含；挑不到返回空串。"""
    target = normalize(value)
    if not target:
        return ""
    best = ""
    best_score = 0
    for option in options:
        text = normalize(option)
        if not text:
            continue
        # 「所属省份 / 所属城市」这类是级联下拉的占位项，不是真的选项
        if _PLACEHOLDER_OPTION.match(text):
            continue
        if text == target:
            return option
        if target in text or text in target:
            score = min(len(text), len(target))
            if score > best_score:
                best, best_score = option, score
    return best


def _equivalents(value: str) -> list[str]:
    """和这个值同义的其它写法（同组里的都算）。"""
    target = normalize(value)
    if not target:
        return []
    for group in _OPTION_EQUIVALENTS:
        if any(normalize(item) == target for item in group):
            return [item for item in group if normalize(item) != target]
    return []


def _align_degree(value: str, options: list[str]) -> str:
    """学历栏：资料写「本科」、选项排「大学」时，按学位层次挑同一档的写法。

    先要完全相等——选项里同时排着「大学」和「大学专科」时，靠包含关系会挑错档；
    退一步按包含关系挑时，选项里写着别的学历层次的写法（专科 / 硕士……）就直接
    放弃，宁可留空让用户选。挑出来不是唯一一个也放弃。
    """
    level = knowledge.degree_level(value)
    if not level:
        return ""
    aliases = [normalize(alias) for alias in _DEGREE_OPTION_ALIASES.get(level, ())]
    if not aliases:
        return ""
    others = [
        normalize(alias)
        for other, group in _DEGREE_OPTION_ALIASES.items()
        if other != level
        for alias in group
    ]
    for option in options:
        if normalize(option) in aliases:
            return option
    hits: list[str] = []
    for option in options:
        text = normalize(option)
        if not text or _PLACEHOLDER_OPTION.match(text):
            continue
        if any(alias in text for alias in others):
            # 这一项是别的学历层次的写法（「大学专科」对「本科」），别抢
            continue
        if any(alias in text or text in alias for alias in aliases):
            hits.append(option)
    return hits[0] if len(hits) == 1 else ""


def _align_salary(value: str, options: list[str]) -> str:
    """按金额区间挑：资料「8-10K」对官网「8001～10000」，取重叠最多的那一档。

    只有选项里确实排着一串金额区间时才启用——否则一个「2024-06」这样的日期值
    也会被当成区间算进去。
    """
    want = salary_bounds(value)
    if want is None:
        return ""
    parsed = [(option, salary_bounds(option)) for option in options]
    ranges = [(option, bound) for option, bound in parsed if bound is not None]
    if len(ranges) < 2:
        return ""
    best = ""
    best_overlap = 0.0
    for option, bound in ranges:
        overlap = min(want[1], bound[1]) - max(want[0], bound[0])
        if overlap > best_overlap:
            best, best_overlap = option, overlap
    return best


def align_option(value: str, options: list[str]) -> str:
    """在表单选项里挑一个最贴近资料的。

    顺序：完全相等 / 互相包含 → 同义说法 → 学历层次 → 金额区间。都挑不到就返回
    空串，交给用户手动选——填错比留空麻烦得多。
    """
    for candidate in (value, *_equivalents(value)):
        hit = _align_text(candidate, options)
        if hit:
            return hit
    if hit := _align_degree(value, options):
        return hit
    return _align_salary(value, options)


def split_admin(value: str) -> list[str]:
    """按行政区后缀把地址拆成几段；拆不出来就原样返回一段。"""
    text = (value or "").strip()
    if not text:
        return []
    parts = _ADMIN_RE.findall(text)
    if not parts:
        return [text]
    rest = text[len("".join(parts)):]
    if rest:
        parts.append(rest)
    return parts


def parse_plan(data: object) -> dict[str, tuple[str, str, str, str]]:
    """把模型返回的填表计划整成 ``{字段序号: (资料键, 选项值, 依据, 共指键)}``。

    兼容两种写法：新版 ``{"key": ..., "value": ..., "basis": ..., "same_as": ...}``，
    以及旧版的纯键字符串（那时只会挑资料、不会挑选项，值留空由本机对齐）。

    ``basis`` 是模型自己写值时的推导依据；``same_as`` 是模型说的「这一栏其实就是
    库里哪一条」（叫法不同而已）。两者没有就是空串。认不出的条目直接丢掉。
    """
    if not isinstance(data, dict):
        return {}
    plan: dict[str, tuple[str, str, str, str]] = {}
    for raw_index, raw_value in data.items():
        if isinstance(raw_value, dict):
            key = str(raw_value.get("key") or "").strip()
            value = str(raw_value.get("value") or "").strip()
            basis = str(raw_value.get("basis") or "").strip()
            same_as = str(raw_value.get("same_as") or raw_value.get("sameAs") or "").strip()
        elif isinstance(raw_value, str):
            key, value, basis, same_as = raw_value.strip(), "", "", ""
        else:
            continue
        if key:
            plan[str(raw_index)] = (key, value, basis, same_as)
    return plan


def _plan_row(row: object) -> tuple[str, str, str, str]:
    """把计划里的一行摊平成四元组。

    旧写法只有三个字段（键 / 值 / 依据），共指键补空串；单测里直接塞三元组的
    也算数，免得每次动计划结构都得把所有桩函数一起改。
    """
    if isinstance(row, str):
        return (row, "", "", "")
    if not isinstance(row, (tuple, list)) or not row:
        return ("", "", "", "")
    parts = [str(part or "").strip() for part in row]
    parts += [""] * (4 - len(parts))
    return parts[0], parts[1], parts[2], parts[3]


def parse_drop_indexes(data: object) -> set[int]:
    """把模型回的「要剔掉的序号」整成 ``{序号}``；认不出的条目直接丢。

    模型被要求只回 ``{"drop": [3, 7]}``，但回 ``[3, 7]`` 或 ``{"3": true}`` 也照收，
    免得一种写法变了就得改提示词。序号取不到整数就跳过那一条。
    """
    raw: object = data
    if isinstance(data, dict):
        raw = data.get("drop")
        if raw is None:
            raw = [key for key, value in data.items() if value]
    if not isinstance(raw, (list, tuple, set)):
        return set()
    indexes: set[int] = set()
    for item in raw:
        try:
            indexes.add(int(item))
        except (TypeError, ValueError):
            continue
    return indexes


def parse_merge_keys(data: object) -> dict[int, str]:
    """把模型回的「这一条该叫哪个名」整成 ``{序号: 规范名}``。

    模型被要求回 ``{"merge": {"3": "专业类别"}}``：同一件事的不同叫法写成同一个
    名字，本机据此把它们并成库里的一条（这就是「AI 归纳」那一步）。认不出的
    条目直接丢；没给名字的条目按原键走。
    """
    if not isinstance(data, dict):
        return {}
    raw = data.get("merge")
    if not isinstance(raw, dict):
        return {}
    merged: dict[int, str] = {}
    for raw_pos, raw_name in raw.items():
        try:
            pos = int(raw_pos)
        except (TypeError, ValueError):
            continue
        name = str(raw_name or "").strip()
        if name:
            merged[pos] = name
    return merged


def _plan_option(want: str, value: str, options: list[str]) -> str:
    """把模型的选项建议落到页面上真实存在的选项；对不上就返回空串。

    先认模型给的那个词（它在提示词里看过完整选项表，最可能直接命中），
    再退回资料里的原值走同义 / 区间对齐。两边都对不上就留空，请用户手动选。
    """
    for candidate in (want, value):
        if not candidate:
            continue
        if candidate in options:
            return candidate
        hit = align_option(candidate, options)
        if hit:
            return hit
    return ""


class FieldMatcher:
    """规则优先、AI 兜底，把资料对应到表单字段。"""

    SYSTEM: ClassVar[str] = (
        "你在帮求职者把个人资料填进企业官网的网申表单。每家企业的表单叫法都不一样，"
        "你的任务就是逐栏判断「这一栏在问什么、该填什么」。\n"
        "我会给出「可用资料」（键 + 中文标签 + 取值）、「简历正文」（本人真实经历）、"
        "「资料库里还空着的字段」和「表单字段」（序号 + 栏位文字 + 控件类型 + 该栏可选项 "
        "+ 本机规则初判）。\n"
        "请为**每一个判断得了的字段**出一份填表计划，只输出一个 JSON 对象：键是字段序号"
        '的字符串，值形如 {"key": 资料键, "value": 要填进去的内容, "basis": 取值依据, '
        '"same_as": 共指键}。\n'
        "规则：\n"
        "1. key 优先挑「可用资料」或「还空着的字段」里出现过的键；资料库里确实没有这一"
        "栏，才用你自己起的英文小写键名；\n"
        "2. 表单栏的叫法和资料不一样、指的却是同一条时，挑那一条的键"
        "（身高 / 身高(cm) / stature 都是同一条资料）；\n"
        "2.1 same_as 用来把「叫法不同、其实是同一条」说清楚：这一栏如果就是"
        "「可用资料」里的某一条，把那条的键原样填进 same_as；确实是资料库里没有的"
        "新栏目才留空。不确定就留空，本机还会自己核对一遍；\n"
        "3. 字段有「可选项」时，value 必须原样抄其中一个选项，一个字都不要改；\n"
        "4. 字段没有「可选项」时，优先把资料里那一项的原值照抄；栏位文字或初判里交代了"
        "单位、或要求「仅限数字」时，按它的要求改写（身高 167cm → 167 或 1.67）；\n"
        "5. 资料里没有这一条、但能从已有资料或简历正文推出来时，自己写 value，"
        "并且必须给 basis 一句话说清依据。能推的例子：学历「本科」→ 学位「学士」、"
        "学历「大学本科」；专业「信息安全 080904K」→ 专业类「计算机类」、学科门类"
        "「工学」；本科 + 工学 → 学位名称「工学学士学位」；毕业院校在国内 → 毕业生类型"
        "「国内应届毕业生」；毕业年份 → 是否应届「是」。凡是自己写的 value 都要给 "
        "basis，本机会把它单列一区请用户核对；一点依据都说不出来的就不要出现在 JSON 里；\n"
        "6. 「本机规则初判」是程序按关键词先猜的一版，只作参考：它对了就照抄它的 key 与 "
        "value，它错了就按你的判断改；\n"
        "7. 判断不了的字段不要出现在 JSON 里，留空比填错好；\n"
        "8. 只输出 JSON，不要任何解释。\n"
        '例如：{"3": {"key": "school", "value": "某某大学"}, '
        '"5": {"key": "available", "value": "一周内"}, '
        '"7": {"key": "degree_name", "value": "学士", "basis": "资料里学历是本科", '
        '"same_as": "degree_name"}}'
    )

    def __init__(
        self,
        profile: ApplyProfile,
        *,
        llm: LlmSettings | None = None,
        domain: str = "",
        timeout: float = 60,
    ) -> None:
        self.profile = profile
        self.llm = llm or LlmSettings.load()
        self.domain = domain
        self.timeout = timeout

    async def match(self, fields: list[FormField], *, outline: str = "") -> list[FormField]:
        """规则 + AI 一次做完；不需要拆开时用这个。"""
        self.match_rules(fields)
        await self.match_ai(fields, outline=outline)
        return fields

    def match_rules(self, fields: list[FormField]) -> list[FormField]:
        """只用记忆与规则（含上传栏）匹配一遍，不碰模型——这一步是快活。

        网申面板要「秒开」，先拿这一步的结果把对照表摆出来；模型兜底留给
        :meth:`match_ai` 在后台补。
        """
        history = ApplyMappingRow.lookup(self.domain, [f.signature() for f in fields])
        # 站点改版换了签名、栏名没变时，靠归一化标签也能沿用旧映射
        labels = self._label_index()
        seen: dict[str, int] = {}
        for field in fields:
            label = normalize(field.label)
            repeated = bool(label) and seen.get(label, 0) > 0
            if label:
                seen[label] = seen.get(label, 0) + 1
            if self._apply_file(field):
                continue
            # 同一标签在页面上重复出现（省 / 市 / 区三级都叫「生源地」）时，记忆里
            # 那一条只对得上第一级，后面的交给规则重判，免得三级全填成同一个值。
            if not repeated and self._apply_history(field, history, labels):
                continue
            self._apply_rule(field)
        return self.spread(fields)

    async def match_ai(
        self, fields: list[FormField], *, outline: str = ""
    ) -> list[FormField]:
        """把整张表交给模型逐栏出值；未开通 AI 服务就静默跳过。

        ``outline`` 是页面的可访问性大纲（可选）：模型据此看清「哪一栏属于哪一段」，
        两处都叫「开始时间」时才分得清该填入学还是该填工作。

        这里不再只挑「规则没命中的」：网申表单千站千面，关键词表够不着的叫法太多，
        逐栏判断本来就该由模型来做。规则与记忆先出一版初判，作为参考一并发给模型，
        它照抄就保留初判的来源，它改了就盖过初判。
        """
        pending = [field for field in fields if askable(field)]
        if pending:
            await self._apply_ai(pending, outline=outline)
        return self.spread(fields)

    def spread(self, fields: list[FormField]) -> list[FormField]:
        """把栏与栏之间的关系摊平：级联组、重复块。

        匹配方式不止一种（整表规则、整表规划、逐栏推理），但收尾这一步是共用的——
        级联的下一级要跟着第一级一起填，同名重复块（省 / 市 / 区三级都叫「生源地」）
        要各归各位。哪条路进来都得走这一遍，所以单独摆出来。
        """
        self._spread_cascade(fields)
        self._spread_repeats(fields)
        return fields

    async def match_one(
        self,
        field: FormField,
        *,
        outline: str = "",
        rejected: set[str] | None = None,
    ) -> str:
        """只判这一栏，判完就地落地，返回一句可复盘摘要；没认领就返回空串。

        「逐栏推理、逐栏填入」走这条路。整表出计划时，模型一张表三四十栏、看着看着
        就顾此失彼：认得前一半就收工，后半段整片空着——换个企业又重演一遍。一栏一栏
        地问，它每次只看这一栏，注意力全在这儿。

        落地那一步与批量计划共用 :meth:`_land_row`，取值规矩完全一致：只能来自用户
        自己的资料 / 简历正文，或页面上真实存在的选项；资料里没有、模型却按已有资料
        推得出来的（必须给出 ``basis``），落成 ``inferred`` 的「推算」项，面板单列一区
        请用户核对。敏感事实（身份证、银行卡）既不推理也不外发。

        ``rejected`` 是「用户在这个站点删过的推算」，一次批量推理里取一次传进来即可；
        逐栏去查库既慢又没意义——同一趟里这份名单不会变。
        """
        if not self.llm.ready or not self.profile:
            return ""
        if rejected is None:
            rejected = ApplyRejectRow.rejected(self.domain)
        try:
            plan = await self._ask([field], outline)
        except Exception as exc:  # noqa: BLE001 逐栏推理只该跳过这一栏，不该拖垮整张表
            log(
                f"AI 逐栏推理未跑通（{field.label or field.name}）：{exception_text(exc)}",
                tag="apply",
            )
            return ""
        row = plan.get(str(field.index))
        if not row:
            return ""
        return self._land_row(field, row, rejected)

    OPTION_SYSTEM: ClassVar[str] = (
        "你在帮求职者把「网申资料」里的一个值对到企业官网表单的选项上。\n"
        "我会给出栏位名、资料里的值和这一栏的全部可选项。\n"
        "请只从可选项里挑一个意思最贴近的，原样抄它的文字；可选项里确实没有意思"
        "对得上的，就回 null——绝不自己造一个新选项，也别挑一个意思不同的凑数。\n"
        '只输出 JSON：{"option": "选项原文"} 或 {"option": null}。不要解释。'
    )

    async def ask_option(self, field: FormField, value: str) -> str:
        """本地对不上官网选项时，请模型在选项里挑一个最贴近资料的。

        各家官网把同一件事写成什么字全凭它自己：资料里是「本科」，选项里排的是
        「大学本科」；资料里是「随时到岗」，选项里写的是「一周内」。本地的同义表与
        学历层次表只能覆盖常见的那几种，剩下的长尾交给模型判。

        返回选项原文；挑不出（未开通 AI、调用失败、模型给的不是选项里的原文）一律
        返回空串——宁可留空请用户自己选，也不硬塞一个意思不对的选项。敏感事实
        （身份证、银行卡）不外发。
        """
        options = [str(item).strip() for item in (field.options or []) if str(item).strip()]
        text = str(value or "").strip()
        if not options or not text or not self.llm.ready:
            return ""
        if safety.is_sensitive(field.key):
            return ""
        prompt = (
            f"栏位名：{field.label or field.name or field.placeholder or '（无名）'}\n"
            f"资料里的值：{safety.redact(text)}\n"
            f"可选项：{'、'.join(options)}"
        )
        try:
            raw = (await self._ask_option(prompt)).output
            payload = extract_json(raw)
        except Exception as exc:  # noqa: BLE001 挑选项是加分项，失败不该拖垮整张表
            log(
                f"AI 选项对齐未跑通（{field.label or field.name}）：{exception_text(exc)}",
                tag="apply",
            )
            return ""
        picked = ""
        if isinstance(payload, dict):
            picked = str(payload.get("option") or "").strip()
        # 模型必须原样抄一个选项：多字少字、自己造的，一律作废
        return picked if picked in options else ""

    async def align_or_ask(self, field: FormField, key: str, value: str) -> bool:
        """规则认下了这一栏、值却对不上官网选项：本地对齐一轮，再请模型挑一轮。

        两边都挑不出来就返回 ``False``，这一栏照旧留空请用户手动选——填错比留空麻烦。
        """
        if not field.options or not str(value or "").strip():
            return False
        option = align_option(value, field.options) or await self.ask_option(field, value)
        if not option:
            return False
        self._assign(field, key=key, value=option, source="资料", confidence=0.9)
        return True

    async def _ask_option(self, prompt: str):
        """把「这一栏该选哪个选项」交给模型判一遍。"""
        model = OpenAIChatModel(self.llm.model, provider=self.llm.provider)
        agent = telemetry.attach(
            Agent(
                model,
                output_type=str,
                system_prompt=self.OPTION_SYSTEM,
                retries={"output": OUTPUT_RETRIES},
                model_settings=OpenAIChatModelSettings(
                    thinking=False, temperature=0, timeout=self.timeout, max_tokens=MAX_OUTPUT_TOKENS
                ),
            )
        )
        return await agent.run(prompt)

    TIDY_SYSTEM: ClassVar[str] = (
        "你在帮求职者整理「网申助手」从企业官网上学回来的字段。\n"
        "我会给出若干条（序号 + 栏位名 + 学到的值），请你做两件事，只输出一个 JSON 对象。\n"
        "一、挑出**根本不是一条个人资料**的条目：{\"drop\": [序号, 序号]}。该挑出来的："
        "下拉没选时的占位提示（请选择 / 请输入 / 点击添加）、按钮或声明栏的文字"
        "（确认投递 / 亲属声明 / 人员类型（校招））、把一整份选项堆在一起的值（有 / 无）、"
        "纯符号或空话（暂无 / -- / 是 / 否 / 无）、一次性的验证码 / 流水号，以及"
        "「类型 / 等级 / 称谓 / 开始时间 / 结束时间 / 获得时间」这种脱离上下文就指不明"
        "的从属栏位。\n"
        "二、归纳同义叫法：同一件事被官网写出好几个名字时，给它们同一个规范名："
        "{\"merge\": {\"序号\": \"规范名\"}}。规范名尽量用库里已有的中文标签（姓名 / "
        "民族 / 政治面貌 / 籍贯 / 生源地 / 身高 / 体重 / 紧急联系人 / 内推码……）；"
        "拿不准就别给 merge，宁可留着。\n"
        "不该挑也不该并的：真实的人事信息，哪怕看着奇怪。拿不准就保留、别动，"
        "宁多留一条也别删错。\n"
        "一条都不该删就回 {\"drop\": []}；没有要归纳的就回 {\"merge\": {}}。"
        "只输出 JSON，不要任何解释。"
    )

    async def tidy_learned(self, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        """让模型把学回来的条目判一遍：剔掉不是资料的，并把同义叫法归并成一条。

        规则那一层（占位文字 / 按钮字 / 选项堆叠）已经在本地拦掉绝大多数，这里兜的是
        它拦不住的长尾：官网自定的怪栏位、把声明正文当值学回来的那种，以及同一件事
        被官网写出好几个名字（专业类别 / 专业类 / major_category）。未开通 AI 服务、
        调用失败、回话解析不出来，都原样返回——整理只是加分项，不该让学习本身失败。
        """
        if not rows or not self.llm.ready:
            return rows
        listing = "\n".join(
            f"{pos}. 栏位「{row.get('label') or row.get('key') or ''}」"
            f"学到「{row.get('value') or ''}」"
            for pos, row in enumerate(rows)
        )
        try:
            raw = (await self._ask_tidy(listing)).output
            payload = extract_json(raw)
            drop = parse_drop_indexes(payload)
            merge = parse_merge_keys(payload)
        except Exception as exc:  # noqa: BLE001 整理是加分项，失败不该拖垮学习
            log(f"AI 整理学习字段未跑通：{exception_text(exc)}", tag="apply")
            return rows
        kept: list[dict[str, str]] = []
        index_of: dict[str, int] = {}
        merged = 0
        for pos, row in enumerate(rows):
            if pos in drop:
                continue
            item = dict(row)
            name = merge.get(pos)
            if name:
                # 模型给的名字先归到库里那条规范键（专业类别 / major_category → 同一个），
                # 归不出来（自定字段）才拿模型给的名字当新键
                key = knowledge.canonical_key(name) or name
                if key and key != str(item.get("key") or ""):
                    merged += 1
                item["key"] = key or str(item.get("key") or "")
            key = str(item.get("key") or "")
            # 归并后撞名：只留一条，后来者覆盖前者（与落库时的「同键覆盖」一致）
            if key and key in index_of:
                kept[index_of[key]] = item
                continue
            if key:
                index_of[key] = len(kept)
            kept.append(item)
        dropped = [rows[pos] for pos in sorted(drop) if 0 <= pos < len(rows)]
        if dropped:
            log(
                f"AI 整理学习字段：剔掉 {len(dropped)} 条无效项"
                f"（{'、'.join(str(row.get('label') or row.get('key')) for row in dropped)}）",
                tag="apply",
            )
        if merged:
            log(f"AI 整理学习字段：把 {merged} 条同义叫法归并到规范键", tag="apply")
        return kept

    async def _ask_tidy(self, listing: str):
        """把「学到的条目」交给模型判一遍。"""
        model = OpenAIChatModel(self.llm.model, provider=self.llm.provider)
        agent = telemetry.attach(
            Agent(
                model,
                output_type=str,
                system_prompt=self.TIDY_SYSTEM,
                retries={"output": OUTPUT_RETRIES},
                model_settings=OpenAIChatModelSettings(
                    thinking=False, temperature=0, timeout=self.timeout, max_tokens=MAX_OUTPUT_TOKENS
                ),
            )
        )
        return await agent.run(f"条目：\n{listing}")

    def _label_index(self) -> dict[str, ApplyMappingRow]:
        """归一化标签 → 最近一条记忆；同一标签有多条时取最近更新的那条。"""
        index: dict[str, ApplyMappingRow] = {}
        for row in ApplyMappingRow.rows_for_domain(self.domain):
            text = normalize(row.label)
            if text and text not in index:
                index[text] = row  # rows_for_domain 已按更新时间倒序
        return index

    def _spread_repeats(self, fields: list[FormField]) -> None:
        """同一栏在页面上出现多次时，把多段资料按结束日期倒序依次填进去。

        官网的「实习经历」常给好几组一模一样的空栏，而资料里一段文本可能写了
        好几段经历（用空行隔开）。这里按结束日期倒序拆开，最近的经历落在最前面
        那一组；只有一段资料时原样不动，免得把一整段正文塞进第一栏。
        """
        groups: dict[str, list[FormField]] = {}
        for field in fields:
            if field.key and field.filled and field.source != "手动":
                groups.setdefault(field.key, []).append(field)
        for key, items in groups.items():
            if len(items) < 2:
                continue
            parts = safety.order_by_end_date(safety.split_entries(self.profile.get(key)))
            if len(parts) < 2:
                continue
            for item, part in zip(items, parts):
                item.filled = part

    def _spread_cascade(self, fields: list[FormField]) -> None:
        """同一标签重复出现多个下拉，就是「省 / 市 / 区」这种级联。

        资料里的地址常写成「浙江省杭州市西湖区」一整串，页面上却拆成三个
        下拉。对照表里没必要把同一栏拆成三行给用户看：整串地址留在第一级那一行，
        其余几级标成 ``hidden`` 只参与填写、不单独展示。填表环节按行政区后缀把地址
        拆开逐级往下选，下一级的选项要等上一级选完才刷新，所以那边会重读选项等它出来。

        只按标签分组、不要求每级都已认领到资料键：级联的下一级在扫描那一刻选项
        往往只有一个占位项（「所属城市」），规则和 AI 都认不出它，如果因为没键就
        跳过，那一行会留在面板上多出一栏。同一标签下若出现两个不同的资料键，说明
        它们不是一条级联（例如两组独立的「开始时间」），那就各自按规则判。
        """
        groups: dict[str, list[FormField]] = {}
        for field in fields:
            if field.tag != "select" or field.hidden:
                continue
            text = normalize(field.label)
            if text:
                groups.setdefault(text, []).append(field)
        for items in groups.values():
            # 用户手动改过其中一级就别再动这一组，免得把人的选择覆盖掉
            if len(items) < 2 or any(item.source == "手动" for item in items):
                continue
            keys = {item.key for item in items if item.key}
            if len(keys) > 1:
                # 同一标签下认领到了不同的键：不是级联，别合并
                continue
            key = next(iter(keys), "") or self._rule_key(items[0])
            if not key:
                continue
            parts = split_admin(self.profile.get(key))
            if len(parts) < 2:
                # 资料里只有一级：不合并，让每一级各自按规则判
                items[0].group = []
                for item in items[1:]:
                    item.hidden = False
                continue
            head, rest = items[0], items[1:]
            # 整串地址挂在第一级这一行，面板上就只看到「生源地」一行
            self._assign(
                head, key=key, value=self.profile.get(key), source="资料", confidence=0.9
            )
            head.group = [item.index for item in rest]
            for item in rest:
                item.key = key
                item.filled = ""
                item.source = ""
                item.confidence = 0.0
                item.ok = True
                item.note = ""
                item.hidden = True

    def remember(self, fields: list[FormField]) -> int:
        """把本次确认过的映射记到域名下，下次同一个官网直接沿用。"""
        items = [
            # 存的是资料键（不是「资料 / AI / 手动」这类来源词）：下次沿用时
            # 要靠它把这一栏还原成下拉里那条资料，存成来源词会变成「资料」这种怪选项。
            {
                "signature": f.signature(),
                "label": f.label,
                "value": f.filled,
                "source": f.key,
                # 用户手动改过的映射下次优先沿用，不被规则或 AI 覆盖
                "manual": f.source == "手动",
            }
            for f in fields
            # 上传栏不记：它的「值」是本机文件，记进映射表只会在下次冒出「简历原件」这种怪选项
            if f.key and f.filled and not f.is_file and not f.hidden
        ]
        return ApplyMappingRow.remember(self.domain, items)

    # --- 三档匹配 ---

    def _apply_history(
        self,
        field: FormField,
        history: dict[str, ApplyMappingRow],
        labels: dict[str, ApplyMappingRow],
    ) -> bool:
        """沿用记忆：用户手动改过的优先，其次同签名的，最后同标签的（站点改版换了壳）。

        标签对不上就别沿用：上一版把隔壁单元格的标签一起拼了进来（电子邮箱
        被安到身份证号头上），旧值很可能已经对不上这一栏，宁可重新按规则判一次。
        """
        rows: dict[str, ApplyMappingRow] = {}
        by_signature = history.get(field.signature())
        if by_signature is not None:
            rows[by_signature.key] = by_signature
        if field.label:
            by_label = labels.get(normalize(field.label))
            if by_label is not None:
                rows[by_label.key] = by_label
        usable = [
            row
            for row in rows.values()
            if row.value
            and row.source.strip() not in _STALE_KEYS
            and normalize(row.label) == normalize(field.label)
        ]
        if not usable:
            return False
        # 手动改过的排最前，其余按最近更新
        row = max(usable, key=lambda item: (item.manual, item.updated_at))
        value = row.value
        if field.tag == "select" and field.options:
            option = align_option(value, field.options)
            if not option:
                # 记忆里的值对不上这一栏现在的选项（多半是上一级联动的占位项），
                # 宁可重新按规则判一次
                return False
            value = option
        if row.manual:
            self._assign(
                field, key=row.source or "manual", value=value, source="手动", confidence=1.0
            )
        else:
            self._assign(
                field, key=row.source or "manual", value=value, source="沿用", confidence=0.95
            )
        return True

    def _apply_rule(self, field: FormField) -> bool:
        key = self._rule_key(field)
        if not key:
            return False
        value = self.profile.get(key)
        if not value:
            return False
        if field.tag == "select" and field.options:
            option = align_option(value, field.options)
            if not option:
                field.key = key
                field.ok = False
                field.note = "表单选项里没有匹配项，请手动选择"
                return True
            self._assign(field, key=key, value=option, source="资料", confidence=0.9)
            return True
        if field.type in ("radio", "checkbox"):
            if field.tag == "div" and field.options:
                # 整组单选 / 多选（组件库不给每个选项配 input）：在选项里挑对得上的那个
                option = align_option(value, field.options)
                if not option:
                    field.key = key
                    field.ok = False
                    field.note = "选项里没有匹配项，请手动选择"
                    return True
                self._assign(field, key=key, value=option, source="资料", confidence=0.9)
                return True
            own = normalize(field.label or field.value)
            if own and (normalize(value) in own or own in normalize(value)):
                self._assign(field, key=key, value=field.label or value, source="资料", confidence=0.85)
            return True
        self._assign(field, key=key, value=self._shape(key, value, field), source="资料", confidence=0.9)
        return True

    @staticmethod
    def _shape(key: str, value: str, field: FormField) -> str:
        """按这一栏的提示把值改成它要的样子（身高 175 ↔ 1.75 ↔ 175cm）。

        表单在标签、占位符或 ``min/max/pattern/inputmode`` 里交代了单位与「只填数字」
        时，同一份身高要换个写法才填得进去；看不出提示就原样照抄。
        """
        return knowledge.adapt(
            key,
            value,
            label=field.label,
            placeholder=field.placeholder,
            field_type=field.type,
            hints=field.hints,
        )

    @staticmethod
    def _derive_ok(key: str, want: str, basis: str) -> bool:
        """模型自己写值时必须给出依据——依据是给用户核对用的，不能省。

        资料里没有这一条时，模型写的自由文本一律进「推算」区，和已对上的项隔开，
        由用户逐条删掉不认的；所以这里只卡「有键、有值、说得出依据、依据不长不短」，
        不再限制键的范围——``_DERIVABLE`` 那几栏只是可信度给高一档的参考。
        """
        return bool(key and want and basis) and len(basis) <= 60

    def _claim_key(self, raw_key: str, same_as: str = "") -> str:
        """把模型给的键名归到库里那一条；模型说「这就是某某」时以本机核对为准。

        先按模型给的键归（``stature`` → ``height``）；归不出库里已有条目时，再看
        模型在 ``same_as`` 里说的共指——模型有时会新起一个名字，却又说清「其实
        就是库里某某」。两边都归不上就按模型给的标签当新键，后面
        :meth:`_derive_ok` 还会再卡一道「必须有依据」。
        """
        primary = knowledge.canonical_key(raw_key)
        if primary and self.profile.entry(primary) is not None:
            return primary
        reference = knowledge.canonical_key(same_as) if same_as else ""
        if reference and self.profile.entry(reference) is not None:
            return reference
        return primary or reference

    def _apply_file(self, field: FormField) -> bool:
        """文件上传栏单独认领：它不靠资料里的值，靠的是本机已经传过的简历 / 生活照。

        资料里没有「简历」这一行字，走规则那套「资料里没值就不认领」的话，上传栏
        永远拿不到 key，也就永远不会被填——这正是「显示会自动上传、实际没传」的根因。
        认领后填表环节按 key 去本机找文件；找不到就如实报「请先上传」。
        """
        if not field.is_file:
            return False
        text = field.normalized
        if not text:
            return False
        for key, keywords in _FILE_RULES:
            if any(normalize(word) in text for word in keywords):
                self._assign(
                    field,
                    key=key,
                    value=_FILE_VALUES.get(key, ""),
                    source="资料",
                    confidence=0.9,
                )
                return True
        return False

    def _rule_key(self, field: FormField) -> str:
        """按关键词命中挑一个键；同时命中多个时取关键词更长的那个。"""
        text = field.normalized
        if not text:
            return ""
        best_key = ""
        best_score = 0
        for key, keywords in RULES:
            if not self.profile.get(key):
                continue
            for keyword in keywords:
                if keyword in text and len(keyword) > best_score:
                    best_key, best_score = key, len(keyword)
        if not best_key:
            # 关键词表够不着的栏：用户自补的字段、官网的怪叫法，按界面标签对上号。
            # 「叫法不同、指的是同一条资料」就靠这一步并到一起。
            best_key = self.profile.key_of_label(field.label)
        # 通用词被专属栏借走时宁可留空：本人的姓名不该填进「紧急联系人姓名」。
        if best_key in _VETO and any(word in text for word in _VETO[best_key]):
            return ""
        return best_key

    async def _apply_ai(self, fields: list[FormField], *, outline: str = "") -> None:
        """把整张表交给模型出填表计划；未配置或调用失败都静默跳过。

        模型是这套匹配的主判，规则与记忆只是摆在提示词里的「初判」：模型照抄初判就
        保留初判的来源（资料 / 沿用 / 手动），模型给出别的结论就盖过它。**取值只能
        来自两处**：用户自己的资料 / 简历正文，或页面上真实存在的选项；资料里没有这
        一条、模型却按已有资料推得出来时（必须给出 ``basis``），落成 ``inferred`` 的
        「推算」项——网申面板会把它单列一区请用户核对，绝不混进「已对上」那一堆。
        取值一律回本机核对：模型给的自由文本不直接落库，选项不在页面上就不认，宁可
        留空请用户手动填。
        """
        if not self.llm.ready or not self.profile:
            return
        # 用户在面板上删过的推算：同一站点不再推同一条，免得反复冒出来让人删
        rejected = ApplyRejectRow.rejected(self.domain)
        started = time.perf_counter()
        try:
            plan = await self._ask(fields, outline)
        except Exception as exc:  # noqa: BLE001 AI 只是兜底，失败不该拖垮整次扫描
            # 模型调用是黑盒：出错时留一条日志，事后能复盘「哪次判断没跑起来、为什么」。
            log(f"AI 填表计划未跑通（{len(fields)} 栏待判）：{exception_text(exc)}", tag="apply")
            return
        by_index = {str(f.index): f for f in fields}
        landed: list[str] = []
        for index, raw_row in plan.items():
            field = by_index.get(str(index))
            if field is None:
                continue
            where = self._land_row(field, raw_row, rejected)
            if where:
                landed.append(where)
        # 每次模型判断都留一行可复盘的摘要：模型认了几栏、落地几栏、各栏落到了哪个键。
        elapsed = (time.perf_counter() - started) * 1000
        detail = f"（{'、'.join(landed)}）" if landed else ""
        log(
            f"AI 填表计划：模型认领 {len(plan)} 栏，落地 {len(landed)} 栏，耗时 {elapsed:.0f}ms{detail}",
            tag="apply",
        )

    def _land_row(self, field: FormField, raw_row: object, rejected: set[str]) -> str:
        """把模型对这一栏的结论落到字段上，返回一句可复盘的摘要；没落地就返回空串。

        批量计划与逐栏推理共用这一步：**取值只能来自两处**——用户自己的资料 / 简历
        正文，或页面上真实存在的选项；资料里没有这一条、模型却按已有资料推得出来时
        （必须给出 ``basis``），落成 ``inferred`` 的「推算」项，面板会把它单列一区
        请用户核对，绝不混进「已对上」那一堆。模型给的自由文本不直接落库，选项不在
        页面上就不认，宁可留空请用户手动填。
        """
        raw_key, want, basis, same_as = _plan_row(raw_row)
        # 模型可能用别的叫法（ethnicity / stature）：先归到规范键，再落到库里那条。
        # 它在 same_as 里说的共指也照同一套核对——本机只认归得到库里已有条目的键。
        key = self._claim_key(raw_key, same_as)
        entry = self.profile.entry(key) if key else None
        if entry is not None:
            key = entry.key
        if key and key in rejected:
            return ""
        value = entry.value if entry is not None else ""
        if not value:
            # 敏感事实（身份证、银行卡）不推理：号码一旦被编出来填进页面就收不回来。
            # 这两栏只认资料里的原值，其余一律留空请用户自己填。
            if key and safety.is_sensitive(key):
                return ""
            # 资料里没有这条：模型编的键一律作废；只有「有依据的推算」才收，且标成
            # inferred，面板上单列一区请用户核对（可逐条删掉不认的）。
            if self._derive_ok(key, want, basis):
                self._assign(
                    field,
                    key=key,
                    value=self._shape(key, want, field),
                    source="推算",
                    confidence=0.5 if key in _DERIVABLE else 0.4,
                )
                field.inferred = True
                field.note = basis
                return f"{field.label or field.name}→{key}（推算：{basis}）"
            return ""
        if field.options:
            option = _plan_option(want, value, field.options)
            if not option:
                # 模型和资料都对不上官网选项：认下这一栏，请用户手动选
                self._assign(field, key=key, value="", source="AI", confidence=0.0)
                field.ok = False
                field.note = "表单选项里没有匹配项，请手动选择"
                return f"{field.label or field.name}→{key}（选项不匹配）"
            chosen = option
        else:
            # 模型改写过的（不是原值照抄）算「推算」：本机照旧按这一栏的提示过一遍
            # 单位，再标上模型给的依据，用户一眼看得出这条不是资料原文。
            chosen = self._shape(key, want or value, field)
        # 模型与初判给出的是同一条、同一个值：初判的来源（资料 / 沿用 / 手动）比
        # 「AI」更贴实情，保留它——别把「照抄资料原文」记成「模型填的」。
        if field.ok and field.key == key and normalize(field.filled) == normalize(chosen):
            return f"{field.label or field.name}→{key}（沿用初判）"
        derived = bool(basis) and normalize(want) != normalize(value)
        self._assign(
            field,
            key=key,
            value=chosen,
            source="推算" if derived else "AI",
            confidence=0.5 if derived else 0.6,
        )
        if derived:
            # 模型自己写的值：单列一区请用户核对，用户删掉就不填、不入库，留着并点
            # 「填入浏览器」才记进本机资料库。
            field.inferred = True
            field.note = basis
        return f"{field.label or field.name}→{key}"

    async def _ask(
        self, fields: list[FormField], outline: str = ""
    ) -> dict[str, tuple[str, str, str, str]]:
        """构造提示词并解析模型返回的填表计划。

        返回 ``{字段序号: (资料键, 选项值, 依据, 共指键)}``。

        兼容两种写法：新版 ``{"key": ..., "value": ...}``，以及旧版的纯键字符串
        （那时只会挑资料、不会挑选项，值留空由本机对齐）。
        """
        model = OpenAIChatModel(self.llm.model, provider=self.llm.provider)
        agent = telemetry.attach(
            Agent(
                model,
                output_type=str,
                system_prompt=self.SYSTEM,
                retries={"output": OUTPUT_RETRIES},
                model_settings=OpenAIChatModelSettings(
                    thinking=False, temperature=0, timeout=self.timeout, max_tokens=MAX_OUTPUT_TOKENS
                ),
            )
        )
        result = (await agent.run(self._prompt(fields, outline))).output
        return parse_plan(extract_json(result))

    def _prompt(self, fields: list[FormField], outline: str = "") -> str:
        # 内置字段 + 用户在配置中心补的自定义字段（民族、政治面貌……）一起交给模型，
        # 只给内置字段的话，官网那些「上面没有的栏」永远轮不到 AI 兜底。
        # 外发前过一遍数据最小化：身份证、银行卡这类敏感事实只在本机用。
        standard = {k for k, _ in RULES}
        entries = safety.public_entries(
            e for e in self.profile.entries if e.key in standard or e.source == "自定义"
        )
        lines = [f"- {e.key}（{e.label}）：{e.value}" for e in entries]
        # 库里有这个键、却还没写值的那些：告诉模型「这些栏是认得的，只是本机没填」。
        # 外发同样过数据最小化：敏感事实（身份证、银行卡……）连键名都不给。
        blanks = [
            f"- {key}（{label}）"
            for key, label in ApplyProfileSettings.LABELS.items()
            if key in standard and not self.profile.get(key) and not safety.is_sensitive(key)
        ]
        rows = [
            f"- 序号 {f.index}：{f.label or f.name or f.placeholder}（控件 {f.type}"
            + (f"，可选项：{'、'.join(f.options)}" if f.options else "")
            + (f"，本机规则初判：{f.key} → {f.filled}" if f.key and f.filled else "")
            + ")"
            for f in fields
        ]
        parts = ["可用资料：", *lines, ""]
        # 简历正文是「资料库」之外唯一的事实来源：结构化资料空着时，模型全靠它推。
        # 外发前过一遍号码掩码——身份证 / 银行卡这类号码不该离开本机。
        resume = safety.redact(self.profile.resume).strip()
        if resume:
            parts += ["简历正文（本人真实经历，取值或推理都可以用它）：", resume, ""]
        if blanks:
            parts += ["资料库里还空着的字段（本机没有值，不要凭空编）：", *blanks, ""]
        if outline.strip():
            # 可访问性大纲：缩进就是层级，分段标题也在里面。同一段里出现好几个
            # 「开始时间」时，靠它才分得清哪一栏属于哪一段经历。
            parts += [
                "表单结构（浏览器算出的可访问性树，缩进表示层级；"
                "序号以「还没识别的表单字段」那份为准）：",
                outline.strip(),
                "",
            ]
        parts += [
            "表单字段：",
            *rows,
            "",
            "请输出 JSON：字段序号 → {key, value, basis, same_as}。有可选项时 value 必须"
            "原样抄其中一个；只有自己推算出来的 value 才需要填 basis（一句话说清依据）。",
        ]
        return "\n".join(parts)

    @staticmethod
    def _assign(field: FormField, *, key: str, value: str, source: str, confidence: float) -> None:
        field.key = key
        field.filled = value
        field.source = source
        field.confidence = confidence
        field.ok = True
        field.note = ""
