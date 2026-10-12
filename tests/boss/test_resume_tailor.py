"""定制简历的幻觉拦截校验：母版之外的事实、丢失的机构条目、说明性文字。"""

from __future__ import annotations

from job.boss.resume_tailor import validate

MASTER = """张三
广州 · 13800000000 · zhangsan@example.com
教育经历
中山大学 | 计算机科学与技术 | 2017年9月-2021年6月
工作经历
腾讯科技有限公司 | 后端开发 | 2021年7月-至今
- 用 Python 把下单接口的 P99 从 800ms 降到 300ms，支撑日订单量 50 万
项目经历
推荐系统重构 | 2022年3月-2022年12月
- 引入 Redis 缓存，命中率 92%，机器成本降低 30%
"""


def test_passes_faithful_rewrite():
    draft = (
        "张三\n"
        "广州 · 13800000000 · zhangsan@example.com\n"
        "技术亮点\n"
        "- 用 Python 把下单接口的 P99 从 800ms 降到 300ms（腾讯科技有限公司）\n"
        "工作经历\n"
        "腾讯科技有限公司 | 后端开发 | 2021年7月-至今\n"
        "- 支撑日订单量 50 万的下单服务\n"
        "项目经历\n"
        "推荐系统重构 | 2022年3月-2022年12月\n"
        "- 引入 Redis 缓存，命中率 92%\n"
        "教育经历\n"
        "中山大学 | 计算机科学与技术 | 2017年9月-2021年6月\n"
    )
    assert validate(MASTER, draft) == []


def test_flags_fabricated_numbers():
    draft = MASTER.replace("50 万", "500 万")
    issues = validate(MASTER, draft)
    assert any("母版里没有的数字或事实" in i for i in issues)


def test_flags_fabricated_year():
    draft = MASTER + "\n2024年8月 获得 PMP 认证"
    issues = validate(MASTER, draft)
    assert any("母版里没有的数字或事实" in i for i in issues)


def test_flags_lost_institution():
    draft = MASTER.replace("腾讯科技有限公司 | 后端开发 | 2021年7月-至今", "某大厂 | 后端开发")
    issues = validate(MASTER, draft)
    assert any("机构条目" in i and "腾讯" in i for i in issues)


def test_flags_artifact_phrase():
    draft = "岗位匹配亮点：Python 经验丰富\n" + MASTER
    issues = validate(MASTER, draft)
    assert any("解释性文字" in i for i in issues)


def test_flags_placeholder():
    draft = MASTER.replace("张三", "XXX公司 张三")
    issues = validate(MASTER, draft)
    assert any("占位符" in i for i in issues)


def test_flags_truncated_tail():
    draft = MASTER + "\n- 与平台组共建了实时数据同步链路和"
    issues = validate(MASTER, draft)
    assert any("截断" in i for i in issues)


def test_flags_shrunk_draft():
    draft = "张三\n中山大学\n腾讯科技有限公司"
    issues = validate(MASTER, draft)
    assert any("偏短" in i for i in issues)


def test_empty_draft():
    assert validate(MASTER, "") == ["定制结果为空"]
