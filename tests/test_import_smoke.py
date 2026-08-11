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
    assert r.get("count", 0) >= 0 and "view" in r

    rep = core.generate_patient_report(
        patient_id="P001",
        demographics={"age_years": 6, "sex": "M", "ckd_stage": 3, "dialysis_mode": "none"},
        lab_summary={"scr_umol_L": 120, "k_mmol_L": 4.5, "albumin_g_L": 38},
        nutrition_assessment={"intake": {"achievement": {"energy_pct": 82}}},
        followup_summary={"records": [{"date": "2026-08-01"}], "adherence": [{"score": 0.9}], "plans": [1]},
        pew_history=[{"level": "low"}, {"level": "medium"}],
        risk_level="L2",
    )
    assert rep.get("ok") is True and "sections" in rep


if __name__ == "__main__":
    test_server_imports()
    test_knowledge_search_and_report()
    print("P5 SMOKE OK")
