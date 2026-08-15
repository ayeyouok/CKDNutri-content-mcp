"""P5 内容输出域 MCP 服务：报告生成 + 循证知识库。

合并自 M9 (a207-report-mcp) + M12 (a207-knowledge-mcp)。

v0.3.2 修复：search_guideline / search_sop / get_citation 此前工具名与导入的 core 函数
同名导致无限递归（RecursionError），改为经 core 模块调用；generate_report 补齐 7 个必需参数。
v0.3.9（BUG-16）修复：工具命名统一 _tool 后缀，与其他域包（*_tool）约定一致。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastmcp import FastMCP

from a207_policy import translate_error

from . import core as _core

mcp = FastMCP("CKDNutri-content-mcp")

# B2（2026-08-12 五包审查）：异常分级归类统一到 care/assessment 口径——
# 未知系统异常（内部 Code Bug）归 INTERNAL_ERROR 且 detail 脱敏，完整堆栈仅服务端日志。
logger = logging.getLogger("CKDNutri-content-mcp")


def _invalid(exc):
    # B2 中心化（2026-08-15）：异常翻译收敛到 a207_policy.translate_error 单实现。
    # content 特例：ValueError 来自数据加载 fail-closed（服务端数据问题而非客户端
    # 入参）→ 归 INTERNAL_ERROR；KeyError 同（数据键缺失=数据问题）；客户端入参
    # 错误（InvalidArgumentError）归 INVALID_INPUT——语义与原 _invalid 一致。
    return translate_error(exc, domain="P5", logger=logger,
                           extra_invalid_types=(_core.InvalidArgumentError,),
                           extra_data_types=(KeyError,),
                           value_error_to_invalid=False)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")  # C2（2026-08-15）：生产 stdout 可采集
    mcp.run()


# ---- M12: 循证知识库 ----

@mcp.tool
def search_guideline_tool(keyword: str, guideline_set: Optional[str] = None,
                          limit: int = 20) -> dict[str, Any]:
    """语义检索指南（按角色视图裁剪：临床=专业语料，家庭=通俗语料）。
    limit（P2 修复 2026-08-13）：结果上限（默认 20、上限 100），防全量命中灌爆上下文。"""
    try:
        return _core.search_guideline(keyword, guideline_set, limit=limit)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def search_sop_tool(keyword: str, limit: int = 20) -> dict[str, Any]:
    """检索临床 SOP。limit（P2 修复）：结果上限（默认 20、上限 100）。"""
    try:
        return _core.search_sop(keyword, limit=limit)
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
