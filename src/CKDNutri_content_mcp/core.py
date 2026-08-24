"""M12 知识库检索纯函数。

不依赖 fastmcp，可直接 import 单测。数据来自 data/guidelines.json 与 data/sops.json。
核心：按调用方身份切三套语料视图（full/popular/child），所有结果强制带 source 出处。
身份由部署注入（A207_CALLER），语料 profile 取自 a207_policy.knowledge_profile —— 模型
不能自选语料视图（P0-1：否则患儿伙伴可自称医生拿到全量专业语料）。
"""
from __future__ import annotations

import html
import json
import math
import os
import threading
from datetime import datetime, timezone
from typing import Any

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
_GUIDES: dict[str, Any] | None = None
_SOPS: dict[str, Any] | None = None
# BUG-66 后补（2026-08-12）：跨文件 id 唯一性校验结果缓存（get_citation 入口一次性校验）
_CROSS_IDS_CHECKED = False
# P1-3（2026-08-18）：校验标志的并发锁——高并发启动时多线程同时触发重复加载/校验
# （_CROSS_IDS_CHECKED 无锁读写非原子），double-checked locking 保证只做一次。
_CROSS_IDS_LOCK = threading.Lock()
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
    with _CROSS_IDS_LOCK:
        # P1-3（2026-08-18）：双重检查——首个线程进入锁后重判，避免排队线程重复校验。
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


def _load_guides() -> dict[str, Any]:
    global _GUIDES
    if _GUIDES is None:
        with _GUIDES_LOCK:
            if _GUIDES is None:  # S3：double-checked locking（对齐 assessment _RULES_LOCK）
                with open(_GUIDE_PATH, encoding="utf-8") as f:
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


def _load_sops() -> dict[str, Any]:
    global _SOPS
    if _SOPS is None:
        with _SOPS_LOCK:
            if _SOPS is None:  # S3：double-checked locking（对齐 assessment _RULES_LOCK）
                with open(_SOP_PATH, encoding="utf-8") as f:
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


def _guideline_set_lookup() -> dict[str, str]:
    """指南 set 合法值（加载期从数据收集，防硬编码漂移）→ 小写名 → 规范名。"""
    guides = _load_guides()
    # 十七审（2026-08-24，C4）：过滤条件由 `if e.get("set")` 改为
    # `if str(e.get("set") or "").strip()`——全空格 "   " 在前者为 truthy，会混入
    # 空字符串键 ""（后续 _validate_guideline_set 报错提示 `可用：['', 'CHINA2023'...]`）。
    return {
        str(e.get("set") or "").strip().lower(): str(e.get("set")).strip()
        for e in guides["entries"] if str(e.get("set") or "").strip()
    }


class InvalidArgumentError(ValueError):
    """客户端入参错误（CT-B2/B4 修复，2026-08-14）。

    与数据文件加载期 ValueError（_load_guides/_load_sops 的 fail-closed，服务端数据
    问题→INTERNAL_ERROR）区分：本异常由**调用方入参**触发（guideline_set 非法、
    limit 非法、query 非字符串等），server 层归 INVALID_INPUT 而非 INTERNAL_ERROR，
    避免把客户端错误误导成"内部数据错误"。
    """


def _validate_guideline_set(guideline_set: str | None) -> str | None:
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


def search_guideline(query: str, guideline_set: str | None = None,
                     limit: int = 20) -> dict[str, Any]:
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
    raw_limit = limit  # P2-1（2026-08-18）：保留调用方原始请求值（语义透明字段）
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
            # 十七审（2026-08-24，C3）：空分支补齐 requested_limit/effective_limit，
            # 与正常分支信封结构 100% 对齐——上层网关统一读取 data.effective_limit
            # 时不再因空查询分支 KeyError。effective_limit 取 min(limit,100) 与
            # 正常分支钳制口径一致；requested_limit 取调用方原始值 raw_limit。
            "requested_limit": raw_limit, "effective_limit": min(limit, 100),
            "note": "查询关键词为空，未执行检索；请提供有效关键词。"}}
    out = []
    for e in guides["entries"]:
        if guideline_set and e.get("set") != guideline_set:
            continue
        # BUG-56（2026-08-12）：① tags 用规范 join 而非 __str__；② hay 只拼当前视图可见
        # 字段（title+tags+set+视图正文），消除"幻影匹配"——非临床角色不再因 full 临床文案命中，
        # 命中理由与返回内容一致；set（如 KDIGO2024）在结果中可见，纳入检索（BUG-62）。
        tags = " ".join(str(t) for t in (e.get("tags") or []))
        # 十八审（2026-08-24，C7）：haystack 必须含 e["id"]——临床/编排 Agent 常按规范
        # 编号（如 "KDIGO2024-K" / "PRNT2020-ENERGY"）精确检索，标题与正文往往不含该
        # 字面量，漏拼 id 会 100% 召回失败。
        hay = " ".join([e["id"], e["title"], tags, str(e.get("set") or ""), e.get(view, "")])
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
        # P2-1（2026-08-18）：requested_limit=调用方请求值、effective_limit=钳制后
        # 实际生效值——此前静默截断到 100 不告知，上层可能误判知识库全量大小。
        "count": len(out), "returned_count": min(len(out), limit),
        "requested_limit": raw_limit, "effective_limit": limit,
        "results": out[:limit],
        "truncated": truncated,
        "note": f"命中 {len(out)} 条，已截断返回前 {limit} 条（limit={limit}）" if truncated else None,
    }}


