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

from a207_policy import CallerError

from . import core as _core

mcp = FastMCP("CKDNutri-content-mcp")

# B2（2026-08-12 五包审查）：异常分级归类统一到 care/assessment 口径——
# 未知系统异常（内部 Code Bug）归 INTERNAL_ERROR 且 detail 脱敏，完整堆栈仅服务端日志。
logger = logging.getLogger("CKDNutri-content-mcp")


def _invalid(exc):
    if isinstance(exc, CallerError):
        # BUG-54（2026-08-12）：越权/身份未解析统一返回 FORBIDDEN 信封，不再向上抛 500。
        # 2026-08-12（七审，care 同口径）：caller/action/reason 三重 or 保底。
        logger.warning("内容服务鉴权拒绝: exc=%s", exc)
        caller = getattr(exc, "caller", None) or "?"
        action = getattr(exc, "action", None) or "access"
        reason = getattr(exc, "reason", None) or str(exc) or "无明确原因"
        return {"ok": False, "error": "FORBIDDEN",
                "detail": f"caller={caller} 无权 {action}（{reason}）"}
    # BUG-56 + B2（2026-08-12）：数据/环境错误（文件缺失/JSON 损坏/_load_* fail-closed
    # 校验）归 INTERNAL_ERROR 且 detail **脱敏**（不裸暴露 str(exc) 中的服务端路径），
    # 完整异常仅留服务端日志。注意：本包的 ValueError 全部来自数据文件加载期校验
    # （_load_guides/_load_sops 的 fail-closed），属服务端数据问题而非客户端入参问题，
    # 故与 care/assessment 的"ValueError→INVALID_INPUT"语义不同，归 INTERNAL_ERROR。
    # KeyError 亦归 INTERNAL_ERROR（[] 访问的键全部来自数据文件，键缺失=数据问题）。
    # CT-B2/B4 修复（2026-08-14）：**客户端入参错误**（guideline_set/limit 非法、
    # query 非字符串等）归 INVALID_INPUT——此前与数据加载错误混同归 INTERNAL_ERROR，
    # 编排层无法区分"改入参重试"与"服务端数据坏了"，误导排障。
    if isinstance(exc, _core.InvalidArgumentError):
        logger.info("内容服务入参错误（客户端）：%s", exc)
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
    if isinstance(exc, (FileNotFoundError, OSError, json.JSONDecodeError, RuntimeError,
                        KeyError, ValueError)):
        logger.warning("内容服务内部数据错误: %s", exc)
        return {"ok": False, "error": "INTERNAL_ERROR",
                "detail": "内部数据错误（error_code=CONTENT_DATA），详情见服务端日志"}
    # 未知系统异常 = 内部 Code Bug——归 INTERNAL_ERROR（编排层不应重试/误判入参问题）
    logger.error("内容服务未预期异常（内部 bug，error_code=CONTENT_UNKNOWN）", exc_info=exc)
    return {"ok": False, "error": "INTERNAL_ERROR",
            "detail": "内容服务内部错误（error_code=CONTENT_UNKNOWN），请查服务端日志"}


def main():
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
