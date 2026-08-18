# -*- coding: utf-8 -*-
"""M12 知识库检索纯函数。

不依赖 fastmcp，可直接 import 单测。数据来自 data/guidelines.json 与 data/sops.json。
核心：按调用方身份切三套语料视图（full/popular/child），所有结果强制带 source 出处。
身份由部署注入（A207_CALLER），语料 profile 取自 a207_policy.knowledge_profile —— 模型
不能自选语料视图（P0-1：否则患儿伙伴可自称医生拿到全量专业语料）。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from a207_policy import (
    CLINICIAN_ONLY_FIELDS,
    CLINICIAN_ONLY_HIDDEN_FROM,
    P1_PARENT_HIDDEN_FIELDS,
    enforce_read,
    get_caller,
    knowledge_profile,
    validate_patient_id,
)

# 本包在权限矩阵中的登记名（enforce_* 查表键，唯一事实源在 a207_policy.matrix）。
MCP_NAME = "CKDNutri-content-mcp"

# a207_policy 的 profile 名 → 本包语料字段名（data/guidelines.json 的条目键）。
# 角色→profile 的判定在 policy，本表只做「profile → 语料字段」的数据映射。
_PROFILE_VIEW = {
    "full": "full",
    "plain_language": "popular",
    "child_safe": "child",
}
# 未登记 profile 一律降级到最受限语料（fail-closed，宁可少给不可多给）。
_FALLBACK_VIEW = "child"
# SOP 缺 child 字段时的回退文案（fail-closed：绝不把完整临床处置下发给患儿）。
_CHILD_SOP_FALLBACK = "该 SOP 仅向临床角色提供；如有不适请告知家长或医生。"

_GUIDE_PATH = os.path.join(os.path.dirname(__file__), "data", "guidelines.json")
_SOP_PATH = os.path.join(os.path.dirname(__file__), "data", "sops.json")
_GUIDES: Optional[Dict[str, Any]] = None
_SOPS: Optional[Dict[str, Any]] = None
# BUG-66 后补（2026-08-12）：跨文件 id 唯一性校验结果缓存（get_citation 入口一次性校验）
_CROSS_IDS_CHECKED = False
# S3（2026-08-12 五包审查）：懒加载并发锁（double-checked locking，对齐 assessment
# _RULES_LOCK）——FastMCP/多 worker 线程并发首次调用时防重复 I/O 与重复 JSON 解析。
_GUIDES_LOCK = threading.Lock()
_SOPS_LOCK = threading.Lock()


def _validate_cross_file_ids() -> None:
    """指南与 SOP 跨文件 id 唯一性校验（fail-closed，get_citation 入口调用）。

    get_citation 按"先指南后 SOP"顺序解析引用，若两文件 id 重叠，SOP 条目会被指南
    遮蔽、永远匹配不到，引用解析不稳定。当前数据前缀已隔离（指南 KDIGO2024-/
    PRNT2020-/CHINA2023-/GROWTH2025-，SOP SOP-），此校验防御未来引入冲突 id。
    注：不能放在 _load_guides/_load_sops 内部（会触发互相递归加载，_GUIDES/_SOPS
    未就绪时无限递归），放在 get_citation 入口做一次性校验并缓存结果。
    """
    global _CROSS_IDS_CHECKED
    if _CROSS_IDS_CHECKED:
        return
    guides = _load_guides()
    sops = _load_sops()
    overlaps = {e["id"] for e in guides["entries"]} & {s["id"] for s in sops["sops"]}
    if overlaps:
        raise ValueError(
            f"指南与 SOP 条目 id 冲突：{sorted(overlaps)}，拒绝加载"
            f"（get_citation 按指南优先解析，需跨文件 id 唯一）")
    _CROSS_IDS_CHECKED = True


def _load_guides() -> Dict[str, Any]:
    global _GUIDES
    if _GUIDES is None:
        with _GUIDES_LOCK:
            if _GUIDES is None:  # S3：double-checked locking（对齐 assessment _RULES_LOCK）
                with open(_GUIDE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # BUG-62 后补（2026-08-12）：顶层 'entries' 键校验，对齐 _load_sops 的 'sops' 校验——
                # 此前 data["entries"] 直接索引，缺键/非列表在加载期抛 KeyError（虽已归 INTERNAL_ERROR，
                # 但加载期显式 ValueError 更规范）。
                if not isinstance(data.get("entries"), list):
                    raise ValueError("指南数据结构损坏：缺少 'entries' 列表，拒绝加载")
                # OD-014（P2-4）：加载时校验语料视图字段完整性——缺任一视图字段直接报错，
                # 而不是运行时回退到 full（fail-closed：宁可少给不可多给）。
                # BUG-62：补基础元数据键校验（id/title/source/strength/evidence）——
                # search_guideline/get_citation 直接访问这些键，缺键会在运行时 KeyError；
                # "set" 经 .get 可选访问（guideline_set 过滤），不强制。
                for e in data["entries"]:
                    # BUG-63（2026-08-12）：元素层 dict 校验——null/非 dict 混入时 e.get 抛
                    # AttributeError，加载期显式拒绝（fail-closed）。
                    if not isinstance(e, dict):
                        raise ValueError(f"指南 entries 含非字典元素：{e!r}，拒绝加载")
                    missing = [k for k in ("id", "title", "source", "strength", "evidence",
                                           "full", "popular", "child")
                               if not str(e.get(k) or "").strip()]
                    if missing:
                        raise ValueError(
                            f"指南条目 {e.get('id', '?')} 缺少必填键 {missing}，"
                            f"拒绝加载（fail-closed：防止家长/患儿回退到 full 临床语料）")
                # BUG-66（2026-08-12）：文件内 id 唯一性校验——get_citation 按 id 查引用，
                # 重复 id 会让引用解析返回首个匹配（不明确）；当前数据前缀已隔离（KDIGO2024-/
                # PRNT2020-/SOP-），此校验为防未来数据引入重复 id 的 fail-closed 防御。
                seen_ids: set[str] = set()
                for e in data["entries"]:
                    eid = str(e.get("id") or "")
                    if eid in seen_ids:
                        raise ValueError(f"指南条目 id 重复：{eid!r}，拒绝加载（get_citation 需 id 唯一）")
                    seen_ids.add(eid)
                _GUIDES = data
    return _GUIDES


def _load_sops() -> Dict[str, Any]:
    global _SOPS
    if _SOPS is None:
        with _SOPS_LOCK:
            if _SOPS is None:  # S3：double-checked locking（对齐 assessment _RULES_LOCK）
                with open(_SOP_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # BUG-56（2026-08-12）：参照 _load_guides 做加载时结构校验（fail-closed）——
                # 缺必填键直接拒绝加载，避免检索期运行 KeyError。
                # BUG-62 后补（2026-08-12）：顶层 'sops' 键缺失/非列表也要拒绝——此前
                # data.get("sops", []) 对缺键静默返回 []，随后 search_sop 的 sops["sops"]
                # 运行时 KeyError；非列表（如 dict）会迭代出字符串键导致 AttributeError。
                if not isinstance(data.get("sops"), list):
                    raise ValueError("SOP 数据结构损坏：缺少 'sops' 列表，拒绝加载")
                for s in data["sops"]:
                    # BUG-63（2026-08-12）：元素层 dict 校验（对齐 _load_guides）——null/非 dict
                    # 混入时 s.get 抛 AttributeError。
                    if not isinstance(s, dict):
                        raise ValueError(f"SOP sops 含非字典元素：{s!r}，拒绝加载")
                    missing = [k for k in ("id", "title", "content", "source")
                               if not str(s.get(k) or "").strip()]
                    if missing:
                        raise ValueError(
                            f"SOP 条目 {s.get('id', '?')} 缺少必填键 {missing}，拒绝加载"
                            f"（fail-closed：防止检索期 KeyError / 下发不完整处置）")
                # BUG-66（2026-08-12）：文件内 id 唯一性校验（与 _load_guides 同口径）
                seen_ids: set[str] = set()
                for s in data["sops"]:
                    sid = str(s.get("id") or "")
                    if sid in seen_ids:
                        raise ValueError(f"SOP 条目 id 重复：{sid!r}，拒绝加载（get_citation 需 id 唯一）")
                    seen_ids.add(sid)
                _SOPS = data
    return _SOPS


def _view_for_caller(caller: str) -> str:
    """按调用方身份取语料视图：profile 由 a207_policy 判定，本包只做字段映射。"""
    return _PROFILE_VIEW.get(knowledge_profile(caller), _FALLBACK_VIEW)


def _match(text: str, query: str) -> bool:
    # BUG-56（2026-08-12）：空查询/全空格不再恒真匹配（"" in text 恒 True 会返回全量语料）。
    # BUG-62（2026-08-12）：q 必须 lower()——上轮修复去掉原 .lower() 引入回归，
    # "KDIGO"/"SOP"/"CKD"/"PRNT" 等大写关键词对 lower 后的文本恒 False，检索完全失效。
    q = (query or "").strip().lower()
    if not q:
        return False
    return q in text.lower()


def _guideline_set_lookup() -> Dict[str, str]:
    """指南 set 合法值（加载期从数据收集，防硬编码漂移）→ 小写名 → 规范名。"""
    guides = _load_guides()
    return {
        str(e.get("set") or "").strip().lower(): str(e.get("set")).strip()
        for e in guides["entries"] if e.get("set")
    }


class InvalidArgumentError(ValueError):
    """客户端入参错误（CT-B2/B4 修复，2026-08-14）。

    与数据文件加载期 ValueError（_load_guides/_load_sops 的 fail-closed，服务端数据
    问题→INTERNAL_ERROR）区分：本异常由**调用方入参**触发（guideline_set 非法、
    limit 非法、query 非字符串等），server 层归 INVALID_INPUT 而非 INTERNAL_ERROR，
    避免把客户端错误误导成"内部数据错误"。
    """


def _validate_guideline_set(guideline_set: Optional[str]) -> Optional[str]:
    """guideline_set 校验 + 大小写容错（四审，2026-08-12）。

    - 非字符串/空串 → InvalidArgumentError（fail-closed，防 TypeError 冒泡归
      INTERNAL_ERROR；CT-B2 修复后归 INVALID_INPUT）；
    - 大小写容错：用户传 "kdigo2024"/"CHINA2023" 归一化到数据规范名（此前严格
      区分大小写，"kdigo2024" 静默返回空结果，无任何提示）；
    - 非法值 → InvalidArgumentError 显式报错并列出可用集合（此前静默返回 count=0，
      LLM 会误以为"该集无内容"）。
    """
    if guideline_set is None:
        return None
    if not isinstance(guideline_set, str) or not guideline_set.strip():
        raise InvalidArgumentError("guideline_set 必须为非空字符串")
    lookup = _guideline_set_lookup()
    canon = lookup.get(guideline_set.strip().lower())
    if canon is None:
        raise InvalidArgumentError(
            f"guideline_set={guideline_set!r} 非法，可用：{sorted(set(lookup.values()))}")
    return canon


def _validate_limit(limit: Any) -> int:
    """统一 limit 入参校验：int（非 bool）且 ≥1，非法一律 InvalidArgumentError。

    P1-1（2026-08-18）：search_guideline/search_sop 此前对 limit **双重校验且异常
    类型不一致**（入口抛 InvalidArgumentError、命中统计处又抛裸 ValueError，历史
    打补丁残留）——统一收敛到本函数单一实现，杜绝两处校验漂移。
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise InvalidArgumentError(
            f"limit 必须为 ≥1 的整数，收到：{limit!r}")
    return limit


