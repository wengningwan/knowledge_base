"""结构化解析结果与不含二进制的持久化投影。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class ParsedDocument:
    """保留正文、区域证据和人工复核所需的解析结果。"""

    markdown: str = ""
    blocks: list[dict] = field(default_factory=list)
    figures: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    assets: dict[str, dict] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def sidecar(self) -> dict:
        """返回可序列化的结构投影，排除资产字节和凭据字段。"""
        return json_safe(
            {
                "schema_version": 1,
                "blocks": self.blocks,
                "figures": self.figures,
                "reviews": self.reviews,
                "diagnostics": self.diagnostics,
                "metadata": safe_metadata(self.metadata),
            }
        )


def safe_metadata(value):
    """删除元数据中的认证字段，避免凭据进入持久化结果。"""
    blocked = {"api_key", "apikey", "authorization", "password", "secret", "token", "credentials"}
    if isinstance(value, list):
        return [safe_metadata(item) for item in value]
    if not isinstance(value, dict):
        return json_safe(value)
    result = {}
    for key, item in value.items():
        if any(part in str(key).lower() for part in blocked):
            continue
        result[str(key)] = safe_metadata(item)
    return result


def json_safe(value):
    """递归保留 JSON 原生值，不把二进制或任意对象转成字符串。"""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items() if not isinstance(item, bytes)}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value if not isinstance(item, bytes)]
    return None
