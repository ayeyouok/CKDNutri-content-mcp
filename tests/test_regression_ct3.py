"""二十审（2026-08-19）content-mcp 回归：审查 一~四 修复固化。

覆盖（对应用户回归清单）：
1. 同一天不同时间的 PEW 记录视为不同记录（valid_count=2、dup=0、current=最新）
2. 完全相同 timestamp 才视为重复（合并取最高）
3. L1/L2/L3 正常识别
4. "L!!!1"/"L-1"/"L 1" 非法格式不得被自动转换成合法等级（risk.valid=false）
5. sex 非字符串拒绝
6. dialysis_mode 非字符串拒绝
7. plans=3 接受 / 8. plans=0 接受 / 9. plans=-1 拒绝 / 10. plans=3.9 拒绝（不静默截断）

pytest + 直接运行双模式（CI 逐文件 `python tests/test_*.py`，不依赖 pytest）。
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


def _expect_raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__} to be raised")


def _report(demographics, risk_level="L2", followup_summary=None, lab_summary=None):
    return core.generate_patient_report(
        patient_id="P0010", demographics=demographics,
        lab_summary=lab_summary or {}, nutrition_assessment={},
        followup_summary=followup_summary or {},
        pew_history=[], risk_level=risk_level)


def _base_demo(**over):
    d = {"age_years": 6, "sex": "M", "ckd_stage": "G2", "dialysis_mode": "none"}
    d.update(over)
    return d


# ---- 1/2：PEW 同日不同时间 / 相同 timestamp ----

def test_pew_same_day_different_time_are_distinct():
    """同日不同时刻（08:00 / 18:00）是两条独立记录，不合并（用户回归清单 1）。"""
    hist = [
        {"date": "2026-08-19T08:00:00+08:00", "level": "L3"},
        {"date": "2026-08-19T18:00:00+08:00", "level": "L1"},
    ]
    info = core._pew_trend_info(hist)
    assert info["valid_count"] == 2, info
    assert info["duplicate_timestamp_count"] == 0, info
    assert info["current_level"] == "L1", info  # 18:00 最新


def test_pew_exact_same_timestamp_merged():
    """完全相同 timestamp 才算重复：合并取风险最高（用户回归清单 2）。"""
    hist = [
        {"date": "2026-08-19T08:00:00+08:00", "level": "L3"},
        {"date": "2026-08-19T08:00:00+08:00", "level": "L1"},
    ]
    info = core._pew_trend_info(hist)
    assert info["valid_count"] == 2, info  # 有效点数仍计 2（两条都有效）
    assert info["duplicate_timestamp_count"] == 1, info  # 同一 timestamp 重复
    assert info["conflict_count"] == 1, info
    assert info["current_level"] == "L1", info  # 同刻取风险最高（L1=3）


def test_pew_levels_l1_l2_l3_recognized():
    """L1/L2/L3 正常识别（用户回归清单 3）。"""
    info = core._pew_trend_info([
        {"date": "2026-08-01", "level": "L3"},
        {"date": "2026-08-08", "level": "L2"},
        {"date": "2026-08-15", "level": "L1"},
    ])
    assert info["current_level"] == "L1", info
    assert info["historical_peak"] == "L1", info
    assert info["trend"] == "worsening", info


# ---- 3：risk_level 严格白名单 ----

def test_invalid_risk_formats_not_coerced():
    """"L!!!1"/"L-1"/"L 1" 不得被自动转换成合法等级（用户回归清单 4）。"""
    for bad in ("L!!!1", "L-1", "L 1", "L@#1", "L9"):
        r = _report(_base_demo(), risk_level=bad)
        risk = r["data"]["sections"]["risk"]
        assert risk["valid"] is False, (bad, risk)
        assert risk["reason"] == "INVALID_RISK_LEVEL", (bad, risk)
        # 不会因清洗而变成 critical（L1）
        assert r["data"]["overall_status"] != "critical", (bad, r["data"]["overall_status"])
    # 合法格式正常识别
    for good, expect in (("L1", "critical"), ("l2", "caution"), ("L3", "stable")):
        r = _report(_base_demo(), risk_level=good)
        assert r["data"]["sections"]["risk"]["valid"] is True, (good, r)
        assert r["data"]["overall_status"] == expect, (good, r)


# ---- 4：sex / dialysis_mode 严格类型 ----

def test_sex_non_string_rejected():
    """sex 非字符串（123/[]/dict）拒绝（用户回归清单 5）。"""
    for bad in (123, [], {"m": 1}):
        _expect_raises(core.InvalidArgumentError, lambda b=bad: _report(_base_demo(sex=b)))
    # 合法字符串（含大小写/空格变体）仍 canonicalize
    r = _report(_base_demo(sex=" f "))
    assert r["data"]["sections"]["patient"]["sex"] == "F", r


def test_dialysis_mode_non_string_rejected():
    """dialysis_mode 非字符串拒绝（用户回归清单 6）。"""
    _expect_raises(core.InvalidArgumentError,
                   lambda: _report(_base_demo(dialysis_mode=123)))
    _expect_raises(core.InvalidArgumentError,
                   lambda: _report(_base_demo(dialysis_mode=[])))
    r = _report(_base_demo(dialysis_mode="Peritoneal"))
    assert r["data"]["sections"]["patient"]["dialysis_mode"] == "peritoneal", r


# ---- 5：plans 数值边界 ----

def test_plans_count_int_accepted():
    """plans=3 / 0 正常接受（用户回归清单 7/8）。"""
    r3 = _report(_base_demo(), followup_summary={"plans": 3})
    assert r3["ok"] is True and "进行中计划数：3" in r3["data"]["summary_markdown"], r3
    r0 = _report(_base_demo(), followup_summary={"plans": 0})
    assert r0["ok"] is True and "进行中计划数：0" in r0["data"]["summary_markdown"], r0


def test_plans_count_negative_or_float_rejected():
    """plans=-1 / 3.9 拒绝（不静默截断、不接受负数；用户回归清单 9/10）。"""
    _expect_raises(core.InvalidArgumentError,
                   lambda: _report(_base_demo(), followup_summary={"plans": -1}))
    _expect_raises(core.InvalidArgumentError,
                   lambda: _report(_base_demo(), followup_summary={"plans": 3.9}))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"CT3 REGRESSION OK（{len(fns)} 个用例）")
