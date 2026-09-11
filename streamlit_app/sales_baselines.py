"""销售事实澄清的非 LLM 对照基线。

两个基线都只返回评测结果，不创建建议、不写数据库、不改变客户状态：
结构化表单把理解成本交给销售，关键词规则保留自由文本但只做可解释匹配。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

import customer_state

FORM_FIELDS = (
    "customer_need",
    "customer_commitment",
    "objection",
    "decision_maker",
    "timeline",
    "next_step",
    "proposed_state",
    "evidence_note",
)
FORM_REQUIRED_FIELDS = (
    "customer_need",
    "customer_commitment",
    "timeline",
    "next_step",
    "proposed_state",
    "evidence_note",
)

# 规则基线只识别有限、可审计的表达；无法命中时必须转人工，不猜测。
RULE_PATTERNS: dict[str, tuple[str, ...]] = {
    "customer_need": ("需要", "希望", "需求", "关注", "想要"),
    "customer_commitment": ("同意", "确认", "接受", "承诺", "决定", "签合同", "签约", "已采购", "成交"),
    "objection": ("担心", "顾虑", "阻塞", "异议", "问题", "不满意"),
    "decision_maker": ("负责人", "决策人", "技术负责人", "老板", "采购"),
    "timeline": ("今天", "明天", "本周", "下周", "月底", "本月", "尽快", "时间"),
    "next_step": ("下一步", "后续", "跟进", "演示", "评审", "试用", "测试", "报价"),
    "competitor_signal": ("竞品", "另一家", "对比", "替代", "竞争对手"),
}
STATE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("won", ("已签合同", "合同已签", "已经签约", "已采购", "成交")),
    ("commercial_negotiation", ("商务谈判", "商务", "报价", "价格", "合同")),
    ("solution_eval", ("方案评估", "技术评审", "试用", "测试", "演示", "评估")),
    ("need_confirmed", ("需求确认", "确认需求", "明确需求")),
)
NEGATION_PREFIXES = ("没有", "未", "尚未", "并未", "不是", "不")


@dataclass(frozen=True)
class BaselineResult:
    """三方案共享的最小评测结果，不代表客户当前事实。"""

    baseline: str
    accepted: bool
    proposed_state: str | None
    extracted_facts: tuple[dict[str, Any], ...]
    missing_fact_types: tuple[str, ...]
    operation_count: int
    field_count: int
    question_count: int
    evidence_mode: str
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def _valid_state(value: Any, current_state: str | None) -> bool:
    if value not in customer_state.STATES or value == "expired":
        return False
    try:
        customer_state.validate_transition(current_state, value)
    except ValueError:
        return False
    return True


def run_structured_form_baseline(payload: Mapping[str, Any], *, current_state: str | None = None) -> BaselineResult:
    """模拟不使用 Agent 的结构化表单：缺任一必填字段即转人工。"""
    missing = tuple(field for field in FORM_REQUIRED_FIELDS if _blank(payload.get(field)))
    proposed = payload.get("proposed_state")
    notes: list[str] = []
    if proposed is not None and not _valid_state(proposed, current_state):
        notes.append("proposed_state 不符合有限状态机")
    if missing:
        notes.append("表单缺少必填字段，无法形成候选建议")
    if not isinstance(payload.get("customer_id"), str) or not payload["customer_id"].strip():
        notes.append("customer_id 必填")
    accepted = not missing and not notes
    fact_values = (
        ("customer_need", "customer_need"),
        ("customer_commitment", "customer_commitment"),
        ("objection", "objection"),
        ("decision_maker", "decision_maker"),
        ("timeline", "timeline"),
        ("next_step", "next_step"),
    )
    facts = tuple(
        {"id": f"form-{index}", "type": fact_type, "value": payload[field], "source": "form_field"}
        for index, (fact_type, field) in enumerate(fact_values, start=1)
        if not _blank(payload.get(field))
    )
    return BaselineResult(
        baseline="structured_form",
        accepted=accepted,
        proposed_state=proposed if accepted else None,
        extracted_facts=facts,
        missing_fact_types=missing,
        operation_count=len(FORM_REQUIRED_FIELDS),
        field_count=sum(not _blank(payload.get(field)) for field in FORM_FIELDS),
        question_count=0,
        evidence_mode="manual_note",
        notes=tuple(notes),
    )


def _is_negated(content: str, start: int) -> bool:
    prefix = content[max(0, start - 5):start]
    return any(prefix.endswith(marker) for marker in NEGATION_PREFIXES)


def _find_rule_hits(content: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for fact_type, terms in RULE_PATTERNS.items():
        for term in terms:
            for match in re.finditer(re.escape(term), content):
                if not _is_negated(content, match.start()):
                    hits.append({
                        "id": f"rule-{len(hits) + 1}",
                        "type": fact_type,
                        "value": match.group(0),
                        "source": "initial_note",
                        "start": match.start(),
                        "end": match.end(),
                    })
                    break
            if any(hit["type"] == fact_type for hit in hits):
                break
    return hits


def _infer_state(content: str) -> str | None:
    for state, terms in STATE_PATTERNS:
        if any(term in content for term in terms):
            return state
    if content.strip():
        return "contacted"
    return None


def _required_for_state(state: str | None) -> set[str]:
    return {
        "contacted": set(),
        "need_confirmed": {"customer_need"},
        "solution_eval": {"customer_need", "next_step"},
        "commercial_negotiation": {"customer_need", "next_step", "customer_commitment"},
        "won": {"customer_commitment"},
    }.get(state or "", set())


def run_keyword_rule_baseline(content: str, *, current_state: str | None = None) -> BaselineResult:
    """用固定词典识别信号；语义不完整或状态转移非法时转人工。"""
    if not isinstance(content, str) or not content.strip():
        return BaselineResult(
            baseline="keyword_rules", accepted=False, proposed_state=None,
            extracted_facts=(), missing_fact_types=(), operation_count=1, field_count=1,
            question_count=0, evidence_mode="exact_span", notes=("content 为空",),
        )
    facts = _find_rule_hits(content)
    proposed = _infer_state(content)
    types = {fact["type"] for fact in facts}
    missing = tuple(sorted(_required_for_state(proposed) - types))
    notes: list[str] = []
    if proposed and not _valid_state(proposed, current_state):
        notes.append("规则推断的状态不符合有限状态机")
    if missing:
        notes.append("关键词无法补齐该状态的最小事实集合")
    accepted = bool(proposed) and not missing and not notes
    if not accepted and not notes:
        notes.append("未命中足够的状态信号，转人工")
    return BaselineResult(
        baseline="keyword_rules",
        accepted=accepted,
        proposed_state=proposed if accepted else None,
        extracted_facts=tuple(facts),
        missing_fact_types=missing,
        operation_count=1,
        field_count=1,
        question_count=0,
        evidence_mode="exact_span",
        notes=tuple(notes),
    )
