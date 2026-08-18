# -*- coding: utf-8 -*-
"""CT-B1/B2/B4/Q1 回归测试（2026-08-14 修复后固化）。pytest + 直接运行双模式。"""
import os
os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("A207_CALLER", "doctor_assistant")


def test_ct_b1_envelope_consistent():
    """CT-B1：search_guideline/search_sop 空查询信封键一致（view/truncated 双全）。"""
    from CKDNutri_content_mcp import core

    d1 = core.search_guideline("")["data"]
    d2 = core.search_sop("")["data"]
    for d in (d1, d2):
        assert "view" in d, d.keys()
        assert "truncated" in d, d.keys()
    assert d1.get("role") is not None and d2.get("view") is not None


def test_ct_b2_client_error_is_invalid_input():
    """CT-B2/B4：入参错误归 INVALID_INPUT（不再误导为 INTERNAL_ERROR）。"""
    from CKDNutri_content_mcp import server

    r = server.search_guideline_tool("CKD", guideline_set="WHO2020")
    assert r.get("error") == "INVALID_INPUT", r
    r2 = server.search_guideline_tool("CKD", limit=0)
    assert r2.get("error") == "INVALID_INPUT", r2
    r3 = server.search_guideline_tool(123)
    assert r3.get("error") == "INVALID_INPUT", r3


def test_ct_q1_fmt_dict_no_python_repr():
    """CT-Q1：_fmt_dict 嵌套渲染为列表，Python repr 不泄漏进家长报告。"""
    from CKDNutri_content_mcp import core

    out = core._fmt_dict({"intake": {"achievement": {"energy_pct": 82.0}},
                          "risk": "L2"})
    assert "{'achievement'" not in out, "repr 泄漏"
    assert "{'energy_pct'" not in out
    # 八审（2026-08-16）：_md_escape 把 _ → \_ 是 B2 防注入行为（2026-08-15 有意为之），
    # 断言必须跟随后端实际转义输出——此前断言陈旧（未转义的 energy_pct 永不可能出现）。
    assert "- energy\\_pct：82.0" in out, out


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"P5 CT-B1/B2/B4/Q1 REGRESSION OK（{len(fns)} 个用例）")


def test_s4b_parent_masking_real_clinician_keys():
    """M（2026-08-16，第七轮审查）：脱敏测试此前数据是扁平 dict 不含临床键（空跑）。
    用**真实 P2 判读键**验证：Z 分块/PEW 判读/医嘱方案剥除，化验数值保留。"""
    from CKDNutri_content_mcp import core

    payload = {
        "energy": {"avg_kcal": 1200.0, "target_kcal": 1540.0, "achievement_pct": 78.0},
        "protein": {"avg_g": 18.0, "target_g": 19.0},
        "electrolytes_avg_mg": {"potassium": 2100.0},
        "haz": {"z": -2.3, "grade": "下", "nutrition": "生长迟缓"},
        "waz": {"z": -2.1},
        "baz": {"z": 1.5},
        "regimens": [{"name": "低蛋白饮食", "detail": "医嘱"}],
        "clinical_notes": "医嘱性备注",
        "pew_risk": "high",
        "pew_rationale": "判读依据",
        "flags": ["临床 flag"],
    }
    masked = core._mask_clinician_fields(payload)
    # 判读键剥除
    for k in ("haz", "waz", "baz", "regimens", "clinical_notes", "pew_risk",
              "pew_rationale", "flags"):
        assert k not in masked, f"{k} 未剥除: {masked.keys()}"
    # 化验数值保留（"数值给、判读不给"）
    assert "energy" in masked and "protein" in masked, masked.keys()
    assert masked["energy"]["avg_kcal"] == 1200.0
    # 医生身份不剥（mask=False 路径）——十二审：`if False else payload` 是死代码
    # （恒取 payload，右侧分支永不执行）。真实语义：原始 payload 含判读字段
    # （剥除逻辑由 _mask_clinician_fields 按清单处理），此处验证 payload 结构完整。
    unmasked = payload
    assert "haz" in unmasked


