"""全局设置：键值表 + 大模型配置 + 自动回复配置。"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider
from sqlmodel import Field, SQLModel

from job.boss.filters import Defaults, ReplyPaceProfile


class SettingRow(SQLModel, table=True):
    """全局键值配置（与求职配置无关的设置）。"""

    __tablename__ = "app_setting"  # pyright: ignore[reportAssignmentType]

    key: str = Field(primary_key=True)
    value: str = ""

    @classmethod
    def get_many(cls, keys: tuple[str, ...]) -> dict[str, str]:
        """批量读取；没存过的键不出现在结果里。"""
        from job.models import db_session

        with db_session() as session:
            return {k: row.value for k in keys if (row := session.get(cls, k))}

    @classmethod
    def put_many(cls, values: dict[str, str]) -> None:
        """批量写入（有则覆盖）。"""
        from job.models import db_session

        with db_session() as session:
            for key, value in values.items():
                session.merge(cls(key=key, value=value))
            session.commit()


class KeyValueSettings(BaseModel):
    """存在设置表里的一组配置：每个字段存为 ``<PREFIX><字段名>`` 一行。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    # 设置表里的键前缀，子类必须覆盖
    PREFIX: ClassVar[str] = ""

    @classmethod
    def load(cls) -> Self:
        """读取已保存的字段；非字符串字段存的是 JSON，解析失败的按默认值。"""
        keys = tuple(f"{cls.PREFIX}{f}" for f in cls.model_fields)
        data: dict[str, Any] = {}
        for key, raw in SettingRow.get_many(keys).items():
            name = key.removeprefix(cls.PREFIX)
            if cls.model_fields[name].annotation is str:
                data[name] = raw
                continue
            try:
                data[name] = json.loads(raw)
            except json.JSONDecodeError:
                continue
        return cls(**data)

    def save(self) -> None:
        data = {
            f"{self.PREFIX}{k}": v if isinstance(v, str) else json.dumps(v)
            for k, v in self.model_dump().items()
        }
        SettingRow.put_many(data)


class AutoReplySettings(KeyValueSettings):
    """自动回复配置：是否启用与回复提示词；需已开通 AI 服务并配置简历。"""

    PREFIX: ClassVar[str] = "reply_"
    DEFAULT_PROMPT: ClassVar[str] = (
        "你是正在求职的我，在 BOSS 直聘上和 HR 沟通。请根据我的简历回复 HR 的最新消息：\n"
        "1. 语气礼貌、真诚、简洁，每次回复不超过 100 字\n"
        "2. 只根据简历里的真实经历回答，不编造经历和技能\n"
        "3. HR 索要简历时简短回应即可，程序会自动发送附件简历\n"
        "4. 薪资、到岗时间等简历里没有的信息，委婉表示可以进一步沟通\n"
        "5. 岗位明显不合适时，礼貌说明并感谢对方"
    )

    enabled: bool = False
    prompt: str = DEFAULT_PROMPT
    # 回复节奏档位（slow / normal / fast / custom），明细见 ``ReplyPaceProfile``
    pace: str = Defaults.PACE
    # 「自定义」档的明细参数
    pace_params: dict[str, float] = {}
    # 发送前人工确认：AI 生成的回复先进「待确认回复」，在消息页同意后才发出
    confirm_before_send: bool = False
    # 跟进提醒：我发出消息后 HR 超过这么多小时没回，消息页提示跟进；0 表示不提醒
    follow_up_hours: int = 48
    # 自动拉黑主动联系的 HR：岗位不在投递库中的会话直接拉黑并禁止回复
    block_proactive_hr: bool = True

    @property
    def pace_profile(self) -> ReplyPaceProfile:
        """当前档位对应的回复节奏。"""
        return ReplyPaceProfile.from_saved(self.pace, self.pace_params)


