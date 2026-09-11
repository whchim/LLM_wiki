from datetime import datetime, timedelta, timezone

import pytest

import customer_state


def _conversation(customer_id="cust-1", key="idem-1"):
    return customer_state.create_conversation(
        customer_id=customer_id,
        idempotency_key=key,
        source_type="meeting_note",
        occurred_at=datetime.now(timezone.utc),
        submitted_by="sales-1",
        display_name_redacted="客户甲",
    )


def _proposal(conversation_id, current_state=None, proposed_state="new_lead"):
    return customer_state.create_proposal(
        conversation_id=conversation_id,
        current_state=current_state,
        proposed_state=proposed_state,
        confidence=0.9,
        evidence_refs=[{"evidence_id": "ev-1", "quote": "客户确认先试用"}],
    )


def test_create_conversation_is_idempotent():
    first = _conversation()
    second = _conversation(customer_id="cust-1", key="idem-1")
    assert second["conversation_id"] == first["conversation_id"]
    assert second["customer_id"] == first["customer_id"]


def test_idempotency_key_conflict_is_rejected():
    _conversation()
    with pytest.raises(ValueError, match="绑定其他客户"):
        _conversation(customer_id="cust-other", key="idem-1")


def test_proposal_does_not_change_current_state():
    conversation = _conversation()
    proposal_id = _proposal(conversation["conversation_id"])
    assert proposal_id
    assert customer_state.get_current_state("cust-1") is None


def test_approved_proposal_writes_event_and_projection():
    conversation = _conversation()
    evidence = customer_state.add_evidence(conversation["conversation_id"], "客户确认先试用")
    proposal_id = customer_state.create_proposal(
        conversation_id=conversation["conversation_id"],
        proposed_state="new_lead",
        confidence=0.9,
        evidence_refs=[{"evidence_id": evidence["evidence_id"]}],
    )
    result = customer_state.decide_proposal(proposal_id, "approved", "owner-1")
    current = customer_state.get_current_state("cust-1")
    events = customer_state.list_state_events("cust-1")
    assert result["event_id"]
    assert current["state"] == "new_lead"
    assert current["source_event_id"] == result["event_id"]
    assert events[0]["event_type"] == "state_confirmed"


def test_illegal_transition_is_rejected():
    with pytest.raises(ValueError):
        customer_state.validate_transition("new_lead", "won")


def test_rejected_proposal_writes_no_event():
    conversation = _conversation()
    proposal_id = _proposal(conversation["conversation_id"])
    result = customer_state.decide_proposal(proposal_id, "rejected", "owner-1", reason="证据不足")
    assert result["event_id"] is None
    assert customer_state.list_state_events("cust-1") == []
    assert customer_state.get_current_state("cust-1") is None


def test_expire_and_withdraw_preserve_history():
    conversation = _conversation()
    proposal_id = customer_state.create_proposal(
        conversation_id=conversation["conversation_id"],
        proposed_state="new_lead", confidence=0.9,
        evidence_refs=[{"evidence_id": "ev-1"}],
        valid_until=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    customer_state.decide_proposal(proposal_id, "approved", "owner-1")
    expired = customer_state.expire_state("cust-1")
    assert expired and customer_state.get_current_state("cust-1")["state"] == "expired"
    withdrawn = customer_state.withdraw_current_state("cust-1", "owner-1", "纠正过期判断")
    assert withdrawn["state"] == "new_lead"
    events = customer_state.list_state_events("cust-1")
    assert [event["event_type"] for event in events[:3]] == [
        "state_withdrawn", "state_expired", "state_confirmed"
    ]


def test_sensitive_numeric_requires_ciphertext_and_never_accepts_plaintext_field():
    conversation = _conversation()
    evidence = customer_state.add_evidence(conversation["conversation_id"], "报价已脱敏")
    with pytest.raises(TypeError):
        customer_state.add_sensitive_numeric(
            evidence["evidence_id"], "quote", plaintext="100", key_version="k1"
        )
    numeric_id = customer_state.add_sensitive_numeric(
        evidence["evidence_id"], "quote", ciphertext="enc:v1:abc", key_version="k1"
    )
    assert numeric_id.startswith("num_")
