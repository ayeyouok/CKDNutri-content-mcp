"""P5 冒烟自测：导入 server 不报错 + 知识库检索与报告生成可调用。

运行：pytest tests/test_import_smoke.py  (或 python tests/test_import_smoke.py)
依赖：a207-policy 已随 pip install -e . 安装；data/guidelines.json、data/sops.json 随包发布。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

os.environ.setdefault("A207_CALLER", "doctor_assistant")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_server_imports():
    """导入 server 不可抛错（回归：server 曾导入不存在的 get_citation_tool 等）。"""
    mod = importlib.import_module("CKDNutri_content_mcp.server")
    assert mod.mcp is not None


def test_knowledge_search_and_report():
    from CKDNutri_content_mcp import core

    r = core.search_guideline("CKD")
    # S1（2026-08-12 五包审查）：统一 {ok, data} 信封——断言随契约更新
    assert r.get("ok") is True and "view" in r["data"]
    assert r["data"].get("count", 0) >= 0

    rep = core.generate_patient_report(
        patient_id="P001",
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


def test_empty_query_note():
    """四审（2026-08-13）回归：空关键词显式提示（防"无匹配"与"没给关键词"混淆）。"""
    from CKDNutri_content_mcp import core

    for fn in (core.search_guideline, core.search_sop):
        r = fn("   ")
        assert r.get("ok") is True and r["data"]["count"] == 0
        assert "关键词为空" in (r["data"].get("note") or ""), r["data"]


if __name__ == "__main__":
    test_server_imports()
    test_knowledge_search_and_report()
    test_guideline_set_validation()
    test_empty_query_note()
    print("P5 SMOKE OK")
