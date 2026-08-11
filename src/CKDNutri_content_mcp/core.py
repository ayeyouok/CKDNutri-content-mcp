# -*- coding: utf-8 -*-
"""M12 知识库检索纯函数。

不依赖 fastmcp，可直接 import 单测。数据来自 data/guidelines.json 与 data/sops.json。
核心：按调用方身份切三套语料视图（full/popular/child），所有结果强制带 source 出处。
身份由部署注入（A207_CALLER），语料 profile 取自 a207_policy.knowledge_profile —— 模型
不能自选语料视图（P0-1：否则患儿伙伴可自称医生拿到全量专业语料）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from ._policy import (
    CLINICIAN_ONLY_FIELDS,
    CLINICIAN_ONLY_HIDDEN_FROM,
    enforce_read,
    get_caller,
    knowledge_profile,
)

# 本包在权限矩阵中的登记名（enforce_* 查表键，唯一事实源在 a207_policy.matrix）。
MCP_NAME = "CKDNutri-content-mcp"

# a207_policy 的 profile 名 → 本包语料字段名（data/guidelines.json 的条目键）。
# 角色→profile 的判定在 policy，本表只做「profile → 语料字段」的数据映射。
_PROFILE_VIEW = {
    "full": "full",
    "plain_language": "popular",
    "child_safe": "child",
}
# 未登记 profile 一律降级到最受限语料（fail-closed，宁可少给不可多给）。
_FALLBACK_VIEW = "child"
# SOP 缺 child 字段时的回退文案（fail-closed：绝不把完整临床处置下发给患儿）。
_CHILD_SOP_FALLBACK = "该 SOP 仅向临床角色提供；如有不适请告知家长或医生。"

_GUIDE_PATH = os.path.join(os.path.dirname(__file__), "data", "guidelines.json")
_SOP_PATH = os.path.join(os.path.dirname(__file__), "data", "sops.json")
_GUIDES: Optional[Dict[str, Any]] = None
_SOPS: Optional[Dict[str, Any]] = None


def _load_guides() -> Dict[str, Any]:
    global _GUIDES
    if _GUIDES is None:
        with open(_GUIDE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # OD-014（P2-4）：加载时校验语料视图字段完整性——缺任一视图字段直接报错，
        # 而不是运行时回退到 full（fail-closed：宁可少给不可多给）。
        for e in data["entries"]:
            missing = [k for k in ("full", "popular", "child")
                       if not str(e.get(k) or "").strip()]
            if missing:
                raise ValueError(
                    f"指南条目 {e.get('id', '?')} 缺少语料视图字段 {missing}，"
                    f"拒绝加载（fail-closed：防止家长/患儿回退到 full 临床语料）")
        _GUIDES = data
    return _GUIDES


def _load_sops() -> Dict[str, Any]:
    global _SOPS
    if _SOPS is None:
        with open(_SOP_PATH, "r", encoding="utf-8") as f:
            _SOPS = json.load(f)
    return _SOPS


def _view_for_caller(caller: str) -> str:
    """按调用方身份取语料视图：profile 由 a207_policy 判定，本包只做字段映射。"""
    return _PROFILE_VIEW.get(knowledge_profile(caller), _FALLBACK_VIEW)


def _match(text: str, query: str) -> bool:
    q = query.lower()
    return q in text.lower()


def search_guideline(query: str, guideline_set: Optional[str] = None) -> Dict[str, Any]:
    """检索指南/共识条文。按调用方身份切语料视图；所有结果带 source 出处。

    语料视图由部署注入的身份决定（doctor_assistant/nutritionist/risk_warning=全量 full；
    parent_assistant=popular 通俗；child_companion=child 科普），调用方不可自选。
    guideline_set: 可选过滤 KDIGO2024/PRNT2020/China2023/Growth2025。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    guides = _load_guides()
    view = _view_for_caller(caller)
    out = []
    for e in guides["entries"]:
        if guideline_set and e.get("set") != guideline_set:
            continue
        hay = " ".join([e["title"], e["tags"].__str__(), e["full"], e.get("popular", ""), e.get("child", "")])
        if not _match(hay, query):
            continue
        out.append({
            "id": e["id"],
            "title": e["title"],
            "set": e["set"],
            "strength": e["strength"],
            "evidence": e["evidence"],
            # OD-014（P2-4）：视图缺失不再回退 full（fail-closed）。
            # _load_guides 已保证 full/popular/child 齐全，此处仅防御性取 ""。
            "text": e.get(view, ""),
            "source": e["source"],
        })
    return {"query": query, "role": caller, "view": view, "count": len(out), "results": out}


