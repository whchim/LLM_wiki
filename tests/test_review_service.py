"""应用内审核引擎测试（`core/review_service.py`）。

覆盖六条契约：
1. **规则/模型分工**：完整性、敏感信息两维由代码判定（模型的这两维只作参考，以代码为准）；
2. **verdict 由代码按判定逻辑链计算**，不信模型自报（不一致时记 concern 并留档 `verdict_model`）；
3. 判定逻辑链逐条锁边界（blocked 一票否决 / 不完整驳回 / 低质量转人工 / duplicate 驳回 / similar 通过但标注）；
4. 确定性一票否决时**不调模型**（省成本，与编译侧"门禁先于模型"同款纪律）；
5. 契约违例 → 违例清单回灌重试；两次不合法 → failed 且**不写库**；
6. 幂等：已有 AI 审核结果的条目跳过（人工处置过的不重审）。
"""
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("core", "tools"):
    if str(ROOT / _sub) not in sys.path:
        sys.path.insert(0, str(ROOT / _sub))

import review_service  # noqa: E402
from sales_clarification_runtime import ModelResponse  # noqa: E402

ENTRY_REL = "pending_review/示例概念.md"
ENTRY_TITLE = "示例概念"
ENTRY_BODY = ("## 定义\n\n这是一个用于测试的概念定义，长度必须超过一百个中文字符才能通过完整性检查，"
              "因此这里刻意写长一点，把定义、背景与关键细节都覆盖到。\n\n"
              "## 背景\n\n背景说明也写在这里，确保正文足够长。\n\n## 关键细节\n\n- 细节一\n- 细节二\n\n"
              "## 关联知识\n\n- [[示例]]\n")


def _entry_text(title: str = ENTRY_TITLE, status: str = "pending") -> str:
    fm = {"type": "concept", "title": title, "status": status, "version": "V1.0",
          "source": "RAW/会议/示例.md", "department": "售前", "tags": ["售前"]}
    return f"---\n{yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)}---\n\n{ENTRY_BODY}"


def _model_output(**overrides) -> dict:
    payload = {
        "verdict": "approved", "department": "售前",
        "scores": {"completeness": "pass", "dedup": "pass", "quality": 4,
                   "sensitive": "pass", "compliance": "pass"},
        "duplicates": [], "concerns": [], "summary": "条目质量合格，建议通过审核",
    }
    payload.update(overrides)
    return payload


class ScriptedPort:
    def __init__(self, outputs: list[dict | str]):
        self._outputs = list(outputs)
        self.calls: list[dict] = []

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int = 2000,
                 **kwargs) -> ModelResponse:
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        payload = self._outputs.pop(0) if self._outputs else {}
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return ModelResponse(text, "fake-review-v1", 200, 80, "req-r")


@pytest.fixture()
def entry(tmp_path, monkeypatch):
    kb = tmp_path / "vault"
    for rel in ("pending_review", "NEXUS/概念", "NEXUS/资源", "_triggers/done"):
        (kb / rel).mkdir(parents=True, exist_ok=True)
    (kb / ENTRY_REL).write_text(_entry_text(), encoding="utf-8")
    monkeypatch.setenv("KB_ROOT", str(kb))
    return kb


def test_review_writes_pending_reviews_row(entry):
    """合法审核 → 写 pending_reviews（人工作业的输入），落库 JSON 形状与工作台/契约一致。"""
    import db
    result = review_service.review_one(ENTRY_REL, port=ScriptedPort([_model_output()]),
                                       submitter="ai_review")
    assert result.status == "reviewed" and result.verdict == "approved"
    row = db.find_review(ENTRY_REL)
    assert row["ai_verdict"] == "approved" and row["department"] == "售前"
    stored = row["ai_scores"]
    # 形状必须与 review_prompt.md 输出契约一致：工作台读 ai_scores.scores.<维度>，
    # review_router 还会拿它跑 validate_review_output 判 ai_scores_valid
    import output_schema
    assert output_schema.validate_review_output(stored) == []
    assert stored["scores"]["quality"] == 4 and stored["engine"] == "api"
    assert stored["verdict_model"] == "approved" and stored["verdict"] == "approved"


def test_deterministic_dimensions_override_model(entry):
    """完整性/敏感信息以**代码**为准：模型谎报 pass 也不影响判定。"""
    payload = _model_output(scores={"completeness": "pass", "dedup": "pass", "quality": 5,
                                    "sensitive": "pass", "compliance": "pass"})
    (entry / ENTRY_REL).write_text(_entry_text().replace("1", "1") + "身份证 110101199003071234\n",
                                   encoding="utf-8")
    result = review_service.review_one(ENTRY_REL, port=ScriptedPort([payload]))
    assert result.verdict == "rejected"                       # 代码判定 sensitive=blocked
    assert result.scores["sensitive"] == "blocked"


def test_blocked_entry_never_reaches_model(entry):
    """确定性一票否决：不信模型、也不烧 token。"""
    (entry / ENTRY_REL).write_text(_entry_text() + "\n内部资料：机密\n", encoding="utf-8")
    port = ScriptedPort([_model_output()])
    result = review_service.review_one(ENTRY_REL, port=port)
    assert result.verdict == "rejected" and port.calls == []
    assert any("确定性检查命中敏感信息" in c for c in result.concerns)