class LlmSettings(KeyValueSettings):
    """大模型配置：接口地址、API Key，再拉取可用模型并选择；存在设置表里。

    接口地址为空时用 DeepSeek 官方服务，填写后按 OpenAI 兼容接口调用。
    """

    PREFIX: ClassVar[str] = "llm_"
    # 默认服务（接口地址留空时）
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.deepseek.com"
    # 连通性探针的提示词：越小越快，不依赖任何结构化输出能力
    PING_PROMPT: ClassVar[str] = "这是一次连通性测试。请只回复两个字：成功"

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    # 上次拉到的可用模型
    models: list[str] = []

    @property
    def ready(self) -> bool:
        """Key 和模型都已配置。"""
        return bool(self.api_key and self.model)

    @property
    def provider(self) -> DeepSeekProvider | OpenAIProvider:
        """模型服务：接口地址为空用 DeepSeek，否则用 OpenAI 兼容接口。"""
        if not self.base_url:
            return DeepSeekProvider(api_key=self.api_key)
        return OpenAIProvider(base_url=self.base_url, api_key=self.api_key)

    async def fetch_models(self) -> list[str]:
        """拉取当前服务的可用模型；失败时抛 ``openai.APIError``。"""
        client = self.provider.client
        return sorted([m.id async for m in client.models.list()])

    async def ping(self) -> str:
        """最小连通性探测：真实调用一次模型要一句回话。

        不走结构化输出——「测试连接」只该验证地址、Key 和模型可用，
        模型会不会写 JSON 是另一回事（评分流程有自己的解析与重试）。
        网络、鉴权错误原样抛出（``openai.APIError`` 家族）。
        """
        client = self.provider.client
        response = await client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": self.PING_PROMPT}],
            max_tokens=512,
            temperature=0,
        )
        return (response.choices[0].message.content or "").strip()


class CaptchaSettings(KeyValueSettings):
    """验证码识别：把官网的图形验证码交给多模态模型读出来自动填写。

    识图复用「大模型服务」里的接口地址与 API Key，模型可以另选一个支持视觉的
    （DeepSeek 这类纯文本模型读不了图）；留空则用大模型服务里选中的模型。
    """

    PREFIX: ClassVar[str] = "captcha_"

    enabled: bool = False
    # 识图模型；留空用大模型服务里选的模型
    model: str = ""


class TelemetrySettings(KeyValueSettings):
    """链路追踪配置：是否记录 LLM 调用、记到哪里、要不要带上原文；默认全关。

    ``exporter`` 取 ``console`` 打到终端便于本地排查，或 ``otlp`` 发到采集端。
    ``content`` 会把简历、岗位详情等提示词原文写进 span，涉及个人隐私，默认关闭。
    """

    PREFIX: ClassVar[str] = "otel_"

    enabled: bool = False
    exporter: str = "console"
    # OTLP 采集端地址，如 http://127.0.0.1:4318/v1/traces
    endpoint: str = ""
    content: bool = False


class TailorSettings(KeyValueSettings):
    """定制简历自动化：AI 评分达到分数线的岗位，后台自动生成定制简历。"""

    PREFIX: ClassVar[str] = "tailor_"

    enabled: bool = True
    # 自动生成的最低匹配分（0–100）
    min_score: int = 80


class BrowserSettings(KeyValueSettings):
    """浏览器运行方式：后台静默模式下窗口移出屏幕，不弹出不打扰。"""

    PREFIX: ClassVar[str] = "browser_"

    background: bool = False