def search_sop(query: str, limit: int = 20) -> dict[str, Any]:
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
    raw_limit = limit  # P2-1（2026-08-18）：保留调用方原始请求值（语义透明字段）
    limit = _validate_limit(limit)
    view = _view_for_caller(caller)
    sops = _load_sops()
    # 四审（2026-08-12）：空关键词显式提示（与 search_guideline 同口径）
    if not (query or "").strip():
        return {"ok": True, "data": {
            "query": query, "role": caller, "count": 0,
            # P2-1（2026-08-18）：空分支补 returned_count（与正常分支 Schema 一致）
            "returned_count": 0, "results": [],
            # CT-B1（2026-08-14）：补 view（与 search_guideline 信封一致，编排层统一
            # 取 data.view；此前 SOP 的 view 只出现在结果条目层，形状分裂）
            "view": view, "truncated": False,
            # 十七审（2026-08-24，C3）：空分支补齐 requested_limit/effective_limit，
            # 与正常分支信封结构对齐（同 search_guideline 口径）。
            "requested_limit": raw_limit, "effective_limit": min(limit, 100),
            "note": "查询关键词为空，未执行检索；请提供有效关键词。"}}
    out = []
    for s in sops["sops"]:
        # BUG-56（2026-08-12）：hay 只拼当前视图可见文本（消除幻影匹配）；tags 规范 join；
        # child 显式 null 时以 (s.get("child") or "") 兜底，避免 " ".join 抛 TypeError。
        tags = " ".join(str(t) for t in (s.get("tags") or []))
        body = s["content"] if view == "full" else (s.get(view) or s.get("child") or "")
        # 十八审（2026-08-24，C7）：haystack 必须含 s["id"]——同 search_guideline 口径，
        # 支持按规范编号（如 "SOP-HYPERK-EMERG"）精确检索。
        hay = " ".join([s["id"], s["title"], tags, body])
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
                                 # 十六审（2026-08-24，#4）：补 role/view 与 search_guideline
                                 # 信封对齐（编排层统一取 data.role/data.view），消除 Schema 漂移。
                                 "role": caller, "view": view,
                                 # P1-4（2026-08-18）：count=命中总数、returned_count=实际返回数
                                 # P2-1（2026-08-18）：requested/effective_limit 语义透明
                                 "count": len(out), "returned_count": min(len(out), limit),
                                 "requested_limit": raw_limit, "effective_limit": limit,
                                 "results": out[:limit],
                                 "truncated": truncated,
                                 "note": (f"命中 {len(out)} 条，已截断返回前 {limit} 条"
                                          f"（limit={limit}）" if truncated else None)}}


def get_citation(ref_id: str) -> dict[str, Any]:
    """生成规范引用串。支持指南条目 id 与 SOP id。

    BUG-66 后补（2026-08-12）：入口先做跨文件 id 唯一性校验——get_citation 按
    "先指南后 SOP"解析，两文件 id 重叠会让 SOP 被遮蔽（校验缓存避免重复开销）。
    视图说明：title/source/strength/evidence 为元数据，对所有读权角色可见（与
    search_guideline/search_sop 返回一致）；视图裁剪只作用于正文 text/content。
    """
    get_caller()  # P0-1 身份校验副作用（未设置/非法 A207_CALLER 抛 CallerUnknown）；本函数返回值未用
    enforce_read(MCP_NAME)
    # P1-3（2026-08-18）：ref_id 类型 + 非空校验——此前 None/非 str 静默走循环
    # 比较返回 NOT_FOUND（"未找到"而非"参数错误"，语义误导）；空串同样无意义。
    if not isinstance(ref_id, str) or not ref_id.strip():
        raise InvalidArgumentError(
            f"ref_id 必须为非空字符串，收到：{ref_id!r}")
    # P1-2（2026-08-18）：strip 归一化——此前仅校验时 strip、匹配 `e["id"] == ref_id`
    # 用原始串，带首尾空格的合法 ID（" KDIGO2024-001 "）匹配失败返回 NOT_FOUND。
    ref_id = ref_id.strip()
    ref_lower = ref_id.lower()  # 十八审（2026-08-24，C10）：大小写容错——上游 Agent 传入
    _validate_cross_file_ids()  # "kdigo2024-k" / "sop-hyperk-emerg" 等小写不应返回 NOT_FOUND。
    guides = _load_guides()
    for e in guides["entries"]:
        if e["id"].lower() == ref_lower:
            citation = (f"[{e['id']}] {e['title']}. {e['source']} "
                        f"（推荐强度：{e['strength']}；证据级别：{e['evidence']}）")
            return {"ok": True, "data": {"ref_id": e["id"], "citation": citation,
                                         "source": e["source"], "strength": e["strength"],
                                         "evidence": e["evidence"]}}
    sops = _load_sops()
    for s in sops["sops"]:
        if s["id"].lower() == ref_lower:
            citation = f"[{s['id']}] {s['title']}. {s['source']}"
            return {"ok": True, "data": {"ref_id": s["id"], "citation": citation,
                                         "source": s["source"]}}
    # S1（2026-08-12 五包审查）：统一 {ok, data} 信封——未找到由扁平 error 字段改为
    # 标准 {ok: false, error: NOT_FOUND, detail} 失败信封（编排层可统一按 ok 分支）。
    return {"ok": False, "error": "NOT_FOUND",
            "detail": f"未找到引用 ID：{ref_id}", "ref_id": ref_id}


