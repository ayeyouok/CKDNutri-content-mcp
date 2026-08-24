r"""十二审（2026-08-24）content-mcp 修复回归：3 claims 修复。

覆盖：
- #1：generate_patient_report 的 PEW 章节在 historical_peak == current_level（初诊
      单点 / 持续同级）时仍展示"当前等级"，不再整段漏显。
- #2：_fmt_dict / _fmt_list 嵌套严格缩进——无双重弹头、子列表不破层级。
- #3：_guideline_set_lookup 并发安全缓存——连续调用返回同一对象（DCL 命中）。

前 6 项（C6–C11）已在十八审确认闭环，本文件不重复覆盖。
"""
import os

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from CKDNutri_content_mcp import core  # noqa: E402


def _minimal_report_args(pew_history):
    return dict(
        patient_id="P0007",
        demographics={"age_years": 8, "sex": "M", "ckd_stage": "G3a",
                      "dialysis_mode": "none"},
        lab_summary={}, nutrition_assessment={}, followup_summary={},
        pew_history=pew_history, risk_level="中")


def test_content1_pew_current_level_always_shown():
    """#1：单点 PEW（current_level==historical_peak）报告仍展示当前等级。"""
    # 单点 L2 → current_level=L2, historical_peak=L2（此前条件不满足 → 漏显）
    resp = core.generate_patient_report(
        **_minimal_report_args([{"date": "2026-08-01", "level": "L2"}]))
    assert resp["ok"] is True, resp
    md = resp["data"]["summary_markdown"]
    assert "当前等级：L2" in md, "PEW 单点应展示当前等级 L2，实际漏显：" + md
    # 不同值场景（历史最高不同）应同时展示当前 + 历史最高
    resp2 = core.generate_patient_report(
        **_minimal_report_args([
            {"date": "2026-07-01", "level": "L1"},
            {"date": "2026-08-01", "level": "L2"}]))
    md2 = resp2["data"]["summary_markdown"]
    assert "当前等级：L2" in md2, "不同值时当前等级应展示：" + md2
    # historical_peak 与 current 不同时，应追加"历史最高"提示
    assert "历史最高：" in md2, "historical_peak 与 current 不同时应追加历史最高：" + md2


def test_content2_fmt_dict_nested_indent():
    """#2：嵌套 dict 渲染带缩进，无双重弹头、子项不破顶层。"""
    d = {"electrolytes": {"k": "4.5 mmol/L", "na": "140 mmol/L"}}
    out = core._fmt_dict(d)
    lines = out.split("\n")
    # 第一行应为 "- electrolytes：" 且不含第二个 "-"
    assert lines[0].startswith("- electrolytes："), lines[0]
    assert "- -" not in lines[0], "不应出现双重弹头"
    # 子项应缩进（2 空格前缀），且含 "- k："
    child = [l for l in lines if l.strip().startswith("- k：")]
    assert child, "子项 k 应存在：" + out
    assert child[0].startswith("  - k："), "子项应缩进 2 空格：" + repr(child[0])


def test_content3_guideline_set_lookup_cached():
    """#3：_guideline_set_lookup 连续调用返回同一对象（DCL 缓存命中）。"""
    a = core._guideline_set_lookup()
    b = core._guideline_set_lookup()
    assert a is b, "应命中缓存返回同一 dict 对象，而非每次重建"


if __name__ == "__main__":
    test_content1_pew_current_level_always_shown()
    test_content2_fmt_dict_nested_indent()
    test_content3_guideline_set_lookup_cached()
    print("content 十二审回归全部通过")