def search_guideline(query: str, guideline_set: Optional[str] = None,
                     limit: int = 20) -> Dict[str, Any]:
    """检索指南/共识条文。按调用方身份切语料视图；所有结果带 source 出处。

    语料视图由部署注入的身份决定（doctor_assistant/nutritionist/risk_warning=全量 full；
    parent_assistant=popular 通俗；child_companion=child 科普），调用方不可自选。
    guideline_set: 可选过滤 KDIGO2024/PRNT2020/China2023/Growth2025（大小写不敏感，
    非法值显式报错）。空 query 返回空结果并提示（不执行检索）。
    limit（P2 修复 2026-08-13）：结果上限钳制（默认 20、上限 100）——指南库数百条
    全量命中会灌爆 LLM 上下文，超限截断并标注 truncated。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    # CT-B2/B4 修复（2026-08-14）：query 类型校验（此前 keyword=123 → query.strip()
    # AttributeError 归 INTERNAL_ERROR 误导排障）+ limit 入参校验（非法归
    # INVALID_INPUT 而非 INTERNAL_ERROR）
    if not isinstance(query, str):
        raise InvalidArgumentError(f"query 必须为字符串，收到 {type(query).__name__}")
    # P1-1（2026-08-18）：limit 统一走 _validate_limit（单一实现，不再双校验双异常）
    limit = _validate_limit(limit)
    # 四审（2026-08-12）：guideline_set 校验（大小写容错 + 枚举报错）
    guideline_set = _validate_guideline_set(guideline_set)
    guides = _load_guides()
    view = _view_for_caller(caller)
    # 四审（2026-08-12）：空关键词显式提示——此前返回 count=0 无解释，LLM 无法区分
    # "无匹配"与"没给关键词"。
    if not (query or "").strip():
        return {"ok": True, "data": {
            "query": query, "role": caller, "view": view, "count": 0,
            # P2-1（2026-08-18）：空分支补 returned_count（与正常分支 Schema 一致，
            # 编排层无需区分空/非空两种形状）
            "returned_count": 0, "results": [],
            # CT-B1（2026-08-14）：空分支补 truncated（与正常分支信封键一致，
            # 编排层无需区分空/非空两种形状）
            "truncated": False,
            "note": "查询关键词为空，未执行检索；请提供有效关键词。"}}
    out = []
    for e in guides["entries"]:
        if guideline_set and e.get("set") != guideline_set:
            continue
        # BUG-56（2026-08-12）：① tags 用规范 join 而非 __str__；② hay 只拼当前视图可见
        # 字段（title+tags+set+视图正文），消除"幻影匹配"——非临床角色不再因 full 临床文案命中，
        # 命中理由与返回内容一致；set（如 KDIGO2024）在结果中可见，纳入检索（BUG-62）。
        tags = " ".join(str(t) for t in (e.get("tags") or []))
        hay = " ".join([e["title"], tags, str(e.get("set") or ""), e.get(view, "")])
        if not _match(hay, query):
            continue
        out.append({
            "id": e["id"],
            "title": e["title"],
            # BUG-62 后补（2026-08-12）：set 在加载校验中本就是可选键（.get 访问），
            # 结果字典却用 e["set"] 直接索引——缺 set 的条目命中时 KeyError，改为 .get。
            "set": e.get("set"),
            # BUG-66 后补 ❷（2026-08-12）：补 tags——hay 匹配域含 tags（如"儿科"命中），
            # 结果不含 tags 时用户无法确认为何被召回，降低可解释性。
            "tags": e.get("tags"),
            "strength": e["strength"],
            "evidence": e["evidence"],
            # OD-014（P2-4）：视图缺失不再回退 full（fail-closed）。
            # _load_guides 已保证 full/popular/child 齐全，此处仅防御性取 ""。
            "text": e.get(view, ""),
            "source": e["source"],
        })
    # S1（2026-08-12 五包审查）：统一 {ok, data} 信封——此前扁平结构
    # （{"query", "role", "view", "count", "results"}）与其余四包契约分裂，
    # 编排层无法统一按 ok/data 解析。数据形状不变，仅包信封。
    # P2 修复（2026-08-13）：limit 钳制——指南库命中可能数百条，全量进上下文
    # 浪费 token；默认 20、上限 100，超限截断并标注 truncated=true。
    # P1-1（2026-08-18）：入口 _validate_limit 已校验，此处仅钳制上限（不再重复抛异常）。
    limit = min(limit, 100)
    truncated = len(out) > limit
    return {"ok": True, "data": {
        "query": query, "role": caller, "view": view,
        # P1-4（2026-08-18）：count=**命中总数**、returned_count=实际截取返回数——
        # 此前仅 count 一处，调用方易误解为"实际返回条数"（limit 截断时 count 是
        # 全量命中数）。新增 returned_count 明确语义，count 保留兼容既有消费者。
        "count": len(out), "returned_count": min(len(out), limit),
        "results": out[:limit],
        "truncated": truncated,
        "note": f"命中 {len(out)} 条，已截断返回前 {limit} 条（limit={limit}）" if truncated else None,
    }}


def search_sop(query: str, limit: int = 20) -> Dict[str, Any]:
    """检索院内 SOP。按调用方身份切语料视图：非临床角色只返回安全/通俗版，不暴露完整临床处置。

    语料视图由部署注入的身份决定（与 search_guideline 一致）。BUG-22 修复（2026-08-12）：
    此前仅 view == "child" 返回安全版，家长（plain_language→popular）会落到 else 分支拿到
    s["content"]（完整临床处置步骤，如高钾抢救流程）——已改为 fail-closed：
    仅 full 视图返回完整 content；其余视图优先取对应字段（popular/child），
    缺字段一律回退到 child 安全版（绝不把完整临床处置下发给非临床角色）。
    limit（P2 修复 2026-08-13）：结果上限钳制（默认 20、上限 100，超限截断标注）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    # CT-B2/B4 修复（2026-08-14）：query 类型 + limit 入参校验（归 INVALID_INPUT）
    if not isinstance(query, str):
        raise InvalidArgumentError(f"query 必须为字符串，收到 {type(query).__name__}")
    # P1-1（2026-08-18）：limit 统一走 _validate_limit（单一实现，不再双校验双异常）
    limit = _validate_limit(limit)
    view = _view_for_caller(caller)
    sops = _load_sops()
    # 四审（2026-08-12）：空关键词显式提示（与 search_guideline 同口径）
    if not (query or "").strip():
        return {"ok": True, "data": {
            "query": query, "count": 0,
            # P2-1（2026-08-18）：空分支补 returned_count（与正常分支 Schema 一致）
            "returned_count": 0, "results": [],
            # CT-B1（2026-08-14）：补 view（与 search_guideline 信封一致，编排层统一
            # 取 data.view；此前 SOP 的 view 只出现在结果条目层，形状分裂）
            "view": view, "truncated": False,
            "note": "查询关键词为空，未执行检索；请提供有效关键词。"}}
    out = []
    for s in sops["sops"]:
        # BUG-56（2026-08-12）：hay 只拼当前视图可见文本（消除幻影匹配）；tags 规范 join；
        # child 显式 null 时以 (s.get("child") or "") 兜底，避免 " ".join 抛 TypeError。
        tags = " ".join(str(t) for t in (s.get("tags") or []))
        body = s["content"] if view == "full" else (s.get(view) or s.get("child") or "")
        hay = " ".join([s["title"], tags, body])
        if _match(hay, query):
            if view == "full":
                content = s["content"]
            else:
                # BUG-22：非 full 视图优先取对应视图字段，缺则回退 child 安全版（fail-closed）
                content = (s.get(view) or s.get("child")) or _CHILD_SOP_FALLBACK
            out.append({
                "id": s["id"],
                "title": s["title"],
                # BUG-66 后补 ❷（2026-08-12）：补 tags（与 search_guideline 同口径，
                # hay 匹配域含 tags 时结果需可解释）
                "tags": s.get("tags"),
                "content": content,
                "source": s["source"],
                "view": view,
            })
    # S1（2026-08-12 五包审查）：统一 {ok, data} 信封（与 search_guideline 同口径）
    # P2 修复（2026-08-13）：limit 钳制（默认 20、上限 100，超限截断标注 truncated）
    # P1-1（2026-08-18）：入口 _validate_limit 已校验，此处仅钳制上限。
    limit = min(limit, 100)
    truncated = len(out) > limit
    return {"ok": True, "data": {"query": query,
                                 # P1-4（2026-08-18）：count=命中总数、returned_count=实际返回数
                                 "count": len(out), "returned_count": min(len(out), limit),
                                 "results": out[:limit],
                                 "truncated": truncated,
                                 "note": (f"命中 {len(out)} 条，已截断返回前 {limit} 条"
                                          f"（limit={limit}）" if truncated else None)}}