def _self_test_refs() -> list[str]:
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

# 审查 P2-4/P2-5（2026-08-18）：报告渲染资源上限（LLM 防护）——
# 外部输入可构造深层嵌套（dict→dict→…1000 层触发 RecursionError）或超大 list
# （["x"]*1000000 灌爆上下文），渲染必须显式截断而非让异常/膨胀穿透到调用方。
_MAX_RENDER_DEPTH = 20          # Markdown 递归最大深度（超限"（嵌套过深，已省略）"）
_MAX_LIST_ITEMS = 100           # 单列表最大渲染项数（超限"共 N 项，仅显示前 100 项"）
_MAX_TEXT_LENGTH = 5000         # 单文本字段最大字符数（超限截断标注）
_MAX_RENDERED_CHARS = 50000     # 最终报告最大字符数（超限截断标注）


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


def _parse_pew_date(value: Any) -> datetime | None:
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


def _pew_trend_info(pew_history: list[dict]) -> dict[str, Any]:
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
    invalid_type_count = total_count - len(pts)
    dated = []
    invalid_date_count = 0
    invalid_level_count = 0
    for p in pts:
        dt = _parse_pew_date(p.get("date"))
        if dt is None:
            invalid_date_count += 1
            continue
        # BUG-66 后补（2026-08-12）：level 未知值同样剔除——_PEW_ORDER.get(level, 0)
        # 会把 "unknown"/拼写错误静默映射为 0(low)，high→unknown 被误判"改善"掩盖恶化；
        # 与日期无效同理，无法可靠判定严重度的点不参与趋势（fail-closed）。
        # M（2026-08-16，第七轮审查）：**缺 level 键也剔除**——此前 p.get("level",
        # "low") 对缺失键默认 low，数据不完整的点被当"轻"参与趋势（与 fail-closed
        # 意图相悖）；显式 None 判定。
        lv = p.get("level")
        if lv is None or str(lv).strip().lower() not in _PEW_ORDER:
            invalid_level_count += 1
            continue
        dated.append((dt, p))
    # 十八审（2026-08-24，C8）：仅当**无任何有效时间点**才抹除当前等级/高危标志；
    # 单条有效记录（如初诊确诊 L1 重度营养不良）必须保留 current_level 与
    # historical_high_risk，否则向临床/家长掩盖初诊患儿的高危信号。趋势仍仅在
    # 去重后 >=2 个离散时间点才计算（下方独立守卫），单点归 no_data。
    if len(dated) == 0:
        return {"trend": "no_data", "valid_count": len(dated), "total_count": total_count,
                # P1-1（2026-08-18）：按原因分类统计——统一"日期格式无效"会误导
                # （风险等级非法/缺 level/非 dict 都被归为日期错误，医疗语义失真）。
                "invalid_date_count": invalid_date_count,
                "invalid_level_count": invalid_level_count,
                "invalid_type_count": invalid_type_count,
                "current_level": None, "historical_peak": None,
                # 审查 P2-1/P2-2（2026-08-18）：契约统一（无有效点→无重复/冲突/高风险）
                "duplicate_timestamp_count": 0, "conflict_count": 0,
                "historical_high_risk": False}
    dated.sort(key=lambda x: x[0])
    # 审查 P2-1（2026-08-18）：同一时间点多条记录 canonical 化——此前仅按日期
    # 稳定排序后取首尾，同日期多条记录时结果依赖输入顺序（[L1,L3] vs [L3,L1]
    # 得到不同 current_level/trend，deterministic 问题）。按时间点分组，组内取
    # **风险等级最高者**为该时间点 canonical 等级；趋势/当前/峰值均基于
    # canonical 序列，与输入顺序无关。同时统计重复/冲突供上游追溯数据质量。
    # 审查（2026-08-19，content 审查 一）：**重复判定基于完整 timestamp 而非日期**——
    # 旧 `dt.date()` 把同日不同时刻（08:00 / 18:00）误并为同一时间点，同日内部风险
    # 变化丢失（valid_count/current_level/trend 全部可能错误）。现以完整 datetime
    # 为唯一键：**完全相同的 timestamp** 才算重复（_parse_pew_date 已统一 UTC，
    # 不同时区的同一时刻解析后相等）；duplicate_timestamp_count 命名与语义一致。
    by_timestamp: dict[Any, dict[str, Any]] = {}
    for dt, p in dated:
        lv = _PEW_ORDER[str(p.get("level")).strip().lower()]
        slot = by_timestamp.get(dt)
        if slot is None:
            by_timestamp[dt] = {"lv": lv, "entry": p, "levels": {lv}}
        else:
            slot["levels"].add(lv)
            if lv > slot["lv"]:
                slot["lv"] = lv
                slot["entry"] = p
    duplicate_timestamp_count = len(dated) - len(by_timestamp)
    conflict_count = sum(1 for s in by_timestamp.values() if len(s["levels"]) > 1)
    canon = [by_timestamp[t] for t in sorted(by_timestamp)]
    # 十七审（2026-08-24，C2）：去重后有效时间点 < 2 时**不能**做首尾趋势比较——
    # 入口守卫 `len(dated) < 2` 仅在去重**前**生效，两条同 timestamp 记录会绕过
    # 它（dated=2 但通过 by_timestamp 去重后 canon 长度=1），此时 canon[0]==canon[-1]
    # 恒定 fo==lo，原逻辑误判 "stable"，而实际仅有一个时间点、不存在时间跨度趋势。
    # 此处独立守卫：去重后若不足 2 个离散时间点，必须返回 "no_data"。
    if len(canon) < 2:
        trend = "no_data"
    else:
        fo = canon[0]["lv"]
        lo = canon[-1]["lv"]
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
    _peak_entry = max(canon, key=lambda s: s["lv"])
    _hist_lv = _peak_entry["lv"]
    return {"trend": trend, "valid_count": len(dated), "total_count": total_count,
            # P1-1（2026-08-18）：按原因分类统计（见 no_data 分支注释）。
            "invalid_date_count": invalid_date_count,
            "invalid_level_count": invalid_level_count,
            "invalid_type_count": invalid_type_count,
            "current_level": canon[-1]["entry"].get("level"),
            "historical_peak": _peak_entry["entry"].get("level"),
            # 审查 P2-1：同时间点重复记录数 / 冲突时间点数（上游数据质量追踪）
            "duplicate_timestamp_count": duplicate_timestamp_count,
            "conflict_count": conflict_count,
            # 审查 P2-2（2026-08-18）：历史最高等级是否为高风险（L1/high/critical，
            # 序=3）——trend 只反映首尾变化，历史中间严重恶化不会进 trend；该机器
            # 字段不改趋势算法、保留历史高风险提示（与 P0-2 解耦精神一致）。
            "historical_high_risk": _hist_lv >= _PEW_ORDER["l1"]}


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