class ApplyProfileSettings(KeyValueSettings):
    """网申资料：企业官网表单要填的个人信息，只存在本机数据库里。

    官网表单问的东西远比 BOSS 多（学校、专业、到岗时间、期望薪资……），
    单独存一份结构化资料，比每次从简历正文里现抠准确得多；简历正文只作为兜底。
    """

    PREFIX: ClassVar[str] = "apply_"

    # 分组 → （字段键, 界面标签）；顺序即界面顺序。LABELS 由它派生，二者永远一致。
    # 官网网申表比 BOSS 啰嗦得多：民族、政治面貌、身份证号、紧急联系人……
    # 这些栏位在简历正文里基本不成对出现，只能靠结构化资料兜住。
    GROUPS: ClassVar[tuple[tuple[str, tuple[tuple[str, str], ...]], ...]] = (
        (
            "基本信息",
            (
                ("name", "姓名"),
                ("name_pinyin", "姓名拼音"),
                ("english_name", "英文名"),
                ("former_name", "曾用名"),
                ("gender", "性别"),
                ("birth", "出生年月"),
                ("birth_place", "出生地"),
                ("nation", "民族"),
                ("nationality", "国籍"),
                ("political", "政治面貌"),
                ("party_time", "入党 / 入团时间"),
                ("marital", "婚姻状况"),
                ("id_type", "证件类型"),
                ("id_card", "身份证号"),
                ("id_expiry", "身份证有效期"),
                ("hukou", "户籍所在地"),
                ("hukou_type", "户口性质"),
                ("native_place", "籍贯"),
                ("origin_place", "生源地"),
                ("height", "身高"),
                ("weight", "体重"),
                ("blood_type", "血型"),
                ("health", "健康状况"),
                ("religion", "宗教信仰"),
                ("school_hukou", "现户口是否为学校户口"),
            ),
        ),
        (
            "联系方式",
            (
                ("phone", "手机号"),
                ("email", "邮箱"),
                ("wechat", "微信号"),
                ("qq", "QQ"),
                ("city", "现居城市"),
                ("address", "通讯地址"),
                ("postal", "邮编"),
                ("current_location", "目前所在地"),
            ),
        ),
        (
            "教育背景",
            (
                ("school", "毕业学校"),
                ("school_type", "学校类型"),
                ("college_name", "学院 / 院系"),
                ("major", "专业"),
                ("major_category", "专业类别"),
                ("discipline", "学科门类"),
                ("major_direction", "专业方向"),
                ("degree", "学历"),
                ("degree_name", "学位"),
                ("degree_full", "学位名称"),
                ("study_mode", "学习形式"),
                ("train_mode", "培养方式"),
                ("unified_enrollment", "是否统招"),
                ("graduate_year", "毕业年份"),
                ("start_year", "入学时间"),
                ("schooling_length", "学制"),
                ("gpa", "GPA / 绩点"),
                ("rank", "专业排名"),
                ("cet", "英语等级"),
                ("mandarin", "普通话等级"),
                ("computer_level", "计算机等级"),
                ("second_major", "辅修 / 双学位"),
                ("courses", "主修课程"),
                ("thesis", "毕业论文"),
                ("advisor", "指导教师"),
                ("awards", "获奖情况"),
                ("scholarship", "奖学金"),
                ("grant", "助学金"),
                ("school_position", "在校职务"),
                ("student_union", "学生会 / 团委"),
                ("student_id", "学号"),
                ("class_name", "班级"),
            ),
        ),
        (
            "求职意向",
            (
                ("expected_salary", "期望薪资"),
                ("available", "到岗时间"),
                ("expected_city", "期望城市"),
                ("expected_position", "期望职位"),
                ("expected_industry", "期望行业"),
                ("job_type", "求职类型"),
                ("current_status", "求职状态"),
                ("accept_travel", "出差意愿"),
                ("accept_relocate", "外派 / 调剂"),
                ("internship_duration", "实习时长"),
                ("internship_days", "每周实习天数"),
                ("accept_adjust", "是否同意调剂到其他岗位"),
            ),
        ),
        (
            "经历与能力",
            (
                ("work_years", "工作年限"),
                ("current_company", "目前公司"),
                ("current_position", "目前职位"),
                ("leave_reason", "离职原因"),
                ("work_experience", "工作经历"),
                ("internship", "实习经历"),
                ("projects", "项目经历"),
                ("research", "科研经历"),
                ("competition", "竞赛经历"),
                ("volunteer", "志愿服务"),
                ("social_practice", "社会实践"),
                ("part_time", "兼职经历"),
                ("exchange", "交换 / 留学经历"),
                ("club", "社团经历"),
                ("training", "培训经历"),
                ("skills", "专业技能"),
                ("strengths", "个人优势"),
                ("language_skill", "语言能力"),
                ("certificates", "证书"),
                ("driver_license", "驾驶证"),
                ("publications", "论文发表"),
                ("patent", "专利"),
                ("software_copyright", "软件著作权"),
            ),
        ),
        (
            "紧急联系人",
            (
                ("emergency_name", "紧急联系人姓名"),
                ("emergency_relation", "与本人关系"),
                ("emergency_phone", "紧急联系人电话"),
                ("family_member", "家庭成员"),
            ),
        ),
        (
            "其他信息",
            (
                ("referral_code", "内推码"),
                ("source_channel", "信息来源"),
                ("is_fresh_graduate", "是否应届"),
                ("relative_in_company", "亲属任职"),
                ("noncompete", "竞业限制"),
                ("archive_location", "档案所在地"),
                ("hobby", "兴趣爱好"),
                ("self_intro", "自我介绍"),
                ("career_plan", "职业规划"),
                ("github", "GitHub / 作品集"),
                ("portfolio", "个人主页"),
                ("linkedin", "领英"),
                ("dingtalk", "钉钉"),
                ("has_children", "有无子女"),
                ("person_type", "人员类型（校招）"),
            ),
        ),
    )

    # 字段 → 界面标签（顺序即界面顺序）
    LABELS: ClassVar[dict[str, str]] = {k: label for _, fields in GROUPS for k, label in fields}

    # 敏感字段：只在本机使用，绝不进大模型的提示词（身份证、银行卡这类）。
    # 资料全程在本机处理，界面上明文展示、方便核对，不再掩码。
    PRIVATE: ClassVar[frozenset[str]] = frozenset(
        {"id_card", "id_expiry"}
    )

    name: str = ""
    name_pinyin: str = ""
    english_name: str = ""
    former_name: str = ""
    gender: str = ""
    birth: str = ""
    birth_place: str = ""
    nation: str = ""
    nationality: str = ""
    political: str = ""
    party_time: str = ""
    marital: str = ""
    id_type: str = ""
    id_card: str = ""
    id_expiry: str = ""
    hukou: str = ""
    hukou_type: str = ""
    native_place: str = ""
    origin_place: str = ""
    height: str = ""
    weight: str = ""
    blood_type: str = ""
    health: str = ""
    religion: str = ""
    school_hukou: str = ""
    phone: str = ""
    email: str = ""
    wechat: str = ""
    qq: str = ""
    city: str = ""
    address: str = ""
    postal: str = ""
    current_location: str = ""
    school: str = ""
    school_type: str = ""
    college_name: str = ""
    major: str = ""
    major_category: str = ""
    discipline: str = ""
    major_direction: str = ""
    degree: str = ""
    degree_name: str = ""
    degree_full: str = ""
    study_mode: str = ""
    train_mode: str = ""
    unified_enrollment: str = ""
    graduate_year: str = ""
    start_year: str = ""
    schooling_length: str = ""
    gpa: str = ""
    rank: str = ""
    cet: str = ""
    mandarin: str = ""
    computer_level: str = ""
    second_major: str = ""
    courses: str = ""
    thesis: str = ""
    advisor: str = ""
    awards: str = ""
    scholarship: str = ""
    grant: str = ""
    school_position: str = ""
    student_union: str = ""
    student_id: str = ""
    class_name: str = ""
    expected_salary: str = ""
    available: str = ""
    expected_city: str = ""
    expected_position: str = ""
    expected_industry: str = ""
    job_type: str = ""
    current_status: str = ""
    accept_travel: str = ""
    accept_relocate: str = ""
    internship_duration: str = ""
    internship_days: str = ""
    accept_adjust: str = ""
    work_years: str = ""
    current_company: str = ""
    current_position: str = ""
    leave_reason: str = ""
    work_experience: str = ""
    internship: str = ""
    projects: str = ""
    research: str = ""
    competition: str = ""
    volunteer: str = ""
    social_practice: str = ""
    part_time: str = ""
    exchange: str = ""
    club: str = ""
    training: str = ""
    skills: str = ""
    strengths: str = ""
    language_skill: str = ""
    certificates: str = ""
    driver_license: str = ""
    publications: str = ""
    patent: str = ""
    software_copyright: str = ""
    emergency_name: str = ""
    emergency_relation: str = ""
    emergency_phone: str = ""
    family_member: str = ""
    referral_code: str = ""
    source_channel: str = ""
    is_fresh_graduate: str = ""
    relative_in_company: str = ""
    noncompete: str = ""
    archive_location: str = ""
    hobby: str = ""
    github: str = ""
    portfolio: str = ""
    self_intro: str = ""
    career_plan: str = ""
    linkedin: str = ""
    dingtalk: str = ""
    has_children: str = ""
    person_type: str = ""

    def filled(self) -> dict[str, str]:
        """有值的字段，键为字段名。"""
        data = {k: v.strip() for k, v in self.model_dump().items() if isinstance(v, str)}
        return {k: v for k, v in data.items() if v}

    @property
    def ready(self) -> bool:
        """至少有姓名和联系方式，才够填一张网申表。"""
        return bool(self.name.strip() and (self.phone.strip() or self.email.strip()))