def get_citation(ref_id: str) -> Dict[str, Any]:
    """生成规范引用串。支持指南条目 id 与 SOP id。

    BUG-66 后补（2026-08-12）：入口先做跨文件 id 唯一性校验——get_citation 按
    "先指南后 SOP"解析，两文件 id 重叠会让 SOP 被遮蔽（校验缓存避免重复开销）。
    视图说明：title/source/strength/evidence 为元数据，对所有读权角色可见（与
    search_guideline/search_sop 返回一致）；视图裁剪只作用于正文 text/content。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    # P1-3（2026-08-18）：ref_id 类型 + 非空校验——此前 None/非 str 静默走循环
    # 比较返回 NOT_FOUND（"未找到"而非"参数错误"，语义误导）；空串同样无意义。
    if not isinstance(ref_id, str) or not ref_id.strip():
        raise InvalidArgumentError(
            f"ref_id 必须为非空字符串，收到：{ref_id!r}")
    _validate_cross_file_ids()
    guides = _load_guides()
    for e in guides["entries"]:
        if e["id"] == ref_id:
            citation = (f"[{e['id']}] {e['title']}. {e['source']} "
                        f"（推荐强度：{e['strength']}；证据级别：{e['evidence']}）")
            return {"ok": True, "data": {"ref_id": ref_id, "citation": citation,
                                         "source": e["source"], "strength": e["strength"],
                                         "evidence": e["evidence"]}}
    sops = _load_sops()
    for s in sops["sops"]:
        if s["id"] == ref_id:
            citation = f"[{s['id']}] {s['title']}. {s['source']}"
            return {"ok": True, "data": {"ref_id": ref_id, "citation": citation,
                                         "source": s["source"]}}
    # S1（2026-08-12 五包审查）：统一 {ok, data} 信封——未找到由扁平 error 字段改为
    # 标准 {ok: false, error: NOT_FOUND, detail} 失败信封（编排层可统一按 ok 分支）。
    return {"ok": False, "error": "NOT_FOUND",
            "detail": f"未找到引用 ID：{ref_id}", "ref_id": ref_id}


def _self_test_refs() -> List[str]:
    """返回所有可被 get_citation 解析的 id（供校验出处完整性）。"""
    guides = _load_guides()
    sops = _load_sops()
    return [e["id"] for e in guides["entries"]] + [s["id"] for s in sops["sops"]]


# ---- M9: report generation helpers (recovered from a207-report-mcp) ----

_RISK_TO_STATUS = {
    "L1": "critical", "high": "critical", "critical": "critical",
    "L2": "caution", "medium": "caution", "caution": "caution",
    # F6（2026-08-17，十二审）：L3 是**最低风险档**（rules.json:9 "L3=低风险：
    # 常规随访关注"，_LEVEL_RANK={"L3":1}）——此前映射 caution 与 L2（中风险）
    # 同标"需关注"，低风险患儿与高危同色、告警信号被稀释（过报）。改 stable
    # （常规随访=稳定），语义对齐 rules.json 与 _PEW_ORDER（l3 排名 1 < l2 2）。
    "L3": "stable",
    "L0": "stable", "low": "stable", "none": "stable", "stable": "stable",
}
# 💭（2026-08-12 五包审查）：状态中文映射前移至常量区（此前定义在使用点之后，
# 依赖模块级运行时查找，可读性差）。
_STATUS_CN = {"stable": "稳定", "caution": "需关注", "critical": "紧急"}


# BUG-62/BUG-63（2026-08-12）：等级阶梯——L0(0) < L3(1) < L2(2) < L1(3)，
# 语义对齐风险严重度；词级同义（low/medium/high/critical/caution/stable）。
_PEW_ORDER = {
    "none": 0, "l0": 0, "low": 0, "stable": 0,
    "l3": 1,
    "l2": 2, "medium": 2, "caution": 2,
    "l1": 3, "high": 3, "critical": 3,
}


def _is_number(value: Any) -> bool:
    """P2-2（2026-08-18）：有限数值判定——bool 是 int 子类（isinstance(True,
    (int,float)) 为 True）、NaN/Inf 属于 float 但比较语义异常（NaN < 50 恒 False），
    二者此前都能通过 isinstance 检查被当有效数值（False→0% 触发误报预警、NaN
    静默不触发）。统一排除 bool 与非有限值。
    """
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value))


def _parse_pew_date(value: Any) -> Optional[datetime]:
    """解析 PEW 历史日期为 datetime；无效/缺失返回 None。

    上游 M3 契约=ISO 升序（BUG-60 已统一写入归一化），此处防御性解析——"/"、"." 分隔
    转 "-" 后 fromisoformat；无法解析（如 "Yesterday"、非零填充 "2023-6-1"）返回 None，
    由调用方决定剔除（fail-closed：无法可靠定位时间线的数据点不参与趋势计算）。

    P5（2026-08-15）：**先试原样 fromisoformat**——旧实现直接 .replace(".", "-")
    会破坏 ISO 微秒时间戳（"2024-01-10T08:30:00.123456" → "…00-123456" 解析失败
    → 返回 None → 该点被剔除，微秒数据点全部丢失）。仅原样失败才做分隔符替换
    （兼容 "2024/01/10"、"2024.01.10"）。
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("/", "-").replace(".", "-"))
        except ValueError:
            return None
    # P1-1（2026-08-18）：统一转 **UTC aware**——fromisoformat 对无时区串
    # （如 "2026-08-18"）生成 naive、带时区串（如 "2026-08-18T10:00:00+08:00"）
    # 生成 aware，两者混排 `dated.sort` 直接抛 TypeError: can't compare
    # offset-naive and offset-aware datetimes（报告生成全盘失败）。统一：
    # naive → replace(tzinfo=utc)、aware → astimezone(utc)，保证可比且跨时区同刻同值。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _pew_trend_info(pew_history: list[dict]) -> Dict[str, Any]:
    """PEW 趋势计算 + 数据质量透明化（BUG-66 后补 ❶，2026-08-12）。

    返回 {trend, valid_count, total_count}：trend 基于日期有效点计算，valid_count/
    total_count 供报告层明示"趋势基于多少有效点"——避免 10 条记录仅 2 条有效时
    报告显示"历史点数：10 趋势：stable"掩盖数据质量问题。
    """
    # P1-2（2026-08-18）：total_count 用**真实分母** len(pew_history or [])（含
    # 非 dict 非法记录）——此前 `pts = [p ... if isinstance(p, dict)]` 后
    # total_count=len(pts)，输入 4 条（2 有效 + 2 非法）显示 valid 2/total 2
    # （"100% 有效"），静默掩盖脏数据。非 dict 记录不参与趋势但必须计入总数，
    # 供报告层透明提示"X 条非法记录未参与计算"。
    total_count = len(pew_history or [])
    pts = [p for p in (pew_history or []) if isinstance(p, dict)]
    dated = []
    for p in pts:
        dt = _parse_pew_date(p.get("date"))
        if dt is None:
            continue
        # BUG-66 后补（2026-08-12）：level 未知值同样剔除——_PEW_ORDER.get(level, 0)
        # 会把 "unknown"/拼写错误静默映射为 0(low)，high→unknown 被误判"改善"掩盖恶化；
        # 与日期无效同理，无法可靠判定严重度的点不参与趋势（fail-closed）。
        # M（2026-08-16，第七轮审查）：**缺 level 键也剔除**——此前 p.get("level",
        # "low") 对缺失键默认 low，数据不完整的点被当"轻"参与趋势（与 fail-closed
        # 意图相悖）；显式 None 判定。
        lv = p.get("level")
        if lv is None or str(lv).strip().lower() not in _PEW_ORDER:
            continue
        dated.append((dt, p))
    if len(dated) < 2:
        return {"trend": "no_data", "valid_count": len(dated), "total_count": total_count,
                "current_level": None, "historical_peak": None}
    dated.sort(key=lambda x: x[0])
    fo = _PEW_ORDER[str(dated[0][1].get("level")).strip().lower()]
    lo = _PEW_ORDER[str(dated[-1][1].get("level")).strip().lower()]
    # P0-2（2026-08-18）：**解耦"当前风险 / 历史最高 / 发展趋势"三概念**——
    # 此前趋势 = 首点 vs 全程最高严重度（peak=max）：患者历史出现过 L1（哪怕早已
    # 完全恢复）也永远 worsening，最终状态被历史峰值错误抬高（"历史病情永久恶化"
    # 误判，修复前 M 注释自述行为）。现：
    #   - trend：仅首尾比较（近期变化，lo vs fo）；
    #   - current_level：当前（最新有效点）等级，供报告透明展示；
    #   - historical_peak：全程最高等级，**只做透明展示**（历史高风险背景仍可见），
    #     不再直接驱动状态抬升（当前状态由当前风险 + 近期趋势决定）。
    if lo > fo:
        trend = "worsening"
    elif lo < fo:
        trend = "improving"
    else:
        trend = "stable"
    _peak_entry = max(dated, key=lambda x: _PEW_ORDER[str(x[1].get("level")).strip().lower()])
    return {"trend": trend, "valid_count": len(dated), "total_count": total_count,
            "current_level": dated[-1][1].get("level"),
            "historical_peak": _peak_entry[1].get("level")}