def _extract_energy_achievement_pct(nutrition_assessment: dict) -> float | None:
    """营养摄入达成率提取**单一事实源**（审查 P2-3，2026-08-18）。

    兼容三种上游结构：① P2 assess_intake_vs_target 实际输出
    `data.energy.achievement_pct`；② 直接 `energy.achievement_pct`；③ 旧契约
    `intake.achievement.energy_pct`。返回有限数值（_is_number 保证，bool/NaN/Inf
    排除），无有效数据返回 None。

    _derive_status（<50% 升级 caution）与 generate_patient_report 的
    nutrition_valid 共用本函数——此前两处各实现一套解析逻辑，未来 schema
    变更时极易出现"一处认为有效、另一处认为无效"的 schema drift。
    """
    if not isinstance(nutrition_assessment, dict):
        return None
    _d = nutrition_assessment.get("data")
    if isinstance(_d, dict) and isinstance(_d.get("energy"), dict) \
            and _is_number(_d["energy"].get("achievement_pct")):
        return float(_d["energy"]["achievement_pct"])
    if isinstance(nutrition_assessment.get("energy"), dict) \
            and _is_number(nutrition_assessment["energy"].get("achievement_pct")):
        return float(nutrition_assessment["energy"]["achievement_pct"])
    intake = nutrition_assessment.get("intake")
    ach = (intake.get("achievement") or {}) if isinstance(intake, dict) else {}
    if _is_number(ach.get("energy_pct")):
        return float(ach["energy_pct"])
    return None