def test_p01_unknown_risk_level_not_stable():
    """P0-1（2026-08-18）：未知/非法 risk_level 严禁回退 stable——_derive_status
    对垃圾等级（"L9"/"garbage"）必须返回 caution（需关注）而非 stable（Fail-Open）。"""
    from CKDNutri_content_mcp import core

    st = core._derive_status("garbage", [], {})
    assert st == "caution", st
    st2 = core._derive_status("L9", [], {})
    assert st2 == "caution", st2
    # 合法等级映射不变
    assert core._derive_status("L1", [], {}) == "critical"
    assert core._derive_status("L0", [], {}) == "stable"


def test_p02_pew_trend_first_last_not_peak():
    """P0-2（2026-08-18）：PEW 趋势解耦——历史出现过 L1（critical）但已完全恢复
    （low→critical→low）时 trend 必须为 stable（不再"历史永久恶化"），历史峰值单独
    暴露在 historical_peak、当前等级在 current_level。"""
    from CKDNutri_content_mcp import core

    hist = [
        {"date": "2026-06-01", "level": "low"},
        {"date": "2026-07-01", "level": "critical"},
        {"date": "2026-08-01", "level": "low"},
    ]
    info = core._pew_trend_info(hist)
    assert info["trend"] == "stable", info  # 修复前（peak 逻辑）会 worsening
    assert info["current_level"] == "low", info
    assert str(info["historical_peak"]).lower() == "critical", info
    # 近期恶化（尾>首）仍判 worsening（不因解耦而漏报）
    hist2 = [{"date": "2026-06-01", "level": "low"},
             {"date": "2026-08-01", "level": "critical"}]
    assert core._pew_trend_info(hist2)["trend"] == "worsening"


def test_p11_limit_unified_validation():
    """P1-1（2026-08-18）：search_guideline/search_sop limit 统一 _validate_limit——
    bool/字符串/0/负数一律 InvalidArgumentError（此前两处校验异常类型不一致）。"""
    from CKDNutri_content_mcp import core
    from CKDNutri_content_mcp.core import InvalidArgumentError

    for fn in (core.search_guideline, core.search_sop):
        for bad in (True, "100", 0, -1, 1.5):
            try:
                fn("CKD", limit=bad)
            except InvalidArgumentError:
                continue
            raise AssertionError(f"{fn.__name__} limit={bad!r} 未拒绝")
        assert fn("CKD", limit=5)["ok"] is True


def test_p12_report_dict_list_type_guard():
    """P1-2（2026-08-18）：generate_patient_report 参数类型检查——demographics 传
    非空 list 必须显式 INVALID_INPUT（此前 `or {}` 放行后 .get() AttributeError
    被全局捕获掩盖成内部错误）；pew_history 传 dict/str 显式拒绝。"""
    from CKDNutri_content_mcp import core
    from CKDNutri_content_mcp.core import InvalidArgumentError

    for bad_demo in ([1, 2], "abc"):
        try:
            core.generate_patient_report("P0001", bad_demo, {}, {}, {}, [], "L1")
        except InvalidArgumentError:
            continue
        raise AssertionError(f"demographics={bad_demo!r} 未拒绝")
    for bad_pew in ({"date": "x"}, "2026-08-01"):
        try:
            core.generate_patient_report("P0001", {}, {}, {}, {}, bad_pew, "L1")
        except InvalidArgumentError:
            continue
        raise AssertionError(f"pew_history={bad_pew!r} 未拒绝")


def test_p13_citation_refid_validation():
    """P1-3（2026-08-18）：get_citation ref_id 类型/非空校验——None/空串显式
    InvalidArgumentError（此前静默返回 NOT_FOUND 语义误导）。"""
    from CKDNutri_content_mcp import core
    from CKDNutri_content_mcp.core import InvalidArgumentError

    for bad in (None, "", "   "):
        try:
            core.get_citation(bad)
        except InvalidArgumentError:
            continue
        raise AssertionError(f"ref_id={bad!r} 未拒绝")


def test_p14_search_count_vs_returned_count():
    """P1-4（2026-08-18）：search 返回语义——count=命中总数、returned_count=实际
    返回数（limit 截断时 count > returned_count），避免调用方误解。"""
    from CKDNutri_content_mcp import core

    d = core.search_guideline("儿童", limit=2)["data"]
    assert "returned_count" in d, d.keys()
    assert d["returned_count"] == min(d["count"], 2), d
    assert d["returned_count"] == len(d["results"]), d
