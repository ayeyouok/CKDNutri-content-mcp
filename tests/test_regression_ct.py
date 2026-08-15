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
    assert "- energy_pct：82.0" in out, out


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"P5 CT-B1/B2/B4/Q1 REGRESSION OK（{len(fns)} 个用例）")