def _derive_status(risk_level: str, pew_history: list[dict],
                   nutrition_assessment: dict,
                   pew_trend: str | None = None) -> str:
    # BUG-66 后补 ❸（2026-08-12）：可选 pew_trend 参数——generate_patient_report 已
    # 调 _pew_trend_info 算过趋势，传入可避免对小数据列表二次解析排序（性能冗余）。
    # 缺省 None 时内部自算（保持独立调用兼容）。
    # BUG-62（2026-08-12）：risk_level 大小写归一化——"HIGH"/"l1" 等变体若直接
    # _RISK_TO_STATUS.get 失败会静默回退 stable（fail-open 掩盖真实风险）。
    # 审查（2026-08-19，content 审查 二）：**废除字符剥离式清洗**——旧
    # `re.sub(r"[^a-z0-9]", "", ...)` 会把 "L!!!1"/"L-1"/"L 1" 等**非法格式**强行
    # 变成合法 "l1"（医疗风险等级判定中，非法输入被自动转换成合法等级是不安全的）。
    # 现仅做 strip().lower()（不删除任何字符），严格白名单匹配：合法值
    # l0/l1/l2/l3/low/medium/high/critical/caution/stable/none 才解析；
    # "L-1"/"L 1"/"L!!!1" 等不在白名单 → fallback "caution" + 调用方 risk.valid=false
    # 标注（未知输入拒绝/忽略，不强行转换）。
    # P5（2026-08-15）：**不可哈希类型 TypeError**——risk_level 为 list/dict 时
    # _RISK_TO_STATUS.get(risk_level)（dict.get 非字符串键）抛 unhashable TypeError
    # → 报告生成整段崩溃。统一用已归一化字符串 key 查询（str() 转换兜底），
    # 不再用原始 risk_level 作 dict 键。
    rl = str(risk_level or "").strip().lower()
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
    # 审查 P2-3（2026-08-18）：解析逻辑收敛到 _extract_energy_achievement_pct
    # 单一事实源（兼容三种上游结构 + _is_number 防 bool/NaN/Inf，见该函数）。
    # 无有效数据（None）→ 不参与升级（与旧"默认 100 不升级"语义等价）。
    energy_pct = _extract_energy_achievement_pct(nutrition_assessment)
    if energy_pct is not None and energy_pct < 50 and base == "stable":
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


