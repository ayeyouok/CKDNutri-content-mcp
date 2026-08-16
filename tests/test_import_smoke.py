"""P5 冒烟自测：导入 server 不报错 + 知识库检索与报告生成可调用。

运行：pytest tests/test_import_smoke.py  (或 python tests/test_import_smoke.py)
依赖：a207-policy 已随 pip install -e . 安装；data/guidelines.json、data/sops.json 随包发布。
"""
from __future__ import annotations

import importlib
import os
os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import sys
from pathlib import Path

os.environ.setdefault("A207_CALLER", "doctor_assistant")

SRC = Path(__file__).resolve().parents[1] / "src"
# 八审（2026-08-16）：与 test_regression_ct.py 同口径，显式把 a207-policy/src 置顶。
# 此前只加本包 src——若测试机 sys.path 残留旧版 a207_policy 副本（如
# D:\MyUserData\...\a207-policy\src，仅 15 个临床字段、缺 haz/waz/baz/regimens/
# pew_risk），pytest 收集本文件先导入时会把它缓存进 core._CLINICIAN_ONLY
# （模块级常量，之后不可变）→ test_s4b 用真实判读键断言必失败。insert(0) 确保
# 仓库内新版 a207-policy 优先于任何已安装/残留副本。
_POLICY_SRC = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for _p in (SRC, _POLICY_SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def test_server_imports():
    """导入 server 不可抛错（回归：server 曾导入不存在的 get_citation_tool 等）。"""
    mod = importlib.import_module("CKDNutri_content_mcp.server")
    assert mod.mcp is not None


def test_knowledge_search_and_report():
    from CKDNutri_content_mcp import core

    r = core.search_guideline("CKD")
    # S1（2026-08-12 五包审查）：统一 {ok, data} 信封——断言随契约更新
    assert r.get("ok") is True and "view" in r["data"]
    # X2（2026-08-14）：此前 `count >= 0` 恒真（count 非负必然成立）——改为有意义的断言：
    # 检索 "CKD" 必须命中指南（语料库非空），且每条结果携带 source 出处契约键。
    assert r["data"]["count"] > 0, "检索 'CKD' 应命中指南，语料库可能为空"
    for item in r["data"]["results"][:3]:
        assert item.get("source") or item.get("set"), f"指南条目缺 source/set 出处: {item}"

    rep = core.generate_patient_report(
        patient_id="P0001",
        demographics={"age_years": 6, "sex": "M", "ckd_stage": 3, "dialysis_mode": "none"},
        lab_summary={"scr_umol_L": 120, "k_mmol_L": 4.5, "albumin_g_L": 38},
        nutrition_assessment={"intake": {"achievement": {"energy_pct": 82}}},
        followup_summary={"records": [{"date": "2026-08-01"}], "adherence": [{"score": 0.9}], "plans": [1]},
        pew_history=[{"level": "low"}, {"level": "medium"}],
        risk_level="L2",
    )
    assert rep.get("ok") is True and "sections" in rep["data"]


def test_guideline_set_validation():
    """四审（2026-08-13）回归：guideline_set 大小写容错 + 非法值报错。"""
    from CKDNutri_content_mcp import core

    # 大小写不敏感：小写/混合大小写均归一化到规范名
    for variant in ("KDIGO2024", "kdigo2024", "Kdigo2024"):
        r = core.search_guideline("CKD", guideline_set=variant)
        assert r.get("ok") is True, (variant, r)
        sets = {e.get("set") for e in r["data"]["results"]}
        assert sets <= {"KDIGO2024"}, (variant, sets)
    # 非法值显式报错（fail-closed，此前静默返回空结果）
    try:
        core.search_guideline("CKD", guideline_set="WHO2020")
    except ValueError as exc:
        assert "WHO2020" in str(exc)
    else:
        raise AssertionError("非法 guideline_set 应抛 ValueError")
    # 非字符串拒绝
    try:
        core.search_guideline("CKD", guideline_set=123)
    except ValueError:
        pass
    else:
        raise AssertionError("非字符串 guideline_set 应抛 ValueError")


def test_unknown_risk_level_transparency():
    """N3 修复回归（2026-08-13）：未知 risk_level 必须透明标注，不得静默归 stable。

    六审逻辑：_derive_status 对非法等级（如 "L9"）fail-open 归 stable——报告必须
    在 risk.valid=false + markdown 中提示核对上游，防"未知风险被展示为稳定"。
    """
    from CKDNutri_content_mcp import core

    base = dict(
        patient_id="P0001",
        demographics={"age_years": 6, "sex": "M", "ckd_stage": 3, "dialysis_mode": "none"},
        lab_summary={"scr_umol_L": 120},
        nutrition_assessment={"intake": {}},
        followup_summary={"records": [], "adherence": [], "plans": []},
        pew_history=[],
    )
    # 未知等级 → risk.valid=false + markdown 提示核对
    bad = core.generate_patient_report(**base, risk_level="L9")
    assert bad["ok"] is True, bad
    assert bad["data"]["sections"]["risk"]["valid"] is False, bad["data"]["sections"]["risk"]
    assert "核对" in bad["data"]["summary_markdown"], bad["data"]["summary_markdown"][:300]
    # 已知等级 → risk.valid=true（不误伤）
    ok = core.generate_patient_report(**base, risk_level="L2")
    assert ok["data"]["sections"]["risk"]["valid"] is True, ok["data"]["sections"]["risk"]
    # 空等级同样透明（"" → 未知）
    none = core.generate_patient_report(**base, risk_level="")
    assert none["data"]["sections"]["risk"]["valid"] is False


def test_empty_query_note():
    """四审（2026-08-13）回归：空关键词显式提示（防"无匹配"与"没给关键词"混淆）。"""
    from CKDNutri_content_mcp import core

    for fn in (core.search_guideline, core.search_sop):
        r = fn("   ")
        assert r.get("ok") is True and r["data"]["count"] == 0
        assert "关键词为空" in (r["data"].get("note") or ""), r["data"]


def test_s4_parent_masking():
    """S4/P0-4（2026-08-13）家长视图脱敏：**化验数值保留、临床判读裁切**。

    2026-08-13 用户决策：家长对化验原始数值有知情权（P1 get_labs 返回 parent_full），
    "数值给、判读不给"。CLINICIAN_ONLY_FIELDS 已重定义为纯判读字段（医生备注/EMR 状态/
    Z 分/分期确认/危急值标记/等级修正）——家长报告应**含化验数值**、不含判读字段。
    """
    import json

    from CKDNutri_content_mcp import core

    os.environ["A207_CALLER"] = "parent_assistant"
    try:
        rep = core.generate_patient_report(
            patient_id="P0001",
            demographics={"age_years": 6, "sex": "M", "ckd_stage": 3, "dialysis_mode": "none"},
            lab_summary={"scr_umol_L": 120, "k_mmol_L": 4.5, "albumin_g_L": 38},
            nutrition_assessment={"intake": {"achievement": {"energy_pct": 82}}},
            followup_summary={"records": [{"date": "2026-08-01"}],
                              "adherence": [{"score": 0.9}], "plans": [1]},
            pew_history=[{"level": "low"}],
            risk_level="L2",
        )
    finally:
        os.environ["A207_CALLER"] = "doctor_assistant"
    assert rep.get("ok") is True, rep
    snap = json.dumps(rep["data"], ensure_ascii=False)
    # ① 化验数值对家长可见（知情权决策）：scr/k/albumin 值应存在
    for leak in ("120", "4.5", "38"):
        assert leak in snap, f"家长视图缺失化验数值（决策要求可见）: {leak}"
    # ② 临床判读字段绝不可见（CLINICIAN_ONLY_FIELDS 重定义后）：
    #    医生备注/EMR 状态/危急值标记/Z 分/分期确认
    for hidden in ("doctor_notes", "note_to_clinician", "emr_status", "push_to_emr",
                   "z_score_height", "stage_confirmed_by", "critical_flag"):
        assert hidden not in snap, f"家长视图泄露临床判读字段: {hidden}"


if __name__ == "__main__":
    test_server_imports()
    test_knowledge_search_and_report()
    test_guideline_set_validation()
    test_unknown_risk_level_transparency()
    test_empty_query_note()
    test_s4_parent_masking()
    print("P5 SMOKE OK")
