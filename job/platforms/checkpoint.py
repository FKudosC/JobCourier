"""断点续采：进度按平台分区存进设置表，互不覆盖。

看完一个城市记一笔，中途停止或登录失效后重开时自动跳过已完成的城市；
改了搜索条件（城市序列变化）进度自动作废。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from job.models.setting import SettingRow

_KEY_PREFIX = "scrape_checkpoint"


def key_of(platform: str) -> str:
    """某平台的断点在设置表里的键。"""
    return f"{_KEY_PREFIX}:{platform}"


def load(platform: str, sig: str) -> set[str]:
    """读出与本次搜索匹配的已完成城市；没有进度或条件变了就从头开始。"""
    key = key_of(platform)
    raw = SettingRow.get_many((key,)).get(key, "")
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, dict) or data.get("sig") != sig:
        return set()
    done = data.get("done")
    return {c for c in done if isinstance(c, str)} if isinstance(done, list) else set()


def save(platform: str, sig: str, entries: list[str], done: set[str]) -> None:
    """记录已完成的进度条目（如「关键词@城市」）；未知条目不记，避免残留垃圾数据。"""
    SettingRow.put_many(
        {
            key_of(platform): json.dumps(
                {
                    "sig": sig,
                    "done": [e for e in entries if e in done],
                    "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            )
        }
    )


def clear(platform: str) -> None:
    """全部城市看完，清除断点。"""
    SettingRow.put_many({key_of(platform): ""})


def sig_of(targets: list[tuple[str, str]], queries: list[str] | None = None) -> str:
    """本次投递的标识：关键词序列 + 城市序列；任一变化进度自动作废。"""
    city_part = "|".join(city for city, _ in targets)
    if queries:
        return "|".join(queries) + "@" + city_part
    return city_part