def _validate_and_canonicalize_demographics(demographics: dict) -> None:
    """P2-2（2026-08-18）：demographics 子字段基础 Schema 校验 + **就地 canonicalize**。

    ⚠️ 注意：本函数**就地修改入参 dict**（非纯函数）——校验通过后把 sex/ckd_stage/
    dialysis_mode 归一为 canonical 值写回原 dict，调用方（generate_patient_report）
    直接消费写回后的 canonical 值渲染报告。这是有意设计（P1 约定：下游只消费
    canonical 值，杜绝同字段多表示），非副作用 bug；若需保留原 dict 请先 copy。

    校验 age_years（非负有限数值）、sex（M/F）、ckd_stage（CKD1-5/G1-5 分期）、
    dialysis_mode（none/hemodialysis/peritoneal）；None/缺省跳过（未提供合法）。
    非法即抛 InvalidArgumentError（server 层 translate_error 归 INVALID_INPUT）。
    """
    _SEX = ("M", "F")
    # 审查 P1-2：CKD stage 白名单（canonical 映射单一事实源）——数字/CKDx 形式归
    # G 记法；G3A/G3B 大小写归一为 G3a/G3b（KDIGO 子分期，assessment classify_ckd
    # 输出同款）；其余任意非空字符串（CKD99/banana/G99）一律拒绝。
    _STAGE_MAP = {
        "1": "G1", "2": "G2", "3": "G3", "4": "G4", "5": "G5",
        "CKD1": "G1", "CKD2": "G2", "CKD3": "G3", "CKD4": "G4", "CKD5": "G5",
        "G1": "G1", "G2": "G2", "G3": "G3", "G4": "G4", "G5": "G5",
        "G3A": "G3a", "G3B": "G3b",
        # 十八审（2026-08-24，C11）：补充临床常见记法别名——"3A"/"3B"（无 CKD 前缀）、
        # "CKD3A"/"CKD3B" 均归一为 G3a/G3b 子分期，避免上游传入被拒。
        "3A": "G3a", "3B": "G3b", "CKD3A": "G3a", "CKD3B": "G3b",
    }
    _DM = ("none", "hemodialysis", "peritoneal")
    age = demographics.get("age_years")
    if age is not None:
        if isinstance(age, bool) or not isinstance(age, (int, float)) \
                or not math.isfinite(float(age)) or float(age) < 0:
            raise InvalidArgumentError(
                f"demographics.age_years 必须为非负有限数值，收到：{age!r}")
    sex = demographics.get("sex")
    if sex is not None:
        # 审查（2026-08-19，content 审查 三）：先严格类型校验再 strip/upper——
        # 旧 `str(sex)` 会把任意对象（123、[]、{"m": 1}）强转成字符串再参与判断，
        # 不是严格的输入类型验证。sex 语义是单字符枚举，非 str 一律拒绝。
        if not isinstance(sex, str):
            raise InvalidArgumentError(
                f"demographics.sex 必须为字符串（M/F），收到：{type(sex).__name__} {sex!r}")
        sex_norm = sex.strip().upper()
        if sex_norm not in _SEX:
            raise InvalidArgumentError(
                f"demographics.sex 必须是 M/F，收到：{sex!r}")
        # P1-1（2026-08-18）：canonicalize——" m "/"f " 归一为 "M"/"F"，
        # 报告层/下游只消费 canonical 值，杜绝同字段多表示。
        demographics["sex"] = sex_norm
    stage = demographics.get("ckd_stage")
    if stage is not None:
        if isinstance(stage, bool) or not isinstance(stage, (int, str)):
            raise InvalidArgumentError(
                f"demographics.ckd_stage 必须为分期编号（1-5）或分期字符串，"
                f"收到：{stage!r}")
        if isinstance(stage, int):
            if not (1 <= stage <= 5):
                raise InvalidArgumentError(
                    f"demographics.ckd_stage 数字分期必须在 1-5，收到：{stage!r}")
            demographics["ckd_stage"] = f"G{stage}"
        else:
            canon_stage = _STAGE_MAP.get(str(stage).strip().upper())
            if canon_stage is None:
                raise InvalidArgumentError(
                    f"demographics.ckd_stage={stage!r} 非法：仅允许 CKD1-5 / G1-5"
                    "（含 G3a/G3b 子分期）或数字 1-5，收到非白名单值")
            demographics["ckd_stage"] = canon_stage
    dm = demographics.get("dialysis_mode")
    if dm is not None:
        # 审查（2026-08-19，content 审查 三）：先严格类型校验（同 sex 口径）——
        # 旧 `str(dm)` 对任意对象强转字符串，非严格类型验证。
        if not isinstance(dm, str):
            raise InvalidArgumentError(
                f"demographics.dialysis_mode 必须为字符串，收到：{type(dm).__name__} {dm!r}")
        dm_norm = dm.strip().lower()
        if dm_norm not in _DM:
            raise InvalidArgumentError(
                f"demographics.dialysis_mode 必须是 {'/'.join(_DM)} 之一，"
                f"收到：{dm!r}")
        # P1-3（2026-08-18）：canonicalize（同上）
        demographics["dialysis_mode"] = dm_norm


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
    # 审查 P2-6（2026-08-18）：core 不直接构造 MCP error envelope——统一抛
    # InvalidArgumentError，由 server 层 translate_error 归 INVALID_INPUT。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        # 保留原始校验异常链（B904），便于排障定位具体校验规则
        raise InvalidArgumentError(str(exc)) from exc
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
    # P2-2（2026-08-18）：demographics 子字段基础 Schema 强校验（fail-closed）——
    # 此前仅校验顶层 dict，age_years:"abc"/sex:[] 透传进报告打印（数据语义失真、
    # 家长侧显示乱码值）。非法即 INVALID_INPUT，不静默透传。
    # 审查 P1-1/P1-2/P1-3（2026-08-18）：校验通过即就地 canonicalize（sex→M/F、
    # ckd_stage→G 记法白名单、dialysis_mode→小写枚举），非法抛 InvalidArgumentError。
    _validate_and_canonicalize_demographics(demographics)
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
    # 审查（2026-08-19）：清洗与 _derive_status 同口径（strip().lower()，不删字符）——
    # "L!!!1"/"L-1"/"L 1" 等非法格式不再被强行洗成合法等级，直接判 unknown。
    rl_norm = str(risk_level or "").strip().lower()
    risk_unknown = rl_norm not in ("l0", "l1", "l2", "l3", "low", "medium", "high",
                                   "critical", "caution", "stable", "none")
    # F7（2026-08-17，十二审）：**营养评估未评估透明化**——空 nutrition dict 时
    # _derive_status 的 energy_pct 默认 100 永不升级（fail-open），且报告无任何提示
    # （与 risk.valid 不对称）。现标注 nutrition_valid=false：nutrition_assessment 为
    # 空/无 data.energy.achievement_pct 时表示摄入数据缺失，报告显式提示"未评估"，
    # 不把"没数据"当"达成 100%"。
    # 审查 P2-3（2026-08-18）：nutrition_valid 与 _derive_status 共用
    # _extract_energy_achievement_pct 单一解析（防两套逻辑 schema drift）。
    nutrition_valid = _extract_energy_achievement_pct(nutrition_assessment) is not None
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
            # 审查 P2-1/P2-2（2026-08-18）：同时间点重复/冲突统计（上游数据质量）
            # + 历史高风险机器标志（历史中间恶化不进 trend 但必须保留提示）。
            "duplicate_timestamp_count": pew_info["duplicate_timestamp_count"],
            "conflict_count": pew_info["conflict_count"],
            "historical_high_risk": pew_info["historical_high_risk"],
            # M-2（2026-08-16，十一审）：架构语言不进家长上下文——此前 source 硬编码
            # "M3 (ADR-007)"（内部模块编号），家长报告暴露架构语言。改中性描述。
            "source": "PEW 历史（营养评估）",
            "note": (f"趋势基于 {pew_info['valid_count']}/{pew_info['total_count']} 条有效记录"
                     if pew_info["valid_count"] < pew_info["total_count"] else "全部记录有效"),
        },
        "risk": {"level": risk_level,
                 # 六审（2026-08-13）：未知等级显式标注——_derive_status 对非法
                 # risk_level 静默归 stable（fail-open 掩盖真实风险），报告必须透明。
                 "valid": not risk_unknown,
                 # 审查 P1-4（2026-08-18）：非法 risk_level fallback 的机器可读原因——
                 # 真实 caution（L2/medium）与非法输入 fallback 的 caution 在只读
                 # overall_status 时不可区分；reason 字段让下游程序化区分：
                 # "VALID"=真实等级 / "INVALID_RISK_LEVEL"=非法输入按保守口径兜底。
                 "reason": "VALID" if not risk_unknown else "INVALID_RISK_LEVEL"},
        # F7（2026-08-17）：营养摄入达成率未评估透明化——空 dict/无摄入数据时
        # nutrition_valid=false，报告显式提示（不把"没数据"当"达成 100%"）。
        "nutrition_valid": nutrition_valid,
        "overall_status": overall_status,
    }

    # ---- 文案（markdown）----
    lines = [f"# 患者报告 · {patient_id}", ""]
    # 十六审（2026-08-24，#7）：缺失基本信息字段兜底为"未提供"，杜绝 Python None
    # 字面量直接呈现给患儿/家长（医疗报告严谨性）。
    _age = demographics.get("age_years")
    age_str = f"{_age} 岁" if _age is not None else "未提供"
    d_sex = {"M": "男", "F": "女"}.get(demographics.get("sex"),
                                      demographics.get("sex") or "未提供")
    _stage = demographics.get("ckd_stage")
    stage_str = _stage if _stage is not None else "未提供"
    _dm = demographics.get("dialysis_mode")
    # 十七审（2026-08-24，C5）：透析方式中文映射（呈现层打磨）——入参经
    # _validate_and_canonicalize_demographics 已归一为小写枚举
    # {none, hemodialysis, peritoneal}，此处仅做展示翻译；不改动
    # sections["patient"]["dialysis_mode"]（保持机器可读原值，避免破坏测试断言）。
    _dm_map = {"none": "未透析", "hemodialysis": "血液透析", "peritoneal": "腹膜透析"}
    dm_str = _dm_map.get(_dm, _dm) if _dm is not None else "未提供"
    lines.append(_section(
        "一、基本信息",
        f"- 年龄：{age_str}　性别：{d_sex}\n"
        f"- CKD 分期：{stage_str}　透析方式：{dm_str}"))
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
        # 十八审（2026-08-24，C9）：引用块须以空行与后续 section 标题隔离——
        # 否则严格 CommonMark 解析器会把下一行 "### 四、随访与依从" 吞进引用块，层级塌陷。
        lines.append("")
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
    elif isinstance(plans, int) and not isinstance(plans, bool):
        # 审查（2026-08-19，content 审查 四）：计划数只接受**非负整数**——旧
        # `int(plans)` 对 float（3.9→3）静默截断、对负数（-10）照单全收（报告可
        # 出现"进行中计划数：-10"）。整数语义拒绝 float/负值（fail-closed）。
        if plans < 0:
            raise InvalidArgumentError(
                f"followup_summary.plans 数值不能为负，收到：{plans!r}")
        plans_count = plans
    elif isinstance(plans, float) and not isinstance(plans, bool):
        # float 非整数值（3.9）不再静默截断——显式拒绝（报告计数语义必须精确）
        raise InvalidArgumentError(
            f"followup_summary.plans 必须为整数或容器，收到 float：{plans!r}")
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
    # P1-1（2026-08-18）：无效原因分类提示——此前统一"日期格式无效"误导
    # （风险等级非法/缺 level/非 dict 被归为日期错误，医疗语义失真）。
    _invalid_parts = []
    if pew_info.get("invalid_date_count"):
        _invalid_parts.append(f"{pew_info['invalid_date_count']} 条日期无效")
    if pew_info.get("invalid_level_count"):
        _invalid_parts.append(f"{pew_info['invalid_level_count']} 条风险等级无效")
    if pew_info.get("invalid_type_count"):
        _invalid_parts.append(f"{pew_info['invalid_type_count']} 条格式非法")
    _invalid_note = ""
    if invalid_count and _invalid_parts:
        _invalid_note = f"\n- 提示：{('、'.join(_invalid_parts))}，未参与趋势计算"
    lines.append(_section(
        "五、PEW 历史",
        f"- 历史点数：{pew_info['valid_count']}/{pew_info['total_count']}（有效/总数）　趋势：{trend}"
        + _pew_hist_note
        + _invalid_note))
    # 六审：未知风险等级在 markdown 中显式提示（不静默展示为"稳定"）
    # 十八审（2026-08-24，C6）：risk_level 为外部可控入参，必须经 _md_escape 转义——
    # 防脏数据/恶意构造的换行与 markdown 元字符伪造标题与结论（医疗报告篡改）。
    # _md_escape 已先于转义把换行折叠为空格（XSS + 结构注入双重防护）。
    risk_line = f"- 等级：{_md_escape(risk_level)}"
    if risk_unknown:
        risk_line += "（⚠ 无法解析的风险等级，整体状态按保守口径展示，请核对上游评估输出）"
    lines.append(_section("六、风险等级", risk_line))
    lines.append(_section("七、综合结论", f"**整体状态：{_STATUS_CN.get(overall_status, overall_status)}**"))

    # S1（2026-08-12 五包审查）：统一 {ok, data} 信封——此前 {ok, patient_id, ...} 平铺
    # 无 data 包裹，与其余四包契约分裂；编排层可统一按 data 取业务字段。
    # 审查 P2-5（2026-08-18）：报告总长度上限——超大 lab_summary/followup_summary
    # 即使逐项截断仍可能整体膨胀（数千个键/值），超 _MAX_RENDERED_CHARS 截断标注，
    # 控制 MCP response 与 LLM 上下文占用。
    summary_markdown = "\n".join(lines)
    if len(summary_markdown) > _MAX_RENDERED_CHARS:
        summary_markdown = summary_markdown[:_MAX_RENDERED_CHARS] + (
            "\n\n> ⚠ 报告内容过长，已截断（超出最大渲染字符数）")
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
            "summary_markdown": summary_markdown,
        },
    }