def _pew_trend(pew_history: list[dict]) -> str:
    # BUG-62 后补（2026-08-12）：过滤非 dict 元素——pew_history 混入 None 时原
    # p.get("date") 抛 AttributeError；仅防排序 key 不够（非 dict 会排最前，
    # pts[0].get("level") 仍崩），直接过滤最干净。
    # BUG-66（2026-08-12）：显式按 date 升序排序，不依赖数据源隐式顺序——营养包
    # get_pew_history 契约=升序，但若上游传倒序，趋势会完全反转（worsening↔improving）。
    # BUG-66 后补（2026-08-12）：**剔除日期无效的点**——fail-closed：无法定位时间线的
    # 数据点不参与趋势，有效点 <2 返回 no_data 而非给出可能错误的结论。
    # 实现委托 _pew_trend_info（数据质量透明化见其 docstring），此处保持返回 str 契约。
    return _pew_trend_info(pew_history)["trend"]


def _derive_status(risk_level: str, pew_history: list[dict],
                   nutrition_assessment: dict,
                   pew_trend: Optional[str] = None) -> str:
    # BUG-66 后补 ❸（2026-08-12）：可选 pew_trend 参数——generate_patient_report 已
    # 调 _pew_trend_info 算过趋势，传入可避免对小数据列表二次解析排序（性能冗余）。
    # 缺省 None 时内部自算（保持独立调用兼容）。
    # BUG-62（2026-08-12）：risk_level 大小写归一化——"HIGH"/"l1" 等变体若直接
    # _RISK_TO_STATUS.get 失败会静默回退 stable（fail-open 掩盖真实风险）。
    # BUG-66（2026-08-12）：剥离非字母数字字符——"L 1"/"L-1"/"L1!" 等带分隔符变体
    # 归一化后与 "l1" 一致（旧逻辑 "L 1".strip().lower()="l 1" 查不到 → stable 漏报危急）。
    # P5（2026-08-15）：**不可哈希类型 TypeError**——risk_level 为 list/dict 时
    # _RISK_TO_STATUS.get(risk_level)（dict.get 非字符串键）抛 unhashable TypeError
    # → 报告生成整段崩溃。统一用已归一化字符串 key 查询（str() 转换兜底），
    # 不再用原始 risk_level 作 dict 键。
    rl = re.sub(r"[^a-z0-9]", "", str(risk_level or "").lower())
    key = rl.upper() if rl in ("l0", "l1", "l2", "l3") else rl
    # P0-1（2026-08-18）：未知/非法 risk_level **严禁回退 stable**——医疗 Fail-Open：
    # 数据异常时显示"稳定"会掩盖真实风险（如 "L9"/垃圾串被展示为"稳定"）。
    # 未命中映射 → "caution"（需关注，宁可多报不漏报），配合调用方 risk.valid=false
    # 透明标注（generate_patient_report 六审逻辑）双保险。
    base = _RISK_TO_STATUS.get(key, "caution")
    # PEW 恶化 → 阶梯提升（BUG-66 后补 ❷，2026-08-12）：
    # stable→caution、caution→critical——此前仅 stable 提升，M8 判 L2(caution) 且 PEW
    # 恶化至危急时报告停留在"需关注"，低估风险。三档状态中 caution 上调一档即 critical
    # （无中间档），按 fail-safe 方向补全；critical 恶化保持 critical（已是天花板）。
    _trend = pew_trend if pew_trend is not None else _pew_trend(pew_history)
    if _trend == "worsening":
        if base == "stable":
            base = "caution"
        elif base == "caution":
            base = "critical"
    # 营养摄入达成率过低（<50%）→ 至少 caution
    # BUG-62：显式 None/dict 处理——{"intake": None} 时旧链式 .get 抛 AttributeError
    # 被宽 except 吞掉（行为虽对），改显式后不再掩盖其它异常
    # F-2（2026-08-15）：**键名兼容**——P2 assess_intake_vs_target 实际输出
    # `data.energy.achievement_pct`，旧链读 `intake.achievement.energy_pct` 永远取不到
    # → 摄入 <50% 的能量严重不足患儿报告整体仍 "stable"（静默漏报，已实测）。兼容
    # 三种结构：① P2 实际 data.energy.achievement_pct；② 直接 energy.achievement_pct；
    # ③ 旧契约 intake.achievement.energy_pct。
    energy_pct: Any = 100
    if isinstance(nutrition_assessment, dict):
        _d = nutrition_assessment.get("data")
        # P2-2（2026-08-18）：isinstance((int,float)) 会放过 bool（True 当 1）与
        # NaN/Inf（NaN<50 恒 False 静默不触发）——统一 _is_number 排除。
        if isinstance(_d, dict) and isinstance(_d.get("energy"), dict) \
                and _is_number(_d["energy"].get("achievement_pct")):
            energy_pct = _d["energy"]["achievement_pct"]
        elif isinstance(nutrition_assessment.get("energy"), dict) \
                and _is_number(nutrition_assessment["energy"].get("achievement_pct")):
            energy_pct = nutrition_assessment["energy"]["achievement_pct"]
        else:
            intake = nutrition_assessment.get("intake")
            ach = (intake.get("achievement") or {}) if isinstance(intake, dict) else {}
            energy_pct = ach.get("energy_pct", 100)
        if _is_number(energy_pct) and energy_pct < 50 and base == "stable":
            base = "caution"
    return base


