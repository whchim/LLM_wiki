"""确定性状态建议规则（不调用模型）：把澄清会话的已确认事实变成状态建议。

依据 `docs/SA-01_销售客户状态Agent_业务契约.md`：
- 状态集合与**最低证据要求**（第 4 节）：每个状态要求什么样的可定位证据；
- **状态机逐级推进**（第 5 节）：建议只落在"当前状态的合法下一步"，不跳跃（`new_lead`→`contacted`→…）；
- 证据不足必须标 `needs_review`，**不猜测状态**；正负信号冲突交人工，不自动选边。

与 LLM 版的关系：产出字段与 `sales_state_agent` 的输出约定一致
（proposed_state / decision / confidence / evidence / reasoning_summary / next_action / risk_flags），
便于后续叠加模型判断。差异：确定性规则允许证据引用**追加回答**（`source=question-N`，
偏移相对该来源文本），而 LLM 契约限定只引用脱敏原纪要。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import customer_state

# 主阶梯（lost_or_paused 是旁路，不在阶梯上）
LADDER = ["new_lead", "contacted", "need_confirmed", "solution_eval", "commercial_negotiation", "won"]
# SA-01 第 4 节的默认有效期（won 不自动过期）
DEFAULT_VALID_DAYS = {"new_lead": 30, "contacted": 30, "need_confirmed": 30,
                      "solution_eval": 45, "commercial_negotiation": 30,
                      "lost_or_paused": 90, "won": None}
# 与 sales_state_agent.MIN_HUMAN_CONFIDENCE 对齐：低于此值必须带 low_confidence 标志
MIN_CONFIDENCE = 0.75
MODEL_VERSION = "deterministic-rules"
PROMPT_VERSION = "sa01-state-rules-v1"

# 关键词只用于"最小可定位证据"的存在性判定，不用于判断成交概率
KEYWORDS = {
    "won": ("合同", "订单", "签约", "已签", "中标", "采购", "下单", "付款", "打款", "成交"),
    "commercial_negotiation": ("报价", "价格", "折扣", "商务", "条款", "预算",
                               "付款条件", "招标", "投标", "议价", "报价单"),
    "solution_eval": ("评估", "试用", "测试", "poc", "技术验证", "演示", "选型", "对比", "验证"),
    "need_confirmed": ("需求", "痛点", "希望解决", "想要"),
    "contacted": ("会面", "拜访", "电话", "沟通", "会议", "对接", "联系"),
    "lost_or_paused": ("拒绝", "暂停", "搁置", "终止", "不考虑", "放弃", "流失", "再议", "缓一缓"),
}
CLAIM_TYPE_LABEL = {
    "customer_need": "客户需求", "customer_commitment": "客户承诺", "objection": "客户异议",
    "decision_maker": "决策人", "timeline": "时间线", "next_step": "下一步",
    "competitor_signal": "竞品信号",
}
# 各状态还缺什么（needs_review 时告知人工/销售）——用业务语言，不出现技术键名
MISSING_HINT = {
    "new_lead": "客户身份或线索来源",
    "contacted": "接触时间与沟通对象",
    "need_confirmed": "客户明确说出的需求",
    "solution_eval": "评估事项与客户动作",
    "commercial_negotiation": "商务议题或客户明确反馈",
    "won": "合同/订单/负责人确认等硬证据",
    "lost_or_paused": "拒绝、暂停或负责人说明",
}
SUMMARY_TAIL = "系统按销售流程逐级推进（每次一级），最终阶段由负责人确认。"


def _value_of(claim: Mapping[str, Any]) -> str:
    return str(claim.get("value") or "")


def _claim_targets(claim: Mapping[str, Any]) -> set[str]:
    """单条声明能支撑哪些状态（类型 + 关键词的确定性判定）。"""
    out: set[str] = set()
    ctype = claim.get("type")
    value = _value_of(claim).lower()
    if ctype == "customer_need":
        out.add("need_confirmed")
    if ctype in ("timeline", "decision_maker"):
        out.add("contacted")
    for state, words in KEYWORDS.items():
        if any(word in value for word in words):
            out.add(state)
    return out


def _source_text(source: Any, content_redacted: str, answer_texts: Mapping[str, str]) -> str | None:
    if source == "initial_note":
        return content_redacted
    if isinstance(source, str) and source in answer_texts:
        return answer_texts[source]
    return None


def _refs_from_claim(claim: Mapping[str, Any], content_redacted: str,
                     answer_texts: Mapping[str, str]) -> list[dict]:
    """把声明里的证据重定位到来源文本；定位不到的丢弃（不编造偏移）。"""
    refs: list[dict] = []
    label = CLAIM_TYPE_LABEL.get(str(claim.get("type")), "事实")
    value = _value_of(claim).strip()
    meaning = f"{label}：{value[:60]}" if value else label
    for item in claim.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        quote = item.get("quote")
        text = _source_text(item.get("source"), content_redacted, answer_texts)
        if not isinstance(quote, str) or not quote.strip() or text is None:
            continue
        start = text.find(quote)
        if start < 0:
            continue                      # 原文里找不到 → 不产出这条证据
        refs.append({"source": item.get("source"), "quote": quote,
                     "start": start, "end": start + len(quote), "meaning": meaning})
    return refs


def _baseline_ref(content_redacted: str) -> list[dict]:
    """兜底证据：纪要本身即"线索存在"的证据（SA-01：new_lead 要求线索来源明确）。"""
    text = (content_redacted or "").strip()
    if not text:
        return []
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 4:
            start = content_redacted.find(line)
            return [{"source": "initial_note", "quote": line, "start": start,
                     "end": start + len(line), "meaning": "线索来源（纪要正文）"}]
    return []


def _legal_target(current_state: str | None, supported: set[str]) -> str | None:
    """在状态机允许的下一步里选目标：优先选证据支持的，其次选最先的前进一步。"""
    legal = set(customer_state.ALLOWED_TRANSITIONS.get(current_state, set())) - {"expired"}
    if not legal:
        return None
    if "lost_or_paused" in legal and "lost_or_paused" in supported:
        return "lost_or_paused"
    for state in reversed(LADDER):
        if state in legal and state in supported:
            return state
    for state in LADDER:
        if state in legal:
            return state
    return "lost_or_paused" if "lost_or_paused" in legal else None


def _implied_by_higher(target: str, supported: set[str]) -> bool:
    """目标是否被"更高状态的证据"所蕴含（逐级推进时，能到 solution_eval 必然先到 contacted）。"""
    if target not in LADDER:
        return False
    idx = LADDER.index(target)
    return any(state in LADDER and LADDER.index(state) > idx for state in supported)


def infer_proposal(*, claims: list[Mapping[str, Any]], current_state: str | None,
                   content_redacted: str, answer_texts: Mapping[str, str] | None = None
                   ) -> dict[str, Any]:
    """从已确认事实推出下一步状态建议（纯函数，不写库、不调模型）。

    返回字段可直接交给 `customer_state.create_proposal`；`needs_review`/`conflicts`
    用于说明为什么没有直接建议推进。
    """
    answer_texts = answer_texts or {}
    supported: set[str] = {"new_lead"}          # 有纪要即有线索来源
    refs_by_state: dict[str, list[dict]] = {}
    conflicts: list[str] = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        targets = _claim_targets(claim)
        if not targets:
            continue
        refs = _refs_from_claim(claim, content_redacted, answer_texts)
        for state in targets:
            supported.add(state)
            if refs:
                refs_by_state.setdefault(state, []).extend(refs)
        # 单条声明同时含正向推进与拒绝/暂停信号 → 冲突，交人工
        if "lost_or_paused" in targets and (targets - {"lost_or_paused"}):
            conflicts.append(f"同一事实同时含推进与暂停信号：{_value_of(claim)[:40]}")

    target = _legal_target(current_state, supported)
    flags: list[str] = []
    missing: list[str] = []
    if target is None:
        return {"generated": False, "reason": "状态机内没有可推进的下一步",
                "conflicts": conflicts, "missing": [MISSING_HINT["lost_or_paused"]]}

    strong = target in supported or _implied_by_higher(target, supported)
    if conflicts:
        decision = "needs_review"
        flags.append("conflicting_signals")
        reason = "同一份材料里既有推进的信号也有暂停的信号，需要人工判断"
    elif strong:
        decision = "propose"
        reason = f"已经看到「{MISSING_HINT[target]}」"
    else:
        decision = "needs_review"
        flags.append("missing_strong_evidence")
        missing.append(MISSING_HINT[target])
        reason = f"还没看到「{MISSING_HINT[target]}」，为避免猜错不自动推进"

    refs = refs_by_state.get(target) or _best_refs(supported, refs_by_state) or _baseline_ref(content_redacted)
    confidence = _confidence(decision, refs)
    if confidence < MIN_CONFIDENCE and "low_confidence" not in flags:
        flags.append("low_confidence")

    days = DEFAULT_VALID_DAYS.get(target)
    valid_until = datetime.now(timezone.utc) + timedelta(days=days) if days else None
    return {
        "generated": True, "proposed_state": target, "decision": decision,
        "confidence": confidence, "evidence": refs, "risk_flags": flags,
        "missing": missing, "conflicts": conflicts, "valid_until": valid_until,
        "reasoning_summary": f"{reason}。{SUMMARY_TAIL}"[:500],
        "next_action": "请负责人确认这条建议，或把它改成正确的阶段；系统不会自动改动客户状态"[:500],
        "model_version": MODEL_VERSION, "prompt_version": PROMPT_VERSION,
    }


def _best_refs(supported: set[str], refs_by_state: dict[str, list[dict]]) -> list[dict]:
    """目标状态证据不足时，退回用"已支持的最高状态"的证据，便于人工判断。"""
    for state in reversed(LADDER):
        if state in supported and refs_by_state.get(state):
            return refs_by_state[state]
    return []


def _confidence(decision: str, refs: list[dict]) -> float:
    if decision != "propose":
        return 0.5
    return round(min(0.80 + 0.05 * max(0, len(refs) - 1), 0.95), 2)
