"""P5 内容输出域 MCP 服务：报告生成 + 循证知识库。

合并自 M9 (a207-report-mcp) + M12 (a207-knowledge-mcp)。
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from a207_policy import CallerError

from .core import (
    generate_patient_report,
    get_citation_tool,
    search_guideline_tool,
    search_sop_tool,
)

mcp = FastMCP("CKDNutri-content-mcp")


def _invalid(exc):
    if isinstance(exc, CallerError):
        raise
    return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}


def main():
    mcp.run()


# ---- M12: 循证知识库 ----

@mcp.tool
def search_guideline(keyword: str) -> dict[str, Any]:
    """语义检索指南（按角色视图裁剪：临床=专业语料，家庭=通俗语料）。"""
    try:
        return search_guideline_tool(keyword)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def search_sop(keyword: str) -> dict[str, Any]:
    """检索临床 SOP。"""
    try:
        return search_sop_tool(keyword)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_citation(entry_id: str) -> dict[str, Any]:
    """返回指定指南条目的完整出处。"""
    try:
        return get_citation_tool(entry_id)
    except Exception as exc:
        return _invalid(exc)


# ---- M9: 报告生成 ----

@mcp.tool
def generate_report(patient_id: str) -> dict[str, Any]:
    """生成结构化营养报告（按身份视图裁剪）。"""
    try:
        return generate_patient_report(patient_id)
    except Exception as exc:
        return _invalid(exc)


if __name__ == "__main__":
    main()
