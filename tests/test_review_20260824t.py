"""十六审（2026-08-24）content-mcp 修复回归：#4 SOP 信封对齐 / #6 改名 / #7 报告 None 兜底。

覆盖：
- #4：search_sop 正常分支与空分支 data 均含 role/view，与 search_guideline 信封对齐。
- #6：_validate_and_canonicalize_demographics 函数存在并可 canonicalize（就地写回）。
- #7：generate_patient_report 缺失 age_years/ckd_stage/dialysis_mode 时渲染"未提供"
      而非 Python None 字面量。
"""
import os

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("A207_CALLER", "doctor_assistant")


def test_search_sop_envelope_has_role_and_view():
    """#4：search_sop 正常分支与空分支 data 均含 role + view（对齐 search_guideline）。"""
    from CKDNutri_content_mcp import core
    # 正常分支（命中查询）
    d_ok = core.search_sop("随访")["data"]
    assert "role" in d_ok and "view" in d_ok, d_ok
    # 空分支
    d_empty = core.search_sop("")["data"]
    assert "role" in d_empty and "view" in d_empty, d_empty
    # 与 search_guideline 同键
    g = core.search_guideline("")["data"]
    for k in ("role", "view", "query", "count", "results"):
        assert k in d_ok and k in g, (k, d_ok, g)


def test_validate_and_canonicalize_demographics_renamed():
    """#6：改名后函数存在且就地 canonicalize（sex/ckd_stage/dialysis_mode）。"""
    from CKDNutri_content_mcp import core
    demo = {"age_years": 10, "sex": " m ", "ckd_stage": "ckd3", "dialysis_mode": "Peritoneal"}
    core._validate_and_canonicalize_demographics(demo)
    assert demo["sex"] == "M", demo
    assert demo["ckd_stage"] == "G3", demo
    assert demo["dialysis_mode"] == "peritoneal", demo


def test_report_missing_demographics_shows_unprovided():
    """#7：缺失基本信息字段渲染"未提供"而非 None 字面量。"""
    from CKDNutri_content_mcp import core
    rep = core.generate_patient_report(
        "P0007",
        {"age_years": None, "sex": None, "ckd_stage": None, "dialysis_mode": None},
        {}, {}, {}, [], "L1")
    md = rep["data"]["summary_markdown"]
    assert "None" not in md, md
    assert "未提供" in md, md
