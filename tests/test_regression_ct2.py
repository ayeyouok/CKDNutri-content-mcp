"""十八审（2026-08-18）content-mcp 回归：审查报告 P1-1~P1-4 / P2-1~P2-6。

覆盖（对应用户建议 Test 1-7 + 新增项）：
- P1-1：sex canonicalize（" m " → "M"）
- P1-2：ckd_stage 白名单严格枚举（CKD99/banana/G99 拒绝；CKD3/3/G3A → G3/G3a）
- P1-3：dialysis_mode canonicalize（" Hemodialysis " → "hemodialysis"）
- P1-4：非法 risk_level 机器可读 reason（INVALID_RISK_LEVEL）
- P2-1：PEW 同时间点重复记录结果与输入顺序无关（取风险最高）
- P2-2：historical_high_risk 机器字段（历史中间恶化保留提示）
- P2-3：nutrition 解析单一事实源（_derive_status 与 nutrition_valid 同逻辑）
- P2-4：深层嵌套不 RecursionError（渲染深度上限）
- P2-5：超大 list 截断 + 超长文本截断
- P2-6：core 非法入参抛 InvalidArgumentError（不构造 MCP envelope）
"""
import os

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest

from CKDNutri_content_mcp import core


def _report(demographics, lab_summary=None, nutrition=None, followup=None,
            pew=None, risk_level="L2"):
    return core.generate_patient_report(
        patient_id="P0010",
        demographics=demographics,
        lab_summary=lab_summary or {},
        nutrition_assessment=nutrition or {},
        followup_summary=followup or {},
        pew_history=pew,
        risk_level=risk_level)


def _base_demo(**over):
    d = {"age_years": 6, "sex": "M", "ckd_stage": "CKD3", "dialysis_mode": "none"}
    d.update(over)
    return d


# ---- P1-1：sex canonicalization ----

def test_p11_sex_canonicalize():
    """P1-1：' m ' 校验通过后规范化为 'M'，报告层不再出现原始变体。"""
    r = _report(_base_demo(sex=" m "))
    assert r["ok"] is True, r
    assert r["data"]["sections"]["patient"]["sex"] == "M", r
    r2 = _report(_base_demo(sex="f"))
    assert r2["data"]["sections"]["patient"]["sex"] == "F", r2


# ---- P1-2：ckd_stage 严格枚举 ----

def test_p12_ckd_stage_whitelist_rejects():
    """P1-2：CKD99 / banana / G99 / 空串 一律拒绝（非法 stage 不得进患者报告）。"""
    for bad in ("CKD99", "banana", "G99", "", "stage3"):
        with pytest.raises(core.InvalidArgumentError):
            _report(_base_demo(ckd_stage=bad))


def test_p12_ckd_stage_canonicalize():
    """P1-2：合法变体归一为 G 记法（数字→Gx、CKDx→Gx、G3A→G3a 保留子分期）。"""
    cases = {3: "G3", "CKD5": "G5", "G1": "G1", "G3A": "G3a", "G3B": "G3b", "g4": "G4"}
    for raw, want in cases.items():
        r = _report(_base_demo(ckd_stage=raw))
        got = r["data"]["sections"]["patient"]["ckd_stage"]
        assert got == want, (raw, got, want)


# ---- P1-3：dialysis_mode canonicalization ----

def test_p13_dialysis_mode_canonicalize():
    """P1-3：' Hemodialysis ' / 'HEMODIALYSIS' 归一为 'hemodialysis'。"""
    for raw in (" Hemodialysis ", "HEMODIALYSIS", "Peritoneal"):
        r = _report(_base_demo(dialysis_mode=raw))
        got = r["data"]["sections"]["patient"]["dialysis_mode"]
        assert got == raw.strip().lower(), (raw, got)
    with pytest.raises(core.InvalidArgumentError):
        _report(_base_demo(dialysis_mode="hemodialysls"))  # 拼写错误


# ---- P1-4：非法 risk_level 机器可读原因 ----

def test_p14_invalid_risk_reason():
    """P1-4：非法 risk_level fallback 与真实 caution 机器可读区分。"""
    r_bad = _report(_base_demo(), risk_level="L9")
    risk = r_bad["data"]["sections"]["risk"]
    assert risk["valid"] is False and risk["reason"] == "INVALID_RISK_LEVEL", risk
    assert r_bad["data"]["overall_status"] == "caution"  # fail-safe 兜底不变
    r_good = _report(_base_demo(), risk_level="L2")
    assert r_good["data"]["sections"]["risk"]["reason"] == "VALID"
    assert r_good["data"]["overall_status"] == "caution"  # 真实 caution


# ---- P2-1：PEW 同时间点冲突确定性 ----