def search_sop(query: str) -> Dict[str, Any]:
    """检索院内 SOP。按调用方身份切语料视图：患儿返回安全摘要，不暴露完整临床处置（MX-1）。

    语料视图由部署注入的身份决定（与 search_guideline 一致）；child 视图下仅返回 child 安全版，
    缺字段时回退到 _CHILD_SOP_FALLBACK，绝不把完整临床处置下发给患儿。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    view = _view_for_caller(caller)
    sops = _load_sops()
    out = []
    for s in sops["sops"]:
        hay = " ".join([s["title"], s["tags"].__str__(), s.get("child", ""), s["content"]])
        if _match(hay, query):
            if view == "child":
                content = s.get("child", _CHILD_SOP_FALLBACK)
            else:
                content = s["content"]
            out.append({
                "id": s["id"],
                "title": s["title"],
                "content": content,
                "source": s["source"],
                "view": view,
            })
    return {"query": query, "count": len(out), "results": out}


def get_citation(ref_id: str) -> Dict[str, Any]:
    """生成规范引用串。支持指南条目 id 与 SOP id。"""
    caller = get_caller()
    enforce_read(MCP_NAME)
    guides = _load_guides()
    for e in guides["entries"]:
        if e["id"] == ref_id:
            citation = (f"[{e['id']}] {e['title']}. {e['source']} "
                        f"（推荐强度：{e['strength']}；证据级别：{e['evidence']}）")
            return {"ref_id": ref_id, "citation": citation, "source": e["source"],
                    "strength": e["strength"], "evidence": e["evidence"]}
    sops = _load_sops()
    for s in sops["sops"]:
        if s["id"] == ref_id:
            citation = f"[{s['id']}] {s['title']}. {s['source']}"
            return {"ref_id": ref_id, "citation": citation, "source": s["source"]}
    return {"ref_id": ref_id, "citation": None, "error": "未找到该引用 ID"}


def _self_test_refs() -> List[str]:
    """返回所有可被 get_citation 解析的 id（供校验出处完整性）。"""
    guides = _load_guides()
    sops = _load_sops()
    return [e["id"] for e in guides["entries"]] + [s["id"] for s in sops["sops"]]


# ---- M9: report generation helpers (recovered from a207-report-mcp) ----

_RISK_TO_STATUS = {
    "L1": "critical", "high": "critical",
    "L2": "caution", "L3": "caution", "medium": "caution",
    "L0": "stable", "low": "stable", "none": "stable",
}


def _pew_trend(pew_history: list[dict]) -> str:
    if not pew_history or len(pew_history) < 2:
        return "no_data"
    order = {"low": 0, "medium": 1, "high": 2}
    fo = order.get(pew_history[0].get("level", "low"), 0)
    lo = order.get(pew_history[-1].get("level", "low"), 0)
    return "worsening" if lo > fo else "improving" if lo < fo else "stable"


def _derive_status(risk_level: str, pew_history: list[dict],
                   nutrition_assessment: dict) -> str:
    base = _RISK_TO_STATUS.get(risk_level, "stable")
    # PEW 恶化 → 至少 caution
    if _pew_trend(pew_history) == "worsening" and base == "stable":
        base = "caution"
    # 营养摄入达成率过低（<50%）→ 至少 caution
    try:
        ach = nutrition_assessment.get("intake", {}).get("achievement", {})
        energy_pct = ach.get("energy_pct", 100)
        if isinstance(energy_pct, (int, float)) and energy_pct < 50:
            if base == "stable":
                base = "caution"
    except Exception:
        pass
    return base


def _section(title: str, body: str) -> str:
    return f"### {title}\n{body}\n"


# 仅临床角色可见字段（MX-1 字段可见性边界）：单一事实源直接引用 a207_policy.CLINICIAN_ONLY_FIELDS，
# 不再在包内维护副本（消除 OD-011/OD-013 指出的副本漂移）。
_CLINICIAN_ONLY: frozenset[str] = CLINICIAN_ONLY_FIELDS

# 非临床角色（家长/患儿）绝不可见 CLINICIAN_ONLY_FIELDS；角色集合单一事实源在
# a207_policy.CLINICIAN_ONLY_HIDDEN_FROM（C3 禁止包内硬编码）。
_NON_CLINICAL_MASKED: frozenset[str] = CLINICIAN_ONLY_HIDDEN_FROM


def _mask_clinician_fields(value: Any) -> Any:
    """递归剔除仅临床可见字段，用于生成对外可读文案 / 受限视图。"""
    if isinstance(value, dict):
        return {k: _mask_clinician_fields(v) for k, v in value.items()
                if k not in _CLINICIAN_ONLY}
    if isinstance(value, list):
        return [_mask_clinician_fields(v) for v in value]
    return value


# ---- M9: report generation ----
def generate_patient_report(patient_id: str, demographics: dict, lab_summary: dict,
                            nutrition_assessment: dict, followup_summary: dict,
                            pew_history: list[dict] | None, risk_level: str) -> dict[str, Any]:
    """生成统一患者报告。

    :param patient_id: 患者标识
    :param demographics: {age_years, sex, ckd_stage, dialysis_mode}
    :param lab_summary: 来自 M2(LIS) 的最新化验摘要
    :param nutrition_assessment: 来自 M3(评估)：PRNT 目标 / 摄入达成率 / PEW
    :param followup_summary: 来自 M4(随访)：最近记录 / 计划 / 依从性
    :param pew_history: 来自 M3 get_pew_history（ADR-007：存储归属 M3）
    :param risk_level: 来自 M8(风险规则) 的等级（L0-L3 / low-high）
    :param caller: 内部形参，缺省由部署注入的 A207_CALLER 解析（P0-1：模型不可自证身份）
    :return: {sections, summary_markdown, overall_status, generated_at}
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    ph = pew_history or []
    overall_status = _derive_status(risk_level, ph, nutrition_assessment)
    trend = _pew_trend(ph)
    # MX-1：家长/患儿（非临床角色）拿受限视图 —— sections 与 summary_markdown 一致脱敏，
    # 避免经结构化章节泄露原始化验值（红队 C8：原仅脱敏 summary，sections 仍透出 raw scr/k）。
    mask = caller in _NON_CLINICAL_MASKED

    # ---- 结构化章节 ----
    sections: dict[str, Any] = {
        "patient": {
            "patient_id": patient_id,
            "age_years": demographics.get("age_years"),
            "sex": demographics.get("sex"),
            "ckd_stage": demographics.get("ckd_stage"),
            "dialysis_mode": demographics.get("dialysis_mode"),
        },
        "labs": _mask_clinician_fields(lab_summary) if mask else lab_summary,
        "nutrition": _mask_clinician_fields(nutrition_assessment) if mask else nutrition_assessment,
        "followup": _mask_clinician_fields(followup_summary) if mask else followup_summary,
        "pew_trend": {"count": len(ph), "trend": trend, "source": "M3 (ADR-007)"},
        "risk": {"level": risk_level},
        "overall_status": overall_status,
    }

    # ---- 文案（markdown）----
    lines = [f"# 患者报告 · {patient_id}", ""]
    d_sex = {"M": "男", "F": "女"}.get(demographics.get("sex"), demographics.get("sex", "?"))
    lines.append(_section(
        "一、基本信息",
        f"- 年龄：{demographics.get('age_years')} 岁　性别：{d_sex}\n"
        f"- CKD 分期：{demographics.get('ckd_stage')}　透析方式：{demographics.get('dialysis_mode')}"))
    lines.append(_section("二、最新化验（M2/LIS）",
                          _fmt_dict(_mask_clinician_fields(lab_summary)) or "（无）"))
    lines.append(_section("三、营养评估（M3）",
                          _fmt_dict(_mask_clinician_fields(nutrition_assessment)) or "（无）"))
    _fu = _mask_clinician_fields(followup_summary) or {}
    lines.append(_section(
        "四、随访与依从（M4）",
        f"- 最近随访：{_fmt_dict(_fu.get('records', [])[-1] if _fu.get('records') else {}) or '（无）'}\n"
        f"- 依从性：{_fmt_dict(_fu.get('adherence', [])[-1] if _fu.get('adherence') else {}) or '（无）'}\n"
        f"- 进行中计划数：{len(_fu.get('plans', []))}"))
    lines.append(_section(
        "五、PEW 历史（M3，ADR-007）",
        f"- 历史点数：{len(ph)}　趋势：{trend}"))
    lines.append(_section("六、风险等级（M8）", f"- 等级：{risk_level}"))
    lines.append(_section("七、综合结论", f"**整体状态：{_STATUS_CN.get(overall_status, overall_status)}**"))

    return {
        "ok": True,
        "patient_id": patient_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "overall_status": overall_status,
        "pew_trend": trend,
        "sections": sections,
        "summary_markdown": "\n".join(lines),
    }


_STATUS_CN = {"stable": "稳定", "caution": "需关注", "critical": "紧急"}


def _fmt_dict(d: Any) -> str:
    if not isinstance(d, dict):
        return str(d)
    if not d:
        return ""
    return "\n".join(f"- {k}：{v}" for k, v in d.items())
