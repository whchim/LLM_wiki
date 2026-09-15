"""应用内问答服务测试（对话窗口后端 `core/answer_service.py`）。

锁住五条纪律（对应 docs/WIKI-80）：
1. **门禁先于模型**：检索无依据 → 一次模型都不调，如实说"知识库没有"，并记知识缺口；
2. **引用必须可溯源**：路径不在检索结果里 / quote 不是逐字原文 → 契约违例 → 回灌重试 1 次；
3. **两次违例不作答**：`status=failed` + `contract_ok=false`，绝不把编造的答案端出去；
4. **不静默降级**：模型未配置 → `failed` + 明确错误（API 层转 409），不给"像答案"的占位文本；
5. **缺口回流**：没答出来（no_hits / failed / insufficient）→ `search_logs.match_count=0`，
   进自增长看板；正常作答 → 记命中数。

检索侧另锁：**只看得见本租户子树**（L3.5 文件分区）+ 正文读取拒绝路径穿越。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("core", "tools", "api"):
    if str(ROOT / _sub) not in sys.path:
        sys.path.insert(0, str(ROOT / _sub))

import answer_service  # noqa: E402
import db  # noqa: E402
import paths  # noqa: E402
import retrieval  # noqa: E402
from sales_clarification_runtime import ModelResponse  # noqa: E402

BODY = "# 示例监测产品\n\n监测产品支持**私有化部署**与**SaaS 订阅**两种模式。\n"
PATH_A = "NEXUS/概念/示例监测产品.md"
QUOTE = "监测产品支持**私有化部署**与**SaaS 订阅**两种模式。"


def _entry(path=PATH_A, score=0.8, title="示例监测产品", content=BODY, **extra):
    e = {"path": path, "title": title, "score": score,
         "channels": {"grep": 1, "vector": 1}, "similarity": 0.81,
         "content": content, "truncated": False}
    e.update(extra)
    return e


def _retriever(entries, *, gap=False, channels=None, warnings=None):
    """假的检索结果（不碰文件系统/数据库）。"""
    def _run(query, **kwargs):
        return {"query": query, "entries": entries, "gap": gap,
                "channels": channels or {"grep": len(entries), "vector": len(entries)},
                "max_sim": 0.81 if entries else None, "warnings": warnings or []}
    return _run


class _Port:
    """假模型：按顺序吐出预设输出，并记录调用次数。"""

    def __init__(self, payloads, model="fake-answer"):
        self.payloads = [p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)
                         for p in payloads]
        self.calls = 0
        self.model = model

    def complete(self, *, system_prompt, user_prompt, max_tokens=4000, **kwargs):
        payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        self.last_user_prompt = user_prompt
        return ModelResponse(payload, self.model, 120, 60, "req-1")


def _valid_payload(**over):
    payload = {"answer": "示例监测产品支持私有化部署与 SaaS 订阅两种模式。",
               "citations": [{"path": PATH_A, "quote": QUOTE, "note": "部署模式"}],
               "insufficient": False, "followups": ["私有化部署的交付周期？"]}
    payload.update(over)
    return payload


def _logs(limit=5):
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT query, match_count, source FROM search_logs ORDER BY id DESC LIMIT %s",
            (limit,)).fetchall()


# ---------- 1. 门禁先于模型 ----------

def test_no_hits_skips_model_and_logs_gap():
    port = _Port([_valid_payload()])
    result = answer_service.ask("查不到的问题", retriever=_retriever([], gap=True), port=port)
    assert result.status == "no_hits"
    assert result.insufficient is True
    assert port.calls == 0, "无检索结果时不得调用模型（不硬答）"
    assert "暂无" in result.answer
    assert _logs(1)[0][:3] == ("查不到的问题", 0, "ask")


def test_entries_without_content_are_not_grounds(tmp_path, monkeypatch):
    """向量命中的条目若正文读不出来（文件已删），不能当依据 → 同样不调模型。"""
    port = _Port([_valid_payload()])
    entries = [_entry(content=None)]
    result = answer_service.ask("正文不可读", retriever=_retriever(entries), port=port)
    assert result.status == "no_hits"
    assert port.calls == 0


# ---------- 2/3. 引用溯源 + 契约违例回灌 ----------

def test_citation_path_must_be_retrieved():
    """编造来源 → 第一次违例、回灌后改正 → 作答（attempts 留痕）。"""
    bad = _valid_payload(citations=[{"path": "NEXUS/概念/不存在的条目.md",
                                     "quote": QUOTE, "note": "编的"}])
    port = _Port([bad, _valid_payload()])
    result = answer_service.ask("部署模式", retriever=_retriever([_entry()]), port=port)
    assert result.status == "answered"
    assert port.calls == 2, "违例必须回灌重试一次"
    assert result.attempts[0]["status"] == "contract_error"
    assert result.attempts[1]["status"] == "accepted"
    assert "不在本次检索结果中" in result.attempts[0]["error"]
    assert result.citations[0]["path"] == PATH_A


def test_fabricated_quote_is_rejected_then_answered():
    bad = _valid_payload(citations=[{"path": PATH_A, "quote": "支持量子加密部署", "note": "编的"}])
    port = _Port([bad, _valid_payload()])
    result = answer_service.ask("部署模式", retriever=_retriever([_entry()]), port=port)
    assert result.status == "answered"
    assert "找不到" in result.attempts[0]["error"]


def test_two_contract_violations_fail_loudly():
    """两次都编造来源 → 明确失败，**不返回答案**（宁可不答也不糊弄）。"""
    bad = _valid_payload(citations=[{"path": "NEXUS/概念/编的.md", "quote": QUOTE, "note": ""}])
    port = _Port([bad, bad])
    result = answer_service.ask("部署模式", retriever=_retriever([_entry()]), port=port)
    assert result.status == "failed"
    assert result.contract_ok is False
    assert result.answer is None
    assert "契约校验" in result.error
    assert _logs(1)[0][1] == 0, "没答出来也要记缺口"


def test_no_citation_with_confident_answer_is_violation():
    """insufficient=false 却零引用 → 契约违例（防"无来源却给确定答案"）。"""
    bad = _valid_payload(citations=[])
    port = _Port([bad, bad])
    result = answer_service.ask("部署模式", retriever=_retriever([_entry()]), port=port)
    assert result.status == "failed"
    assert "至少要给出一条可溯源引用" in result.attempts[0]["error"]


def test_model_exception_is_retried_then_fails():
    class _Boom:
        def __init__(self):
            self.calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            raise RuntimeError("upstream 502")

    port = _Boom()
    result = answer_service.ask("部署模式", retriever=_retriever([_entry()]), port=port)
    assert port.calls == 2
    assert result.status == "failed"
    assert "upstream 502" in result.attempts[-1]["error"]


# ---------- 4. 正常作答 + 记账 ----------

def test_answered_records_usage_and_logs_hit():
    port = _Port([_valid_payload()])
    result = answer_service.ask("部署模式有哪些", retriever=_retriever([_entry(), _entry("NEXUS/资源/纪要.md")]),
                                port=port)
    assert result.status == "answered"
    assert result.contract_ok is True
    assert result.citations and result.citations[0]["excerpt"].startswith("监测产品")
    assert result.followups == ["私有化部署的交付周期？"]
    assert (result.input_tokens, result.output_tokens) == (120, 60)
    assert result.model == "fake-answer"
    assert result.trace_id and len(result.trace_id) == 32
    assert _logs(1)[0][1] == 2, "正常作答记命中条数"
    with db.get_conn() as conn:
        span = conn.execute(
            "SELECT status, detail->>'citations' FROM trace_events WHERE span_type='ask' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    assert span[0] == "ok" and span[1] == "1"


def test_model_prompt_carries_retrieved_bodies():
    """依据必须真的进了 prompt（否则"基于检索结果"是空话）。"""
    port = _Port([_valid_payload()])
    answer_service.ask("部署模式", retriever=_retriever([_entry()]), port=port)
    assert PATH_A in port.last_user_prompt
    assert "私有化部署" in port.last_user_prompt


def test_insufficient_answer_counts_as_gap():
    port = _Port([_valid_payload(answer="知识库中未找到直接信息。", citations=[], insufficient=True)])
    result = answer_service.ask("合同金额是多少", retriever=_retriever([_entry()]), port=port)
    assert result.status == "insufficient"
    assert result.insufficient is True
    assert _logs(1)[0][1] == 0


# ---------- 5. 不静默降级 ----------

def test_missing_model_fails_loudly(monkeypatch):
    monkeypatch.setattr(answer_service.model_port, "for_tenant", lambda *a, **k: None)
    result = answer_service.ask("部署模式", retriever=_retriever([_entry()]))
    assert result.status == "failed"
    assert "未配置" in result.error


def test_empty_question_fails_without_calling_anything():
    port = _Port([_valid_payload()])
    result = answer_service.ask("   ", retriever=_retriever([_entry()]), port=port)
    assert result.status == "failed" and result.error == "问题为空"
    assert port.calls == 0


# ---------- 检索侧：租户分区与路径安全 ----------

def _write_entry(root: Path, rel: str, title: str, body: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    fm = (f"---\ntype: concept\ntitle: {title}\nstatus: active\nsource: 测试\n"
          f"version: V1.0\n---\n\n")
    target.write_text(fm + body, encoding="utf-8")


def test_retrieval_only_sees_own_tenant_files(tmp_path, monkeypatch):
    """L3.5：同一 KB_ROOT 下两个租户**同名条目**各自检索，读到的都是自己那份。"""
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    for tenant in ("tenant-a", "tenant-b"):
        root = paths.ensure_tenant_tree(tenant)
        _write_entry(root, "NEXUS/概念/专属知识.md", f"{tenant} 专属知识",
                     f"{tenant} 的私有化部署说明，仅本租户可见。")

    token = db.bind_tenant("tenant-a")
    try:
        got = retrieval.run_search("私有化部署", mode="grep", with_content=True)
    finally:
        db.reset_tenant(token)
    assert [e["path"] for e in got["entries"]] == ["NEXUS/概念/专属知识.md"]
    assert got["entries"][0]["title"] == "tenant-a 专属知识"
    assert got["entries"][0]["content"].startswith("tenant-a 的私有化部署说明")

    token = db.bind_tenant("tenant-b")
    try:
        got_b = retrieval.run_search("私有化部署", mode="grep", with_content=True)
    finally:
        db.reset_tenant(token)
    assert [e["path"] for e in got_b["entries"]] == ["NEXUS/概念/专属知识.md"]   # 同名
    assert got_b["entries"][0]["title"] == "tenant-b 专属知识"                    # 内容各归其主
    assert got_b["entries"][0]["content"].startswith("tenant-b 的私有化部署说明")


def test_read_entry_rejects_traversal_and_outside_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    root = paths.ensure_tenant_tree("default")
    _write_entry(root, "NEXUS/概念/正常.md", "正常", "正文")
    (tmp_path / "secret.md").write_text("---\nstatus: active\n---\n机密", encoding="utf-8")
    (root / "RAW").mkdir(parents=True, exist_ok=True)
    (root / "RAW" / "草稿.md").write_text("---\nstatus: active\n---\n草稿", encoding="utf-8")

    assert retrieval.read_entry("NEXUS/概念/正常.md")["title"] == "正常"
    assert retrieval.read_entry("../secret.md") is None
    assert retrieval.read_entry("NEXUS/../../secret.md") is None
    assert retrieval.read_entry("RAW/草稿.md") is None, "RAW 未编译内容不得作为问答依据"
