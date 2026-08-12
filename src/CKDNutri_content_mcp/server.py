"""P5 内容输出域 MCP 服务：报告生成 + 循证知识库。

合并自 M9 (a207-report-mcp) + M12 (a207-knowledge-mcp)。

v0.3.2 修复：search_guideline / search_sop / get_citation 此前工具名与导入的 core 函数
同名导致无限递归（RecursionError），改为经 core 模块调用；generate_report 补齐 7 个必需参数。
v0.3.9（BUG-16）修复：工具命名统一 _tool 后缀，与其他域包（*_tool）约定一致。
"""
from __future__ import annotations

from typing import Any, Optional

from fastmcp import FastMCP

from a207_policy import CallerError

from . import core as _core

mcp = FastMCP("CKDNutri-content-mcp")


def _invalid(exc):
    if isinstance(exc, CallerError):
        raise
    return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}


def main():
    mcp.run()


# ---- M12: 循证知识库 ----

@mcp.tool
def search_guideline_tool(keyword: str, guideline_set: Optional[str] = None) -> dict[str, Any]:
    """语义检索指南（按角色视图裁剪：临床=专业语料，家庭=通俗语料）。"""
    try:
        return _core.search_guideline(keyword, guideline_set)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def search_sop_tool(keyword: str) -> dict[str, Any]:
    """检索临床 SOP。"""
    try:
        return _core.search_sop(keyword)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_citation_tool(entry_id: str) -> dict[str, Any]:
    """返回指定指南条目的完整出处。"""
    try:
        return _core.get_citation(entry_id)
    except Exception as exc:
        return _invalid(exc)


# ---- M9: 报告生成 ----

@mcp.tool
def generate_report_tool(
    patient_id: str,
    demographics: dict,
    lab_summary: dict,
    nutrition_assessment: dict,
    followup_summary: dict,
    pew_history: Optional[list] = None,
    risk_level: str = "unknown",
) -> dict[str, Any]:
    """生成结构化患者报告（按身份视图裁剪：非临床角色脱敏）。

    demographics: {age_years, sex, ckd_stage, dialysis_mode}
    lab_summary: 来自 M2(LIS) 的最新化验摘要
    nutrition_assessment: 来自 M3(评估)：PRNT 目标 / 摄入达成率 / PEW
    followup_summary: 来自 M4(随访)：最近记录 / 计划 / 依从性
    pew_history: 来自 M3 get_pew_history（ADR-007）
    risk_level: 来自 M8(风险规则) 的等级（L0-L3 / low-high）
    """
    try:
        return _core.generate_patient_report(
            patient_id, demographics, lab_summary, nutrition_assessment,
            followup_summary, pew_history, risk_level,
        )
    except Exception as exc:
        return _invalid(exc)


if __name__ == "__main__":
    main()
