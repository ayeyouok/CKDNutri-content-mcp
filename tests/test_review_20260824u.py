r"""十七审（2026-08-24）content-mcp 修复回归：C1 md_escape 撇号 / C2 PEW 同 ts /
C3 空分支信封 / C4 set 过滤 / C5 透析方式中文映射。

覆盖：
- C1：_md_escape 用 html.escape(quote=False)，含英文撇号文本不再被破坏为 &\#x27;；
      & < > 的 XSS 防护仍完整。
- C2：_pew_trend_info 两条同 timestamp 记录去重后 canon<2 → trend="no_data"
      （此前误判 stable）。
- C3：search_guideline/search_sop 空查询分支 data 含 requested_limit/effective_limit
      （与正常分支信封对齐）。
- C4：_guideline_set_lookup 过滤全空格 set（不混入空键 ""）。
- C5：generate_patient_report 的 dialysis_mode 中文映射仅作用于 markdown，
      sections 机器字段保持原值。
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

from CKDNutri_content_mcp import core  # noqa: E402


def test_md_escape_preserves_apostrophe():
    r"""C1：含撇号文本不被破坏为 &\#x27;；XSS 防护（& < >）仍完整。"""
    out = core._md_escape("Crohn's disease (St. Jude's)")
    assert r"&\#x27;" not in out, out
    assert "&#x27;" not in out, out
    assert "Crohn's disease" in out, out
    assert core._md_escape("<script>&") == "&lt;script&gt;&amp;", core._md_escape("<script>&")


def test_pew_trend_same_timestamp_is_no_data():
    """C2：两条同 timestamp 去重后 canon<2 → trend=no_data（非 stable）。"""
    hist = [
        {"date": "2026-08-18T10:00:00Z", "level": "L2"},
        {"date": "2026-08-18T10:00:00Z", "level": "L2"},
    ]
    info = core._pew_trend_info(hist)
    assert info["trend"] == "no_data", info
    assert info["current_level"] == "L2", info          # 单点仍可见
    assert info["duplicate_timestamp_count"] == 1, info
    # 对照：两条不同 timestamp 仍正常判 stable（修复不误伤正常情形）
    hist2 = [
        {"date": "2026-08-17T10:00:00Z", "level": "L2"},
        {"date": "2026-08-18T10:00:00Z", "level": "L2"},
    ]
    assert core._pew_trend_info(hist2)["trend"] == "stable"


def test_empty_query_envelope_has_limit_keys():
    """C3：空查询分支含 requested_limit / effective_limit（对齐非空分支）。"""
    for fn in (core.search_guideline, core.search_sop):
        d = fn("", limit=20)["data"]
        assert "requested_limit" in d and "effective_limit" in d, (fn.__name__, d)
        assert d["requested_limit"] == 20, (fn.__name__, d)
        assert d["effective_limit"] == 20, (fn.__name__, d)


def test_guideline_set_lookup_strips_whitespace():
    """C4：全空格 set 被过滤（不混入空键 ""）。"""
    fake = {"entries": [
        {"set": "KDIGO2024", "id": "a", "title": "t"},
        {"set": "   ", "id": "b", "title": "t2"},
    ]}
    saved = core._load_guides
    core._load_guides = lambda: fake
    try:
        lut = core._guideline_set_lookup()
        assert "" not in lut, lut
        assert "kdigo2024" in lut, lut
    finally:
        core._load_guides = saved  # 卫生：还原，避免污染后续测试（get_citation 依赖真实加载）


def test_dialysis_mode_chinese_mapping():
    """C5：dialysis_mode 中文映射仅作用于 markdown，machine 字段保持原值。"""
    for dm, zh in (("none", "未透析"), ("hemodialysis", "血液透析"),
                   ("peritoneal", "腹膜透析")):
        rep = core.generate_patient_report(
            "P0007",
            {"age_years": 10, "sex": "M", "ckd_stage": None, "dialysis_mode": dm},
            {}, {}, {}, None, "L1")
        md = rep["data"]["summary_markdown"]
        assert zh in md, (dm, md)
        # machine 字段不被翻译（保持下游可解析原值）
        assert rep["data"]["sections"]["patient"]["dialysis_mode"] == dm, \
            rep["data"]["sections"]["patient"]


if __name__ == "__main__":
    test_md_escape_preserves_apostrophe()
    test_pew_trend_same_timestamp_is_no_data()
    test_empty_query_envelope_has_limit_keys()
    test_guideline_set_lookup_strips_whitespace()
    test_dialysis_mode_chinese_mapping()
    print("CONTENT 十七审 OK")