def _section(title: str, body: str) -> str:
    return f"### {title}\n{body}\n"


# 仅临床角色可见字段（MX-1 字段可见性边界）：单一事实源直接引用 a207_policy.CLINICIAN_ONLY_FIELDS，
# 不再在包内维护副本（消除 OD-011/OD-013 指出的副本漂移）。
# D2 修复（2026-08-14）：并集 P1_PARENT_HIDDEN_FIELDS——P1 家长档案视图额外隐藏的
# 档案级键（medical_record_no / dialysis_detail / bsa_m2 等）也一并剥除。当前编排层
# 未传 P1 全量档案 dict（不可达），但若未来复用 P1 档案数据进报告，无此并集即透出。
_CLINICIAN_ONLY: frozenset[str] = CLINICIAN_ONLY_FIELDS | P1_PARENT_HIDDEN_FIELDS

# 非临床角色（家长/患儿）绝不可见 CLINICIAN_ONLY_FIELDS；角色集合单一事实源在
# a207_policy.CLINICIAN_ONLY_HIDDEN_FROM（C3 禁止包内硬编码）。
_NON_CLINICAL_MASKED: frozenset[str] = CLINICIAN_ONLY_HIDDEN_FROM


def _mask_clinician_fields(value: Any) -> Any:
    """递归剔除仅临床可见字段，用于生成对外可读文案 / 受限视图。"""
    if isinstance(value, dict):
        return {k: _mask_clinician_fields(v) for k, v in value.items()
                if k not in _CLINICIAN_ONLY}
    if isinstance(value, list):
        return [_mask_clinician_fields(v) for v in value]
    return value