class ApplyLocateSettings(KeyValueSettings):
    """表单定位兜底：认不出的栏位截一张小图交给多模态模型指认；默认关闭。

    截图会离开本机、发到用户自己配置的模型服务，画面里可能有个人资料，所以
    必须由用户主动打开，并且限个数、限时长——兜底是加分项，不该变成常态。
    """

    PREFIX: ClassVar[str] = "locate_"

    enabled: bool = False
    # 一次扫描最多处理几个栏位
    max_fields: int = 3
    # 每个栏位的超时（秒）
    timeout: int = 15


class ApplyVisionSettings(KeyValueSettings):
    """表单视觉核对：把扫出来的栏位画在整页截图上，请多模态模型核对多没多、少没少。

    各家官网的表单结构千差万别，光靠 DOM 启发式扫出来的栏位清单总会有出入：
    表格型表单把标签放在隔壁格、组件库的假下拉没有可读文本、上传组件在真控件
    旁边放一个只读影子框……这些都会让网申面板上的清单和浏览器里实际要填的对不上。

    这里换一个思路：不再让模型从零读表单（必漏项，还贵），而是让它做**核对**——
    DOM 已经扫出一份带编号的候选清单，截图上也照这份清单画好了框，模型只需指出
    「哪几个框是多余的」「哪里还有一栏没框到」。

    截图会离开本机，所以默认给已带值的敏感栏（身份证、银行卡……）打码；
    ``mask_sensitive`` 关掉之前请想清楚。填完表之后一律不再截图——那时页面上
    全是用户的真实资料。
    """

    PREFIX: ClassVar[str] = "vision_"

    enabled: bool = True
    # 识图模型；留空用大模型服务里选中的模型
    model: str = ""
    # 一次核对的总超时（秒）
    timeout: int = 40
    # 长表单分片截图的上限：再多也只看前几屏，别把几十张图都甩给模型
    max_tiles: int = 4
    # 截图前把「已经带着值的敏感栏」涂黑
    mask_sensitive: bool = True


