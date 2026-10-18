"""本地图片文字识别：RapidOCR 懒加载单例。

简历（图片格式）与验证码兜底都要读图，共用同一个引擎——建一次要一两秒、
几百兆内存，各建各的纯属浪费。

``rapidocr-onnxruntime`` 是可选依赖：没装时 :func:`engine` 抛 ``ValueError``，
调用方按「本地读不出来」处理，别把整条流程拖垮。
"""

from __future__ import annotations

from typing import Any

# 引擎懒加载缓存（首次调用约 1-2 秒加载模型）
_engine: Any = None

# 一行识别结果：文字、置信度、上边距、左边距
Line = tuple[str, float, float, float]


def engine() -> Any:
    """取本地 OCR 引擎；没装这个可选依赖就抛 ``ValueError``。"""
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:  # pragma: no cover - 依赖已随包安装
            raise ValueError(
                "图片识别组件未安装：请在项目目录运行 uv add rapidocr-onnxruntime"
            ) from exc
        _engine = RapidOCR()
    return _engine


def lines(data: bytes) -> list[Line]:
    """识别一张图片，返回 ``(文字, 置信度, 上边距, 左边距)``；读不出返回空表。

    图片损坏、模型跑不起来都抛 ``ValueError``，让调用方自己决定是「提示用户」
    还是「悄悄跳过这一张」。
    """
    try:
        result, _elapsed = engine()(data)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 模型内部异常一律转成可读原因
        raise ValueError("图片无法识别，请确认是清晰的截图或照片") from exc
    out: list[Line] = []
    for row in result or []:
        if len(row) < 3 or not row[1]:
            continue
        box = row[0] or []
        top = min((float(point[1]) for point in box), default=0.0)
        left = min((float(point[0]) for point in box), default=0.0)
        out.append((str(row[1]), float(row[2] or 0.0), top, left))
    return out