def _md_escape(value: Any) -> str:
    """B2（2026-08-15）：markdown 值转义——化验备注/医生备注等自由文本可含换行或
    markdown 元字符（#、*、`、[、]、| 等），直接插值可篡改报告结构（注入标题/列表/
    代码块/表格）。换行折叠为空格（保持单行条目语义），元字符转义为字面量。
    P2-3（2026-08-18）：**先 html.escape 再 md 转义**——此前不处理 <script> 等 HTML
    标签，支持 HTML 渲染的前端存在 XSS 风险；html.escape 处理 & < > " '（在 md
    转义前做，避免引入的实体被后续转义破坏）。
    审查 P2-5（2026-08-18）：超长文本截断（_MAX_TEXT_LENGTH）——自由文本字段
    无上限会撑爆 Markdown 输出 / MCP response / LLM 上下文。"""
    text = str(value if value is not None else "")
    if len(text) > _MAX_TEXT_LENGTH:
        text = text[:_MAX_TEXT_LENGTH] + "…（已截断）"
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    # 十七审（2026-08-24，C1）：quote=False——markdown 正文不在 HTML 属性引号内，
    # 无需转义 ' "；默认 quote=True 会把 ' 转成 &#x27;，后续 .replace("#","\\#")
    # 破坏实体成 &\#x27;，含英文撇号文本（Crohn's disease）前端渲染乱码。
    # & < > 的 XSS 防护在 quote=False 下仍完整。
    text = html.escape(text, quote=False)  # P2-3：HTML 标签/实体转义（XSS 防护）
    return (text.replace("\\", "\\\\").replace("`", "\\`").replace("*", "\\*")
            .replace("_", "\\_").replace("[", "\\[").replace("]", "\\]")
            .replace("#", "\\#").replace(">", "\\>").replace("|", "\\|"))