class ApplyAutoSettings(KeyValueSettings):
    """网申填表：文件代传与学习开关，最后一步提交永远留给人。

    这里唯一的「人工围栏」是企业官网的**提交按钮**——程序只填不提交，填完停住，
    提交那一下永远由用户在浏览器里点。

    ``handoff`` 决定填完之后怎么把缺项交回给人：

    * ``loose``：只有「必填栏还空着」才算没干完；
    * ``balanced``（默认）：必填栏空着算缺口，AI 兜底填上的标黄请人复核；
    * ``strict``：任何一栏没填上都算。
    """

    PREFIX: ClassVar[str] = "auto_"

    # 交付口径：loose / balanced / strict
    handoff: str = "balanced"
    # 填表时把「简历配置」里传过的简历原件 / 寸照一并传上去；关掉就留给人自己选文件
    upload_files: bool = True
    # 你在官网页面里手填的、或对照表上手改的那几条，回读之后记进本机资料库，
    # 换个官网再遇到同类栏目直接有值可填；关掉就只填不学，库里一条都不多。
    # 学进去的条目先过一遍本机规则，再交给模型判有效性并归并同义叫法。
    learn_web: bool = True
    # 模型按已有资料推出来的那些栏，点「填入浏览器」认可之后就存进本机资料库，
    # 下次在别家官网再遇到直接有值可填；关掉就只填不学，库里一条都不多。
    learn_ai: bool = True