@pytest.mark.parametrize("scores,concerns,expected", [
    ({"sensitive": "blocked", "completeness": "pass", "quality": 5, "compliance": "pass", "dedup": "pass"}, [], "rejected"),
    ({"sensitive": "pass", "completeness": "insufficient", "quality": 5, "compliance": "pass", "dedup": "pass"}, [], "rejected"),
    ({"sensitive": "pass", "completeness": "pass", "quality": 2, "compliance": "pass", "dedup": "pass"}, [], "needs_human_review"),
    ({"sensitive": "warning", "completeness": "pass", "quality": 5, "compliance": "pass", "dedup": "pass"}, [], "needs_human_review"),
    ({"sensitive": "pass", "completeness": "pass", "quality": 5, "compliance": "flagged", "dedup": "pass"}, [], "needs_human_review"),
    ({"sensitive": "pass", "completeness": "pass", "quality": 5, "compliance": "pass", "dedup": "pass"}, ["a", "b", "c"], "needs_human_review"),
    ({"sensitive": "pass", "completeness": "pass", "quality": 5, "compliance": "pass", "dedup": "duplicate"}, [], "rejected"),
    ({"sensitive": "pass", "completeness": "pass", "quality": 5, "compliance": "pass", "dedup": "similar"}, [], "approved"),
    ({"sensitive": "pass", "completeness": "pass", "quality": 5, "compliance": "pass", "dedup": "pass"}, [], "approved"),
])
def test_verdict_chain_is_deterministic(scores, concerns, expected):
    """判定逻辑链逐条锁边界（与 prompts/review_prompt.md §判定逻辑一一对应）。"""
    assert review_service.decide_verdict(scores, concerns)[0] == expected


def test_model_verdict_mismatch_is_recorded_not_followed(entry):
    """模型自报 approved（它自称敏感信息 pass），而代码判定 sensitive=warning 需人工 → 按规则执行并留档差异。"""
    (entry / ENTRY_REL).write_text(_entry_text() + "\n联系电话：13800138000\n", encoding="utf-8")
    payload = _model_output()          # 模型自称六维全 pass、verdict=approved（自身契约合法）
    result = review_service.review_one(ENTRY_REL, port=ScriptedPort([payload]))
    assert result.scores["sensitive"] == "warning"
    assert result.verdict == "needs_human_review" and result.verdict_model == "approved"
    assert any("不一致" in c for c in result.concerns)


def test_contract_violation_is_fed_back_then_fails(entry):
    """契约违例 → 回灌重试；两次不合法 → failed 且**不写库**。"""
    import db
    bad = _model_output(scores={"completeness": "pass", "dedup": "pass", "quality": 9,
                                "sensitive": "pass", "compliance": "pass"})
    port = ScriptedPort([bad, bad])
    result = review_service.review_one(ENTRY_REL, port=port)
    assert result.status == "failed" and "quality" in result.error
    assert len(port.calls) == 2 and "未通过契约校验" in port.calls[1]["user_prompt"]
    assert db.find_review(ENTRY_REL) is None


def test_review_is_idempotent(entry):
    """已有 AI 审核结果 → 跳过，不再调模型。"""
    review_service.review_one(ENTRY_REL, port=ScriptedPort([_model_output()]))
    port = ScriptedPort([_model_output()])
    again = review_service.review_one(ENTRY_REL, port=port)
    assert again.status == "skipped" and port.calls == []


def test_dedup_candidates_and_scan(entry):
    """去重候选按标题词重合度确定性挑选；scan_pending 只返回未审条目。"""
    (entry / "NEXUS/概念/示例概念-详版.md").write_text(_entry_text("示例概念 详版", "active"),
                                                       encoding="utf-8")
    (entry / "NEXUS/概念/无关条目.md").write_text(_entry_text("完全无关", "active"), encoding="utf-8")
    candidates = review_service.dedup_candidates(ENTRY_REL, ENTRY_TITLE)
    assert [c["path"] for c in candidates] == ["NEXUS/概念/示例概念-详版.md"]
    assert candidates[0]["excerpt"]
    assert review_service.scan_pending() == [ENTRY_REL]
    review_service.review_one(ENTRY_REL, port=ScriptedPort([_model_output()]))
    assert review_service.scan_pending() == []


def test_similar_verdict_appends_concern_with_duplicate_path(entry):
    """similar → 通过但把重复来源写进 concerns（人工作业时能看到该和谁合并）。"""
    payload = _model_output(scores={"completeness": "pass", "dedup": "similar", "quality": 4,
                                    "sensitive": "pass", "compliance": "pass"},
                            duplicates=["NEXUS/概念/示例概念-详版.md"])
    result = review_service.review_one(ENTRY_REL, port=ScriptedPort([payload]))
    assert result.verdict == "approved"
    assert any("示例概念-详版" in c for c in result.concerns)


def test_worker_cli_engine_writes_trigger(entry, monkeypatch):
    """REVIEW_ENGINE=claude_cli：写 review 触发纸条而不调模型；非法值报错。"""
    import review_worker
    monkeypatch.setenv("REVIEW_ENGINE", "claude_cli")
    assert review_worker._engine() == "claude_cli"
    batch = review_worker.run_once(limit=1, engine="claude_cli")
    assert batch["queued"] == 1 and list((entry / "_triggers").glob("review_*.md"))
    monkeypatch.setenv("REVIEW_ENGINE", "bogus")
    with pytest.raises(ValueError):
        review_worker._engine()