def test_p21_pew_same_timestamp_order_independent():
    """P2-1：同时间点重复记录，输入顺序不同结果必须完全一致（取风险最高）。

    注：PEW 语境 _PEW_ORDER 中 **L1=3（最高风险）、L3=1（最低）**——同时间点
    [L1, L3] 冲突时 canonical 取风险最高者 = L1。
    """
    h_a = [{"date": "2026-08-18", "level": "L1"},
           {"date": "2026-08-18", "level": "L3"}]
    h_b = [{"date": "2026-08-18", "level": "L3"},
           {"date": "2026-08-18", "level": "L1"}]
    # 直接对比 _pew_trend_info（趋势/当前/峰值/统计四元组）
    info_a = core._pew_trend_info(h_a)
    info_b = core._pew_trend_info(h_b)
    for k in ("trend", "current_level", "historical_peak", "valid_count",
              "duplicate_timestamp_count", "conflict_count", "historical_high_risk"):
        assert info_a[k] == info_b[k], (k, info_a[k], info_b[k])
    # 同时间点 canonical 取风险最高（L1）：当前等级与峰值均为 L1
    assert info_a["current_level"] == "L1", info_a
    assert info_a["historical_peak"] == "L1", info_a
    assert info_a["duplicate_timestamp_count"] == 1, info_a
    assert info_a["conflict_count"] == 1, info_a


# ---- P2-2：historical_high_risk ----

def test_p22_historical_high_risk_flag():
    """P2-2：历史中间严重恶化（L3→L1→L3）trend=stable 但 historical_high_risk=True。"""
    info = core._pew_trend_info([
        {"date": "2026-08-01", "level": "L3"},
        {"date": "2026-08-10", "level": "L1"},
        {"date": "2026-08-18", "level": "L3"},
    ])
    assert info["trend"] == "stable", info  # 首尾相同
    assert info["historical_peak"] == "L1", info
    assert info["historical_high_risk"] is True, info  # L1 为高风险（序 3）


# ---- P2-3：nutrition 解析单一事实源 ----

def test_p23_nutrition_single_source_of_truth():
    """P2-3：_derive_status 与 nutrition_valid 对每种合法 schema 判定一致。"""
    schemas = [
        {"data": {"energy": {"achievement_pct": 40}}},        # P2 实际输出
        {"energy": {"achievement_pct": 40}},                  # 直接结构
        {"intake": {"achievement": {"energy_pct": 40}}},      # 旧契约
        {"data": {"energy": {"achievement_pct": 60}}},        # ≥50 不升级
        {},                                                   # 空 → 未评估
    ]
    for schema in schemas:
        valid = core._extract_energy_achievement_pct(schema) is not None
        # 报告层 nutrition_valid 与单一解析一致
        r = _report(_base_demo(), nutrition=schema, risk_level="L3")
        assert r["data"]["sections"]["nutrition_valid"] == valid, (schema, valid)
        # _derive_status 用同一解析（<50 且 stable → caution；未评估不升级）
        status = core._derive_status("L3", [], schema)
        pct = core._extract_energy_achievement_pct(schema)
        if pct is not None and pct < 50:
            assert status == "caution", (schema, status)
        else:
            assert status == "stable", (schema, status)


# ---- P2-4：深层嵌套 ----

def test_p24_deep_nesting_no_recursion_error():
    """P2-4：100 层嵌套 dict 不触发 RecursionError（渲染深度上限省略）。"""
    deep = {}
    cur = deep
    for _ in range(100):
        cur["x"] = {}
        cur = cur["x"]
    r = _report(_base_demo(), lab_summary={"nested": deep})
    assert r["ok"] is True, r
    md = r["data"]["summary_markdown"]
    assert "嵌套过深" in md, md  # 显式省略提示


# ---- P2-5：超大 list / 超长文本 ----

def test_p25_huge_list_truncated():
    """P2-5：超大 list 不崩溃、输出截断、有明确截断提示。"""
    r = _report(_base_demo(), lab_summary={"notes": ["x"] * 100000})
    assert r["ok"] is True, r
    md = r["data"]["summary_markdown"]
    assert "共 100000 项，仅显示前 100 项" in md, md[:500]
    assert len(md) < 10000, len(md)  # response 大小受控


def test_p25_long_text_truncated():
    """P2-5：超长单文本字段截断（_MAX_TEXT_LENGTH）。"""
    r = _report(_base_demo(), lab_summary={"note": "长" * 10000})
    md = r["data"]["summary_markdown"]
    assert "已截断" in md, md[:300]


# ---- P2-6：core 不构造 MCP error envelope ----

def test_p26_invalid_patient_id_raises():
    """P2-6：畸形 patient_id 抛 InvalidArgumentError（非返回错误信封）。"""
    with pytest.raises(core.InvalidArgumentError):
        core.generate_patient_report(
            patient_id="abc", demographics=_base_demo(), lab_summary={},
            nutrition_assessment={}, followup_summary={}, pew_history=[], risk_level="L2")
