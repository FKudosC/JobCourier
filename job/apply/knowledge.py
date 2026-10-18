"""网申知识的两个小引擎：确定性推算，与按表单提示把值改成这一栏要的样子。

网申表单千站千面，光靠「关键词命中 → 原样照抄」总有够不着的地方：

* **推算** —— 资料里只写了「本科」「信息安全」，表单却问「学位」「学科门类」。
  这些对应关系是确定的（本科 → 学士；080904K → 计算机类 / 工学），按规则推出来
  即可，不必让模型猜。推出来的值一律标上依据，让用户在对照表上看得见「这是推的」；
* **变形** —— 同一条身高，有的表单要 ``175``、有的要 ``1.75``、有的要 ``175cm``。
  按表单给出的单位提示与「仅限数字」这类限制，把值改成这一栏要的样子。

两条硬规矩：推算只填资料里**空着**的栏，用户亲手填的永远优先；推不出确定依据的
就不填，宁可留空请用户手动补——填错比留空麻烦得多。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date

# ---------------------------------------------------------------------------
# 推算：学历 → 学位
# ---------------------------------------------------------------------------

# 学历的说法很多（大学本科 / 全日制本科 / 硕士研究生），先归到一个层次上
_DEGREE_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("博士", ("博士",)),
    ("硕士", ("硕士", "研究生")),
    ("本科", ("本科", "学士")),
    ("专科", ("专科", "大专", "高职", "高专")),
    ("高中", ("高中", "中专", "职高", "技校")),
)

# 层次 → 学位；查不到（专科及以下）就没有学位这一说，不推
_DEGREE_NAMES: dict[str, str] = {"本科": "学士", "硕士": "硕士", "博士": "博士"}

# 反过来：资料里只写了「学位」，表单问「学历」时也要认得出（学士 → 本科）。
# 学位与学历不是一一对应（「硕士」两边都叫硕士），但按层次反推够用。
_DEGREE_BY_NAME: dict[str, str] = {"学士": "本科", "硕士": "硕士", "博士": "博士"}


def degree_level(text: str) -> str:
    """把「全日制本科」「硕士研究生」这类写法归到本科 / 硕士 / 博士……；认不出返回空串。"""
    value = _clean(text)
    if not value:
        return ""
    for level, words in _DEGREE_LEVELS:
        if any(word in value for word in words):
            return level
    return ""


# ---------------------------------------------------------------------------
# 推算：专业 → 专业类 / 学科门类
# ---------------------------------------------------------------------------

# 专业代码 → （专业类, 学科门类）。只列常见的那一批；查不到就退回按专业名认。
MAJOR_CODES: dict[str, tuple[str, str]] = {
    "080901": ("计算机类", "工学"),
    "080902": ("计算机类", "工学"),
    "080903": ("计算机类", "工学"),
    "080904K": ("计算机类", "工学"),
    "080905": ("计算机类", "工学"),
    "080906": ("计算机类", "工学"),
    "080907T": ("计算机类", "工学"),
    "080910T": ("计算机类", "工学"),
    "080911TK": ("计算机类", "工学"),
    "080717T": ("电子信息类", "工学"),
    "080701": ("电子信息类", "工学"),
    "080702": ("电子信息类", "工学"),
    "080703": ("电子信息类", "工学"),
    "080704": ("电子信息类", "工学"),
    "080601": ("电气类", "工学"),
    "080801": ("自动化类", "工学"),
    "080201": ("机械类", "工学"),
    "080202": ("机械类", "工学"),
    "080207": ("机械类", "工学"),
    "081001": ("土木类", "工学"),
    "081002": ("土木类", "工学"),
    "082801": ("建筑类", "工学"),
    "081301": ("化工与制药类", "工学"),
    "120201K": ("工商管理类", "管理学"),
    "120202": ("工商管理类", "管理学"),
    "120203K": ("工商管理类", "管理学"),
    "120204": ("工商管理类", "管理学"),
    "120206": ("工商管理类", "管理学"),
    "020101": ("经济学类", "经济学"),
    "020301K": ("金融学类", "经济学"),
    "020401": ("经济与贸易类", "经济学"),
    "030101K": ("法学类", "法学"),
    "030302": ("社会学类", "法学"),
    "050101": ("中国语言文学类", "文学"),
    "050201": ("外国语言文学类", "文学"),
    "050301": ("新闻传播学类", "文学"),
    "040101": ("教育学类", "教育学"),
    "070101": ("数学类", "理学"),
    "071201": ("统计学类", "理学"),
    "070201": ("物理学类", "理学"),
    "071001": ("生物科学类", "理学"),
    "100201K": ("临床医学类", "医学"),
    "101101": ("护理学类", "医学"),
    "100701": ("药学类", "医学"),
    "130502": ("设计学类", "艺术学"),
    "130202": ("音乐与舞蹈学类", "艺术学"),
}

# 专业名里出现这些词就归到对应的（专业类, 学科门类）。顺序即优先级，先长后短。
MAJOR_KEYWORDS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("信息安全", "网络空间安全", "网络工程", "软件工程", "计算机", "人工智能", "物联网", "数据科学"), "计算机类", "工学"),
    (("电子信息", "通信工程", "微电子", "集成电路"), "电子信息类", "工学"),
    (("电气工程", "电气"), "电气类", "工学"),
    (("自动化",), "自动化类", "工学"),
    (("机械", "车辆工程"), "机械类", "工学"),
    (("土木",), "土木类", "工学"),
    (("建筑学", "城乡规划"), "建筑类", "工学"),
    (("化学工程", "制药工程"), "化工与制药类", "工学"),
    (("统计学", "统计"), "统计学类", "理学"),
    (("数学", "信息与计算科学"), "数学类", "理学"),
    (("物理学", "应用物理"), "物理学类", "理学"),
    (("生物科学", "生物技术", "生物工程"), "生物科学类", "理学"),
    (("金融",), "金融学类", "经济学"),
    (("国际经济与贸易", "贸易经济"), "经济与贸易类", "经济学"),
    (("经济学", "经济"), "经济学类", "经济学"),
    (("会计", "财务管理", "工商管理", "市场营销", "人力资源"), "工商管理类", "管理学"),
    (("法学", "法律"), "法学类", "法学"),
    (("汉语言", "汉语国际"), "中国语言文学类", "文学"),
    (("英语", "日语", "德语", "法语", "俄语", "翻译"), "外国语言文学类", "文学"),
    (("新闻", "广告学", "传播学"), "新闻传播学类", "文学"),
    (("教育",), "教育学类", "教育学"),
    (("临床医学", "口腔医学"), "临床医学类", "医学"),
    (("护理",), "护理学类", "医学"),
    (("药学", "中药学"), "药学类", "医学"),
    (("视觉传达", "环境设计", "产品设计", "工业设计"), "设计学类", "艺术学"),
)

# 专业里带着的六位专业代码（可能跟一个字母后缀，如 080904K）
_MAJOR_CODE = re.compile(r"(\d{6}[A-Za-z]?)")


def split_major(text: str) -> tuple[str, str]:
    """把「信息安全（080904K）」拆成 ``("080904K", "信息安全")``；没写代码就是空串 + 原话。"""
    value = _clean(text)
    if not value:
        return "", ""
    match = _MAJOR_CODE.search(value)
    if not match:
        return "", value
    code = match.group(1).upper()
    name = _clean(value[: match.start()] + value[match.end() :])
    name = name.strip("（）()·-— \t")
    return code, name


def major_info(text: str) -> tuple[str, str] | None:
    """专业（代码或名称）→ ``(专业类, 学科门类)``；认不出返回 ``None``。"""
    code, name = split_major(text)
    if code and code in MAJOR_CODES:
        return MAJOR_CODES[code]
    for keywords, category, discipline in MAJOR_KEYWORDS:
        if any(word in name for word in keywords):
            return category, discipline
    return None


# ---------------------------------------------------------------------------
# 推算：应届身份
# ---------------------------------------------------------------------------

# 毕业年份：资料里写「2026」「2026年6月」都能读出来
_YEAR = re.compile(r"(?:19|20)\d{2}")
# 交换 / 留学经历里出现这些词，就是境外学历
_OVERSEAS_WORDS = ("留学", "海外", "国外", "出国", "境外", "交换生")


def _overseas_school(school: str) -> bool:
    """学校名里一个汉字都没有（Harvard University）时，按境外院校算。"""
    text = _clean(school)
    if not text:
        return False
    return not any("\u4e00" <= char <= "\u9fff" for char in text)


def fresh_status(values: Mapping[str, str]) -> tuple[str, str]:
    """判「国内应届毕业生 / 留学归国人员 / 往届毕业生」；看不出依据就返回空串。

    表单常把「应届 / 留学归国」做成一栏单选，问的其实是「你是哪一类毕业生」。
    这里只用资料里已有的硬证据：交换 / 留学经历写了留学、或学校名是外文 → 留学归国；
    否则拿毕业年份对当前的年份——离校太久的不硬说成应届。
    """
    exchange = _clean(values.get("exchange"))
    if exchange and any(word in exchange for word in _OVERSEAS_WORDS):
        return "留学归国人员", f"交换 / 留学经历写着「{exchange[:20]}」"
    school = _clean(values.get("school"))
    if _overseas_school(school):
        return "留学归国人员", f"毕业学校「{school}」是境外院校"
    year = _clean(values.get("graduate_year"))
    match = _YEAR.search(year)
    if not match:
        return "", ""
    graduated = int(match.group(0))
    if graduated >= date.today().year - 1:
        return "国内应届毕业生", f"毕业年份「{year}」属于应届"
    return "往届毕业生", f"毕业年份「{year}」已经离校"


def derive(values: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """按确定规则推出资料里没写的字段：返回一串 ``(字段键, 取值, 依据)``。

    只推资料里空着的键——用户亲手填过的一律不动。推不出依据的不出现在结果里。
    推算要能一环扣一环（专业 → 学科门类 → 学位名称），所以推出来的值也回填进
    这张表：只读传进来的 ``values`` 的话，「工学学士学位」永远等不到「工学」。
    """
    out: list[tuple[str, str, str]] = []
    have = {str(key): _clean(value) for key, value in values.items()}

    def add(key: str, value: str, basis: str) -> None:
        if value and not have.get(key):
            have[key] = value
            out.append((key, value, basis))

    degree = have.get("degree", "")
    if level := degree_level(degree):
        if name := _DEGREE_NAMES.get(level):
            add("degree_name", name, f"学历「{degree}」对应的学位")
    else:
        # 反着来：只写了学位（学士），表单问学历
        name = have.get("degree_name", "")
        for word, level in _DEGREE_BY_NAME.items():
            if word in name:
                add("degree", level, f"学位「{name}」对应的学历")
                break

    major = have.get("major", "")
    if major and (info := major_info(major)):
        category, discipline = info
        add("major_category", category, f"专业「{major}」所属的专业类")
        add("discipline", discipline, f"专业「{major}」所属的学科门类")

    # 学位名称：「工学」+「学士」→「工学学士学位」。表单把「学位」和「学位名称」
    # 拆成两栏问时，后一栏要的是带学科门类的全称。
    discipline = have.get("discipline", "")
    name = have.get("degree_name", "")
    # 用户自己写的学位已经带着学科门类（「工学学士」）时不再套一层，免得拼成
    # 「工学工学学士学位」这种怪东西。
    if discipline and name and discipline not in name:
        add("degree_full", f"{discipline}{name}学位", f"学科门类「{discipline}」+ 学位「{name}」")

    status, basis = fresh_status(have)
    if status:
        add("is_fresh_graduate", status, basis)

    return out


# ---------------------------------------------------------------------------
# 同义归并：叫法不同、指的是同一条资料
# ---------------------------------------------------------------------------

# 规范键 → 别的叫法。用户自己补的字段、模型回过来的键、老版本存下的键，写法五花八门；
# 归到一个键上，才不会出现「库里明明有身高，换个名字就对不上」。
KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("fullname", "realname", "yourname", "username", "candidatename"),
    "name_pinyin": ("pinyin", "namepinyin"),
    "english_name": ("englishname",),
    "former_name": ("formername", "usedname", "previousname"),
    "gender": ("sex",),
    "birth": (
        "birthday", "birthdate", "dateofbirth", "dob", "born",
        "出生日期", "出生年月日", "生日",
    ),
    "birth_place": ("birthplace", "placeofbirth"),
    "nation": ("ethnicity", "ethnic", "ethnicgroup"),
    "nationality": ("country", "citizenship"),
    "political": ("politicalstatus", "politics", "party"),
    "party_time": ("partytime", "partydate"),
    "marital": ("maritalstatus", "marriage", "maritalstate"),
    "id_type": ("idtype", "certtype", "idkind"),
    "id_card": (
        "idcard", "idnumber", "idno", "certno", "identitycard",
        "身份证号码", "证件号码", "证件号", "身份证件号码",
    ),
    "id_expiry": ("idexpiry", "idexpire", "idvalidity"),
    "hukou": ("household", "hukouplace", "residence", "domicile", "户口所在地"),
    "hukou_type": ("hukoutype", "householdtype"),
    "school_hukou": ("schoolhukou", "户口是否在学校", "学校集体户口"),
    "native_place": ("nativeplace", "hometown", "ancestralhome", "祖籍", "家乡"),
    "origin_place": (
        "originplace", "sourceplace", "originarea", "生源所在地", "生源地区",
    ),
    "height": ("stature", "身高cm", "身高厘米"),
    "weight": ("bodyweight", "体重kg", "体重公斤"),
    "phone": (
        "mobile", "tel", "telephone", "cellphone", "phonenumber",
        "手机号码", "联系电话", "其他联系方式手机号",
    ),
    "email": ("mail", "emailaddress", "mailbox"),
    "wechat": ("weixin",),
    "qq": ("qqnumber",),
    "city": ("currentcity", "residencecity", "livedcity"),
    "address": ("addr", "mailingaddress", "contactaddress"),
    "postal": ("zipcode", "zip", "postalcode", "postcode"),
    "current_location": ("currentlocation", "presentlocation"),
    "school": (
        "university", "schoolname", "graduatedschool",
        "学校名称", "毕业院校", "院校名称", "就读学校",
    ),
    "college_name": (
        "faculty", "department", "institute",
        "学院名称", "院系名称", "所在学院", "所属学院",
    ),
    "major": ("specialty", "majorname", "fieldofstudy", "专业名称", "所学专业"),
    "major_category": ("majorclass", "majorcategory", "majortype"),
    "discipline": ("disciplinename", "disciplines"),
    "degree": ("education", "educationlevel", "degreelevel", "highestdegree"),
    "degree_name": ("degreename", "academicdegree", "awardeddegree"),
    "study_mode": ("studymode", "studytype"),
    "graduate_year": (
        "graduationyear", "graduation", "graduationdate",
        "毕业时间", "毕业年月", "毕业日期",
    ),
    "start_year": ("enrollmentdate", "admissiondate", "enrollyear"),
    "gpa": ("gradepoint", "gradeaverage", "gpaavg"),
    "rank": ("ranking", "classrank"),
    "cet": ("cet4", "cet6", "englishlevel"),
    "language_skill": ("语言类型", "外语水平", "语言水平", "外语语种"),
    "mandarin": ("mandarinlevel",),
    "expected_salary": ("salaryexpectation", "expectedsalary", "salarywant"),
    "available": ("onboard", "availability", "reportdate", "onboarddate"),
    "expected_city": (
        "expectedcity", "desiredcity", "workcity",
        "期待工作地点", "期望工作地点", "意向工作地点",
    ),
    "expected_position": ("expectedposition", "desiredposition", "applyposition"),
    "job_type": ("jobtype", "employmenttype"),
    "accept_adjust": ("acceptadjust", "调剂", "服从调剂", "是否服从调剂"),
    "work_years": ("yearsofexperience", "workyears", "experienceyears"),
    "self_intro": (
        "selfintroduction", "introduction", "aboutme", "selfdescription",
        "自我评价", "个人简介", "个人介绍", "自我描述",
    ),
    "skills": ("专业技能", "技能特长", "技能专长", "技能/兴趣爱好", "技能兴趣爱好"),
    "awards": ("获奖情况", "奖励/证书名称", "奖励情况", "获奖经历", "荣誉奖项"),
    "certificates": ("证书", "证书描述", "证书名称", "资格证书"),
    "is_fresh_graduate": ("是否应届", "应届生", "是否应届毕业生"),
    "has_children": ("haschildren", "子女情况", "有无子女"),
    "person_type": ("persontype", "人员类型", "招聘类型", "校招类型"),
    "github": ("gitee", "coderepo"),
    "portfolio": ("website", "homepage", "blog", "personalwebsite"),
    "emergency_name": ("emergencycontact", "emergencyname"),
    "emergency_phone": ("emergencyphone", "emergencytel"),
    "emergency_relation": ("relationship", "relation", "emergencyrelation"),
    "referral_code": ("referralcode", "referral", "invitecode"),
    "source_channel": ("sourcechannel", "channel", "knowfrom"),
}

# 反向索引：别名 → 规范键。建表时顺手把规范键自己也收进去，查起来不用分情况。
_ALIAS_TO_KEY: dict[str, str] = {
    alias: key for key, aliases in KEY_ALIASES.items() for alias in aliases
}

# 用户在信息树上手动合并的那些：内置别名表并不掉的叫法，由用户自己认下来。
# 存在本机库里（``ApplyAliasRow``），这里只留一份进程内缓存，合并 / 撤销时刷新。
_USER_ALIASES: dict[str, str] = {}
_USER_LOADED = False


def user_aliases() -> dict[str, str]:
    """用户手动认下的合并关系 ``{叫法: 规范键}``；库还没建好时返回空表。"""
    _ensure_user_aliases()
    return dict(_USER_ALIASES)


def set_user_aliases(mapping: Mapping[str, str] | None) -> None:
    """换掉进程内的合并缓存；传 ``None`` 只作废缓存，下次用时会重新读库。"""
    global _USER_LOADED
    _USER_ALIASES.clear()
    if mapping is not None:
        _USER_ALIASES.update(
            {_clean(k).lower(): _clean(v) for k, v in mapping.items() if _clean(k) and _clean(v)}
        )
        _USER_LOADED = True
    else:
        _USER_LOADED = False


def _ensure_user_aliases() -> None:
    """第一次用时懒读一次库，之后走缓存；读不到就当没有合并关系。"""
    global _USER_LOADED
    if not _USER_LOADED:
        try:
            from job.models.application import ApplyAliasRow

            set_user_aliases(ApplyAliasRow.pairs())
        except Exception:  # noqa: BLE001 合并表是加分项，读不到就只按内置别名走
            _USER_LOADED = True


def _user_alias(text: str) -> str:
    """查用户合并表。"""
    _ensure_user_aliases()
    return _USER_ALIASES.get(text, "")

# 中文标签 → 规范键。用户在官网上手补的那一栏，标题多半就是内置字段的中文标签
# （「民族」→ nation、「专业类别」→ major_category），拿它归并比英文别名管用。
_LABEL_TO_KEY: dict[str, str] = {}


def _label_index() -> dict[str, str]:
    """内置字段的中文标签 / 键 → 规范键；建一次就缓存。

    延迟建表：knowledge 被 profile / models 引用，模块顶层再反向 import 会成环。
    建不出表（比如脱离项目单独 import）时返回空表，只按英文别名归并。
    """
    if _LABEL_TO_KEY:
        return _LABEL_TO_KEY
    try:
        from job.apply.profile import normalize_label
        from job.models.setting import ApplyProfileSettings

        for key, label in ApplyProfileSettings.LABELS.items():
            _LABEL_TO_KEY[normalize_label(label)] = key
            _LABEL_TO_KEY[normalize_label(key)] = key
    except Exception:  # noqa: BLE001 归并是加分项，建不出表不该拖垮调用方
        return _LABEL_TO_KEY
    return _LABEL_TO_KEY


def canonical_key(key: str) -> str:
    """把键归到规范写法（``ethnicity`` → ``nation``、「民族」→ ``nation``）。

    先查用户手动合并的那张表（用户说了算），再查英文别名，再查内置字段的中文标签；
    都认不出就原样返回（用户自定的新字段）。
    """
    text = _clean(key)
    if not text:
        return ""
    lowered = text.lower()
    # 用户合并进来的叫法可能本身就是个别名（「民族」→ nation），所以合并结果还要
    # 再过一遍内置表；只在真的归到了别的键时才继续，免得自己指自己转不出去。
    if merged := _user_alias(lowered):
        return _builtin_key(merged) or merged
    return _builtin_key(lowered) or text


def _builtin_key(lowered: str) -> str:
    """内置两张表里查一遍（英文别名 → 中文标签 / 键）；认不出返回空串。"""
    if hit := _ALIAS_TO_KEY.get(lowered):
        return hit
    try:
        from job.apply.profile import normalize_label

        text = normalize_label(lowered)
        # 别名表里也有中文叫法（「技能/兴趣爱好」），标点写法对不上时按归一化再查一遍
        if hit := _alias_index().get(text):
            return hit
        if hit := _label_index().get(text):
            return hit
        # 「体重(公斤)」「身高cm」这类尾巴上挂着单位的：去掉单位再认一次，
        # 免得同一个身高在库里存成「身高」和「身高(厘米)」两条
        if tail := _without_unit_tail(text):
            return _alias_index().get(tail) or _label_index().get(tail)
    except Exception:  # noqa: BLE001 归并是加分项，出岔子就按原键走
        return ""
    return ""


_ALIAS_INDEX: dict[str, str] = {}


def _alias_index() -> dict[str, str]:
    """别名 → 规范键，比对前先去空白与标点（「技能/兴趣爱好」=「技能兴趣爱好」）。"""
    if _ALIAS_INDEX:
        return _ALIAS_INDEX
    try:
        from job.apply.profile import normalize_label

        for key, aliases in KEY_ALIASES.items():
            _ALIAS_INDEX.setdefault(normalize_label(key), key)
            for alias in aliases:
                _ALIAS_INDEX.setdefault(normalize_label(alias), key)
    except Exception:  # noqa: BLE001 归并是加分项，建不出表不该拖垮调用方
        return _ALIAS_INDEX
    return _ALIAS_INDEX


def _without_unit_tail(text: str) -> str:
    """去掉尾巴上的度量单位（``体重公斤`` → ``体重``）；没挂单位返回空串。"""
    for word, _ in _UNIT_ALIASES:
        if len(word) < len(text) and text.endswith(word):
            return text[: -len(word)]
    return ""


def aliases_of(key: str) -> list[str]:
    """这个键认下的全部叫法（内置别名 + 用户手动合并进来的），给信息树第三层用。

    传进来的键先归到规范写法，这样 ``ethnicity`` 与 ``nation`` 查到的是同一串别名。
    顺序固定：内置别名在前，用户后来认下的在后，界面展示才不会每次都不一样。
    """
    target = canonical_key(key)
    if not target:
        return []
    out: list[str] = list(KEY_ALIASES.get(target, ()))
    for alias, mapped in user_aliases().items():
        if alias in out:
            continue
        if (canonical_key(mapped) or mapped) == target:
            out.append(alias)
    return out


def orphan_keys(keys: Iterable[str]) -> list[str]:
    """这批键里，内置表认不出、也没被用户合并过的那些（信息树上「待归并」用）。

    认得出（归到了别的写法）或本身就是内置规范键的都算已归并；只有真正落单的
    才返回，界面上提示用户「这条和库里哪条是同一条」。
    """
    from job.models.setting import ApplyProfileSettings

    known = set(ApplyProfileSettings.LABELS)
    out: list[str] = []
    for raw in keys:
        text = _clean(raw)
        if not text or text in known or text in out:
            continue
        if _builtin_key(text.lower()) or _user_alias(text.lower()):
            continue
        out.append(text)
    return out


# ---------------------------------------------------------------------------
# 变形：按表单提示把身高 / 体重改成这一栏要的样子
# ---------------------------------------------------------------------------

# 这两个键是「带度量单位的数值」，其它键一律原样照抄
MEASURE_KEYS: dict[str, str] = {"height": "cm", "weight": "kg"}

# 单位别名 → 规范写法；长的排前面，「厘米」别被「米」抢走
_UNIT_ALIASES: tuple[tuple[str, str], ...] = (
    ("厘米", "cm"),
    ("公分", "cm"),
    ("毫米", "mm"),
    ("千克", "kg"),
    ("公斤", "kg"),
    ("cm", "cm"),
    ("mm", "mm"),
    ("kg", "kg"),
    ("米", "m"),
    ("克", "g"),
    ("斤", "jin"),
    ("m", "m"),
    ("g", "g"),
)

# 相对基准单位的倍数（长度按 cm 算，重量按 kg 算）
_UNIT_FACTOR: dict[str, float] = {
    "cm": 1.0,
    "mm": 0.1,
    "m": 100.0,
    "kg": 1.0,
    "g": 0.001,
    "jin": 0.5,
}
_LENGTH_UNITS = frozenset({"cm", "mm", "m"})
_MASS_UNITS = frozenset({"kg", "g", "jin"})

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# 提示文字里的英文单位：按词匹配，别把 form / mail 里的 m 当单位
_ASCII_UNIT = re.compile(r"(?<![a-z])(cm|mm|kg|g|m)(?![a-z])", re.IGNORECASE)
# 英文单位 → 规范写法（``_ASCII_UNIT`` 只负责找出是哪几个字母，这里补上映射）
_ASCII_UNIT_UNITS: dict[str, str] = {"cm": "cm", "mm": "mm", "kg": "kg", "g": "g", "m": "m"}
# 「只让填数字」的 pattern：^\d+$ / \d{1,3} 这类
_NUMERIC_PATTERN = re.compile(r"^\^?\\d(?:\{\d+(?:,\d*)?\}|\+|\*)?\$?$")
_NUMERIC_WORDS = ("只能填数字", "仅限数字", "只允许数字", "请输入数字", "纯数字", "数字即可")
# 占位符把单位写进了示例值（``如 175cm`` / ``例：1.75米``）：这一栏要连单位一起写。
# 英文单位后面不许再跟字母或数字，免得把 ``175gm`` 这种串里的 ``g`` 当成单位。
_VALUE_HINT_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(厘米|公分|毫米|千克|公斤|斤|米|克|cm|mm|kg|m|g)(?![a-z0-9])",
    re.IGNORECASE,
)


def _unit_wanted_in_value(placeholder: str) -> str:
    """占位符的示例值里带着单位时，返回要一起写进去的那串单位写法（``cm`` / ``厘米``）。"""
    match = _VALUE_HINT_UNIT.search(_clean(placeholder))
    return match.group(2) if match else ""


def _clean(value: object) -> str:
    return str(value or "").strip()


def _parse_measure(value: str) -> tuple[float, str]:
    """把「1.75 米」「175cm」「175」读成 ``(数值, 规范单位)``；读不出数值返回 ``(0, "")``。"""
    text = _clean(value).lower()
    if not text:
        return 0.0, ""
    match = _NUMBER.search(text)
    if not match:
        return 0.0, ""
    unit = ""
    for word, canonical in _UNIT_ALIASES:
        if word in text:
            unit = canonical
            break
    return float(match.group(0)), unit


def unit_in_text(text: str) -> str:
    """提示文字里写的单位（``身高(cm)`` → ``cm``）；没写返回空串。"""
    value = _clean(text)
    if not value:
        return ""
    # 中文单位先认（「厘米」别被「米」抢走，靠 _UNIT_ALIASES 里「长词在前」的顺序）。
    # 单字母英文单位不在这里做子串匹配，否则 form / mail 里的 m 会被当成米。
    for word, canonical in _UNIT_ALIASES:
        if not word.isascii() and word in value:
            return canonical
    match = _ASCII_UNIT.search(value.lower())
    return _ASCII_UNIT_UNITS.get(match.group(1), "") if match else ""


def _family(unit: str) -> frozenset[str]:
    return _LENGTH_UNITS if unit in _LENGTH_UNITS else _MASS_UNITS


def _numeric_only(*, field_type: str, pattern: str, inputmode: str, placeholder: str, label: str) -> bool:
    """这一栏是不是「只让填数字」（原生 number、inputmode、pattern、提示语都算证据）。"""
    if field_type == "number":
        return True
    if inputmode.strip().lower() in ("numeric", "decimal"):
        return True
    if _NUMERIC_PATTERN.match(pattern.replace(" ", "")):
        return True
    text = f"{placeholder} {label}"
    return any(word in text for word in _NUMERIC_WORDS)


def _looks_meters(hints: Mapping[str, str]) -> bool:
    """``min`` / ``max`` 落在 1~3 之间，说明这一栏是按「米」收的。"""
    try:
        low = float(_clean(hints.get("min")) or 0)
        high = float(_clean(hints.get("max")) or 0)
    except ValueError:
        return False
    return 0 < high <= 3 and low >= 1


def _trim(number: float) -> str:
    """``175.0`` → ``175``，``1.7500`` → ``1.75``。"""
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def adapt(
    key: str,
    value: str,
    *,
    label: str = "",
    placeholder: str = "",
    field_type: str = "",
    hints: Mapping[str, str] | None = None,
) -> str:
    """按表单提示把值改成这一栏要的样子；看不出提示就原样返回。

    只处理身高 / 体重这类「带单位的数值」。表单自己标了单位（``身高(cm)``）或明确
    只要数字时，就填纯数字；``min`` / ``max`` 落在 1~3 之间说明这栏按「米」收，
    也照它改。看不出提示就原样返回——没依据的改写等于编造。
    """
    base = MEASURE_KEYS.get(key)
    if not base:
        return value
    amount, unit = _parse_measure(value)
    if amount <= 0:
        return value
    data = hints or {}
    hint_unit = unit_in_text(" ".join((label, placeholder, _clean(data.get("unit")))))
    numeric = _numeric_only(
        field_type=field_type,
        pattern=_clean(data.get("pattern")),
        inputmode=_clean(data.get("inputmode")),
        placeholder=placeholder,
        label=label,
    )
    # 「这栏是按米收的」：min / max 都落在 1~3 之间（身高 1.75 米那个量级）
    meters = base == "cm" and _looks_meters(data)
    if not hint_unit and not numeric and not meters:
        # 什么提示都没有：不动用户写的那一份
        return value
    if not unit:
        # 没写单位时按常识补：身高写 1.75 的是米，写 175 的是厘米
        unit = "m" if base == "cm" and amount < 3 else base
    if unit not in _UNIT_FACTOR or _family(unit) != _family(base):
        return value
    in_base = amount * _UNIT_FACTOR[unit] / _UNIT_FACTOR[base]
    if hint_unit and _family(hint_unit) == _family(base):
        # 表单自己标了单位，听它的（比 min/max 猜出来的准）
        target = hint_unit
    elif meters:
        target = "m"
    else:
        target = base
    text = _trim(in_base * _UNIT_FACTOR[base] / _UNIT_FACTOR[target])
    # 占位符写着「如 175cm」这种带单位的示例：这一栏要连单位一起写，光丢个数字进去
    # 官网会判格式不对。只有确实要填数字（number / 纯数字提示）时才不跟单位。
    literal = _unit_wanted_in_value(placeholder)
    if literal and not numeric and unit_in_text(literal) == target:
        return f"{text}{literal}"
    return text