def _ensure_dict(value: Any, name: str) -> dict:
    """P1-2（2026-08-18）：入参类型检查——`value or {}` 无法拦截**非空**错误类型
    （如非空 list/str 保持原值），后续 .get() 抛 AttributeError 被全局捕获掩盖成
    内部错误。None → {}（空数据处理），非 dict → InvalidArgumentError（显式）。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidArgumentError(f"{name} 必须为对象，收到 {type(value).__name__}")
    return value


def _ensure_list(value: Any, name: str) -> list:
    """P1-2（2026-08-18）：pew_history 入口类型检查——str 会被逐字符解析、
    dict 被静默吞掉，显式要求 list；None → []。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidArgumentError(f"{name} 必须为列表，收到 {type(value).__name__}")
    return value


# ---- M9: report generation ----
def generate_patient_report(patient_id: str, demographics: dict, lab_summary: dict,
                            nutrition_assessment: dict, followup_summary: dict,
                            pew_history: list[dict] | None, risk_level: str) -> dict[str, Any]:
    """生成统一患者报告。

    :param patient_id: 患者标识
    :param demographics: {age_years, sex, ckd_stage, dialysis_mode}
    :param lab_summary: 来自 M2(LIS) 的最新化验摘要
    :param nutrition_assessment: 来自 M3(评估)：PRNT 目标 / 摄入达成率 / PEW
    :param followup_summary: 来自 M4(随访)：最近记录 / 计划 / 依从性
    :param pew_history: 来自 M3 get_pew_history（ADR-007：存储归属 M3）
    :param risk_level: 来自 M8(风险规则) 的等级（L0-L3 / low-high）
    :return: {sections, summary_markdown, overall_status, generated_at}
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（畸形 id 不进报告生成）
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
    # BUG-62 后补（2026-08-12）：顶层防空——编排层直调可能传 None（fastmcp 工具层对
    # 必填 dict 形参可能放行显式 null），demographics=None 会在下方 .get() 抛
    # AttributeError。统一 `or {}` 兜底，None/空按无数据处理。
    # P1-2（2026-08-18）：`or {}` 无法拦截**非空**错误类型（list/str），显式类型
    # 校验（_ensure_dict/_ensure_list）——类型不符抛 InvalidArgumentError（归
    # INVALID_INPUT），不静默吞、不被全局捕获掩盖成内部错误。
    demographics = _ensure_dict(demographics, "demographics")
    lab_summary = _ensure_dict(lab_summary, "lab_summary")
    nutrition_assessment = _ensure_dict(nutrition_assessment, "nutrition_assessment")
    followup_summary = _ensure_dict(followup_summary, "followup_summary")
    ph = _ensure_list(pew_history, "pew_history")
    # BUG-66 后补（2026-08-12）：透明化——trend 仅基于日期有效点计算，count 若用原始
    # len(ph) 会掩盖"10 条记录仅 2 条有效"的数据质量问题；用 _pew_trend_info 同时
    # 暴露 valid_count（参与计算点数）与 total_count（原始记录数）。
    # BUG-66 后补 ❸（2026-08-12）：先算 pew_info 一次，trend 复用于 _derive_status，
    # 避免 PEW 日期二次解析排序（性能冗余）。
    pew_info = _pew_trend_info(ph)
    trend = pew_info["trend"]
    overall_status = _derive_status(risk_level, ph, nutrition_assessment, pew_trend=trend)
    # 六审（2026-08-13）：未知风险等级显式标注——_derive_status 对非法 risk_level
    # 静默归 stable（fail-open 掩盖真实风险，如 "L9" 会被展示为"稳定"），报告必须
    # 透明化：未知等级标注 risk.valid=false 并在 markdown 中提示核对上游。
    rl_norm = re.sub(r"[^a-z0-9]", "", str(risk_level or "").lower())
    risk_unknown = rl_norm not in ("l0", "l1", "l2", "l3", "low", "medium", "high",
                                   "critical", "caution", "stable", "none")
    # F7（2026-08-17，十二审）：**营养评估未评估透明化**——空 nutrition dict 时
    # _derive_status 的 energy_pct 默认 100 永不升级（fail-open），且报告无任何提示
    # （与 risk.valid 不对称）。现标注 nutrition_valid=false：nutrition_assessment 为
    # 空/无 data.energy.achievement_pct 时表示摄入数据缺失，报告显式提示"未评估"，
    # 不把"没数据"当"达成 100%"。
    _na = nutrition_assessment if isinstance(nutrition_assessment, dict) else {}
    _na_data = _na.get("data") if isinstance(_na.get("data"), dict) else {}
    nutrition_valid = (
        isinstance(_na_data.get("energy"), dict)
        and _is_number(_na_data["energy"].get("achievement_pct"))) \
        or (
            isinstance(_na.get("energy"), dict)
            and _is_number(_na["energy"].get("achievement_pct"))) \
        or (isinstance(_na.get("intake"), dict)
            and _is_number((_na["intake"].get("achievement") or {}).get("energy_pct")))
    # MX-1：家长/患儿（非临床角色）拿受限视图 —— sections 与 summary_markdown 一致脱敏，
    # 避免经结构化章节泄露原始化验值（红队 C8：原仅脱敏 summary，sections 仍透出 raw scr/k）。
    mask = caller in _NON_CLINICAL_MASKED

    # ---- 结构化章节 ----
    sections: dict[str, Any] = {
        "patient": {
            "patient_id": patient_id,
            "age_years": demographics.get("age_years"),
            "sex": demographics.get("sex"),
            "ckd_stage": demographics.get("ckd_stage"),
            "dialysis_mode": demographics.get("dialysis_mode"),
        },
        "labs": _mask_clinician_fields(lab_summary) if mask else lab_summary,
        "nutrition": _mask_clinician_fields(nutrition_assessment) if mask else nutrition_assessment,
        "followup": _mask_clinician_fields(followup_summary) if mask else followup_summary,
        "pew_trend": {
            "count": pew_info["valid_count"],
            "total_records": pew_info["total_count"],
            "trend": trend,
            # P0-2（2026-08-18）：当前等级与历史最高等级透明化——趋势已改为"近期
            # 首尾变化"，历史高风险背景不参与状态抬升但不可丢失（医生/家长可见
            # 患者历史出现过的最严重 PEW 等级）。
            "current_level": pew_info["current_level"],
            "historical_peak": pew_info["historical_peak"],
            # M-2（2026-08-16，十一审）：架构语言不进家长上下文——此前 source 硬编码
            # "M3 (ADR-007)"（内部模块编号），家长报告暴露架构语言。改中性描述。
            "source": "PEW 历史（营养评估）",
            "note": (f"趋势基于 {pew_info['valid_count']}/{pew_info['total_count']} 条有效记录"
                     if pew_info["valid_count"] < pew_info["total_count"] else "全部记录有效"),
        },
        "risk": {"level": risk_level,
                 # 六审（2026-08-13）：未知等级显式标注——_derive_status 对非法
                 # risk_level 静默归 stable（fail-open 掩盖真实风险），报告必须透明。
                 "valid": not risk_unknown},
        # F7（2026-08-17）：营养摄入达成率未评估透明化——空 dict/无摄入数据时
        # nutrition_valid=false，报告显式提示（不把"没数据"当"达成 100%"）。
        "nutrition_valid": nutrition_valid,
        "overall_status": overall_status,
    }

    # ---- 文案（markdown）----
    lines = [f"# 患者报告 · {patient_id}", ""]
    d_sex = {"M": "男", "F": "女"}.get(demographics.get("sex"), demographics.get("sex", "?"))
    lines.append(_section(
        "一、基本信息",
        f"- 年龄：{demographics.get('age_years')} 岁　性别：{d_sex}\n"
        f"- CKD 分期：{demographics.get('ckd_stage')}　透析方式：{demographics.get('dialysis_mode')}"))
    lines.append(_section("二、最新化验",
                          _fmt_dict(_mask_clinician_fields(lab_summary) if mask else lab_summary)
                          or "（无）"))
    lines.append(_section("三、营养评估",
                          _fmt_dict(_mask_clinician_fields(nutrition_assessment) if mask
                                    else nutrition_assessment) or "（无）"))
    # F7（2026-08-17）：营养未评估显式提示——空 nutrition dict 时"（无）"不区分
    # "未评估"与"评估无数据"，家长无法判断是否漏评。补一行提示（与 risk.valid=false
    # 的透明口径对称）。
    if not nutrition_valid:
        lines.append("> ⚠ 营养摄入数据未提供（或未评估），摄入达成率不参与整体状态判定；"
                     "请补充 3 日饮食日记后复评。")
    _fu = (_mask_clinician_fields(followup_summary) if mask else followup_summary) or {}
    # BUG-63（2026-08-12）：类型安全提取——records/adherence 非列表时原 [-1] 索引会
    # TypeError；plans 为数值（如 3 表示 3 项计划）时原 len() 也崩，数值按计数处理。
    records = _fu.get("records")
    adherence = _fu.get("adherence")
    records = records if isinstance(records, list) else []
    adherence = adherence if isinstance(adherence, list) else []
    plans = _fu.get("plans")
    if isinstance(plans, (list, tuple, dict, set)):
        plans_count = len(plans)
    elif isinstance(plans, (int, float)) and not isinstance(plans, bool):
        plans_count = int(plans)
    else:
        plans_count = 0
    lines.append(_section(
        "四、随访与依从",
        f"- 最近随访：{_fmt_block(records[-1] if records else {})}\n"
        f"- 依从性：{_fmt_block(adherence[-1] if adherence else {})}\n"
        f"- 进行中计划数：{plans_count}"))
    # BUG-66 后补（2026-08-12）：无效记录数 = total - valid——此前误用 valid_count 显示
    # "N 条无效"，数据质量越差（有效点越少）显示的"无效"反而越少，严重误导可信度判断。
    invalid_count = pew_info["total_count"] - pew_info["valid_count"]
    _pew_hist_note = ""
    if pew_info["historical_peak"] is not None and \
            str(pew_info["historical_peak"]).strip().lower() != \
            str(pew_info.get("current_level") or "").strip().lower():
        # P0-2（2026-08-18）：历史峰值透明展示——趋势不再携带"历史永久恶化"语义，
        # 历史最高等级单独呈现，避免信息丢失。
        _pew_hist_note = (f"\n- 当前等级：{pew_info['current_level']}　"
                          f"历史最高：{pew_info['historical_peak']}")
    lines.append(_section(
        "五、PEW 历史",
        f"- 历史点数：{pew_info['valid_count']}/{pew_info['total_count']}（有效/总数）　趋势：{trend}"
        + _pew_hist_note
        + (f"\n- 提示：{invalid_count} 条记录日期格式无效，未参与趋势计算"
           if pew_info["valid_count"] < pew_info["total_count"] else "")))
    # 六审：未知风险等级在 markdown 中显式提示（不静默展示为"稳定"）
    risk_line = f"- 等级：{risk_level}"
    if risk_unknown:
        risk_line += "（⚠ 无法解析的风险等级，整体状态按保守口径展示，请核对上游评估输出）"
    lines.append(_section("六、风险等级", risk_line))
    lines.append(_section("七、综合结论", f"**整体状态：{_STATUS_CN.get(overall_status, overall_status)}**"))

    # S1（2026-08-12 五包审查）：统一 {ok, data} 信封——此前 {ok, patient_id, ...} 平铺
    # 无 data 包裹，与其余四包契约分裂；编排层可统一按 data 取业务字段。
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            # C3（2026-08-15）：统一 UTC——此前 datetime.now().astimezone() 本地 aware，
            # 跨时区部署报告时间戳漂移（与其他包 recorded_at/created_at UTC 口径不一致）
            "overall_status": overall_status,
            "pew_trend": trend,
            "sections": sections,
            "summary_markdown": "\n".join(lines),
        },
    }


def _md_escape(value: Any) -> str:
    """B2（2026-08-15）：markdown 值转义——化验备注/医生备注等自由文本可含换行或
    markdown 元字符（#、*、`、[、]、| 等），直接插值可篡改报告结构（注入标题/列表/
    代码块/表格）。换行折叠为空格（保持单行条目语义），元字符转义为字面量。"""
    text = str(value if value is not None else "")
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return (text.replace("\\", "\\\\").replace("`", "\\`").replace("*", "\\*")
            .replace("_", "\\_").replace("[", "\\[").replace("]", "\\]")
            .replace("#", "\\#").replace(">", "\\>").replace("|", "\\|"))


def _fmt_dict(d: Any) -> str:
    # BUG-56（2026-08-12）：None 返回 "" 而非 "None"——否则 "None" or "（无）" 会渲染出
    # "二、最新化验：None" 而非预期的 "（无）"。
    # CT-Q1 修复（2026-08-14）：**嵌套 dict/list 递归格式化**——此前 f"- {k}：{v}"
    # 对嵌套结构直接 str(v) → Python repr（{'achievement': {...}}）渲染进家长报告，
    # PII 文本与机器可读 repr 混入 markdown。递归后嵌套键渲染为缩进子列表。
    # B2（2026-08-15）：标量值经 _md_escape 转义（防自由文本注入 markdown 结构）。
    if d is None:
        return ""
    if isinstance(d, dict):
        if not d:
            return ""
        lines = []
        for k, v in d.items():
            if isinstance(v, dict):
                inner = _fmt_dict(v)
                lines.append(f"- {_md_escape(k)}：{inner if inner else '（无）'}")
            elif isinstance(v, (list, tuple)):
                inner = _fmt_list(v)
                lines.append(f"- {_md_escape(k)}：{inner if inner else '（无）'}")
            else:
                lines.append(f"- {_md_escape(k)}：{_md_escape(v)}")
        return "\n".join(lines)
    return _md_escape(d)


def _fmt_list(items: Any) -> str:
    """列表递归渲染：元素为 dict → 递归 _fmt_dict；标量 → 顿号拼接（B2 转义）。"""
    if not items:
        return ""
    parts = []
    for it in items:
        if isinstance(it, dict):
            inner = _fmt_dict(it)
            parts.append(inner if inner else "（无）")
        else:
            parts.append(_md_escape(it))
    return "；".join(parts)


def _fmt_block(d: Any) -> str:
    """渲染为 Markdown 缩进子列表块：空返回"（无）"；有值每行缩进 2 空格。

    BUG-62（2026-08-12）：原 _fmt_dict 直接嵌进 "- 最近随访：{...}" 会生成
    "- 最近随访：- date：..." 的嵌套弹头，Markdown 结构错乱；改为
    "- 最近随访：\\n  - date：...\\n  - note：..." 规范子列表。
    """
    text = _fmt_dict(d)
    if not text:
        return "（无）"
    return "\n" + "\n".join("  " + line for line in text.split("\n"))