def _fmt_dict(d: Any, _depth: int = 0) -> str:
    # BUG-56（2026-08-12）：None 返回 "" 而非 "None"——否则 "None" or "（无）" 会渲染出
    # "二、最新化验：None" 而非预期的 "（无）"。
    # CT-Q1 修复（2026-08-14）：**嵌套 dict/list 递归格式化**——此前 f"- {k}：{v}"
    # 对嵌套结构直接 str(v) → Python repr（{'achievement': {...}}）渲染进家长报告，
    # PII 文本与机器可读 repr 混入 markdown。递归后嵌套键渲染为缩进子列表。
    # B2（2026-08-15）：标量值经 _md_escape 转义（防自由文本注入 markdown 结构）。
    # 审查 P2-4（2026-08-18）：递归深度上限——外部构造 1000 层嵌套 dict 此前触发
    # RecursionError（报告生成崩溃）；超过 _MAX_RENDER_DEPTH 显式省略。
    if _depth > _MAX_RENDER_DEPTH:
        return "（嵌套过深，已省略）"
    if d is None:
        return ""
    if isinstance(d, dict):
        if not d:
            return ""
        lines = []
        for k, v in d.items():
            if isinstance(v, dict):
                inner = _fmt_dict(v, _depth + 1)
                lines.append(f"- {_md_escape(k)}：{inner if inner else '（无）'}")
            elif isinstance(v, (list, tuple)):
                inner = _fmt_list(v, _depth + 1)
                lines.append(f"- {_md_escape(k)}：{inner if inner else '（无）'}")
            else:
                lines.append(f"- {_md_escape(k)}：{_md_escape(v)}")
        return "\n".join(lines)
    return _md_escape(d)


def _fmt_list(items: Any, _depth: int = 0) -> str:
    """列表递归渲染：元素为 dict → 递归 _fmt_dict；标量 → 顿号拼接（B2 转义）。

    审查 P2-4/P2-5（2026-08-18）：递归深度上限（同 _fmt_dict）+ 超大列表截断——
    报告数据含 ["x"]*1000000 时此前全量渲染（CPU/内存/Markdown 膨胀）；现最多
    渲染 _MAX_LIST_ITEMS 项，超限明确标注"共 N 项，仅显示前 M 项"。
    """
    if _depth > _MAX_RENDER_DEPTH:
        return "（嵌套过深，已省略）"
    if not items:
        return ""
    parts = []
    total = len(items)
    shown = min(total, _MAX_LIST_ITEMS)
    for it in items[:shown]:
        if isinstance(it, dict):
            inner = _fmt_dict(it, _depth + 1)
            parts.append(inner if inner else "（无）")
        else:
            parts.append(_md_escape(it))
    if total > shown:
        parts.append(f"…共 {total} 项，仅显示前 {shown} 项")
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
