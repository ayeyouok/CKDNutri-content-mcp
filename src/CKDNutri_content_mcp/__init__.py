"""P5 内容输出域——报告生成+循证知识库。"""
from __future__ import annotations

from importlib import metadata as _metadata


def _pkg_version() -> str:
    """从安装元数据读取版本（P2-6：与 pyproject.toml 单一事实源对齐）。未安装时回退 "0.0.0"。"""
    try:
        return _metadata.version("CKDNutri-content-mcp")
    except _metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = _pkg_version()

__all__ = ["__version__"]
