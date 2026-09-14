"""应用内编译引擎测试（`core/compile_service.py`）。

覆盖六条纪律：
1. 契约合法 → 资源摘要（active）落 `NEXUS/资源/`、概念页（pending）落 `pending_review/`，
   并双写 `knowledge_entries`、任务置 done、写 `compile_session` trace；
2. 落盘 frontmatter 必须过 `output_schema.validate_entry_frontmatter`（防"写出来不合规"）；
3. 契约违例 → **把违例清单回灌**重试 1 次（第二次 prompt 里能看到违例），再败标 failed 且**不落盘**；
4. 指纹幂等：同路径同指纹的 done 任务直接跳过（不再调模型）；
5. 门禁在模型之前：blocked 文档 failed 且**模型零调用**；
6. 概念页产出后写 review 触发纸条（审核阶段可接手）。
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("core", "tools"):
    if str(ROOT / _sub) not in sys.path:
        sys.path.insert(0, str(ROOT / _sub))

import compile_service  # noqa: E402
import ops  # noqa: E402
import output_schema  # noqa: E402
from sales_clarification_runtime import ModelResponse  # noqa: E402

RAW_REL = "RAW/会议/示例会议纪要.md"
RAW_TEXT = "示例会议纪要\n\n会议时间：2026-01-02\n议题：应急哨兵试点进度\n结论：下月进入联调。\n"


def _valid_output(title: str = "示例会议纪要") -> dict:
    return {
        "resource": {
            "title": title,
            "description": "示例提示：这是一份用于测试的会议纪要。",
            "tags": ["会议纪要", "应急管理"],
            "department": "售前",
            "summary": "## 摘要\n\n这是一份测试用会议纪要，记录试点进度与后续联调安排。\n\n## 关键信息\n\n- 试点进度已确认\n- 下月进入联调",
            "key_points": ["试点进度已确认", "下月进入联调"],
            "source_type": "会议",
        },
        "concepts": [{
            "title": "应急哨兵试点进度",
            "description": "记录试点进入联调阶段的时间点。",
            "tags": ["应急管理", "项目管理"],
            "department": "售前",
            "content": "## 定义\n\n试点进度指…\n\n## 背景\n\n…\n\n## 关键细节\n\n- 下月联调\n\n## 关联知识\n\n- [[示例]]",
            "related_to": ["示例"],
        }],
    }


class ScriptedPort:
    """按脚本返回模型输出；记录每次调用的 prompt（用于断言"违例回灌"）。"""

    def __init__(self, outputs: list[dict | str]):
        self._outputs = list(outputs)
        self.calls: list[dict] = []

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int = 4000,
                 **kwargs) -> ModelResponse:
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt,
                           "max_tokens": max_tokens})
        payload = self._outputs.pop(0) if self._outputs else {}
        import json
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return ModelResponse(text, "fake-compile-v1", 100, 50, "req-1")


@pytest.fixture()
def raw_file(tmp_path, monkeypatch):
    """在测试 KB_ROOT 下放一篇 RAW 文档，并准备 vault 目录树。"""
    kb = tmp_path / "vault"
    for rel in ("RAW/会议", "NEXUS/资源", "NEXUS/概念", "pending_review", "_triggers/done"):
        (kb / rel).mkdir(parents=True, exist_ok=True)
    (kb / "NEXUS/index.md").write_text("# 知识库索引\n\n## 资源\n\n## 概念\n", encoding="utf-8")
    (kb / RAW_REL).write_text(RAW_TEXT, encoding="utf-8")
    monkeypatch.setenv("KB_ROOT", str(kb))
    return kb


def _entries(path_prefix: str) -> list[dict]:
    import db
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT path, type, title, status, fingerprint FROM knowledge_entries "
            "WHERE path LIKE %s ORDER BY path", (path_prefix + "%",)).fetchall()
    return [{"path": r[0], "type": r[1], "title": r[2], "status": r[3], "fingerprint": r[4]}
            for r in rows]


def test_valid_output_writes_products_and_bookkeeping(raw_file):
    """合法输出：资源(active) + 概念(pending) 落盘、双写 PG、任务 done、trace 落库。"""
    import db
    port = ScriptedPort([_valid_output()])
    result = compile_service.compile_one(RAW_REL, port=port)

    assert result.status == "done" and result.error is None
    assert result.resource_path and result.resource_path.startswith("NEXUS/资源/")
    assert len(result.concept_paths) == 1 and result.concept_paths[0].startswith("pending_review/")
    assert (raw_file / result.resource_path).exists()
    assert (raw_file / result.concept_paths[0]).exists()

    # frontmatter 必须过契约（SCHEMA.md 三类标签命名空间 + type/status/title/source）
    import yaml
    for rel in [result.resource_path, *result.concept_paths]:
        fm = yaml.safe_load((raw_file / rel).read_text(encoding="utf-8").split("---", 2)[1])
        assert output_schema.validate_entry_frontmatter(fm) == []

    # 双写：资源 active，概念 pending
    entries = _entries("NEXUS/资源/") + _entries("pending_review/")
    assert {e["status"] for e in entries} == {"active", "pending"}
    assert all(e["fingerprint"] for e in entries)

    # 任务状态
    assert db.latest_compile_task(RAW_REL)["status"] == "done"


def test_index_and_review_trigger_written(raw_file):
    """资源摘要进 index.md 的资源节；概念页写 review 触发纸条；批次写 compile_session trace。"""
    import db
    batch = compile_service.compile_batch([RAW_REL], port=ScriptedPort([_valid_output()]))
    assert batch["compiled"] == 1
    index = (raw_file / "NEXUS/index.md").read_text(encoding="utf-8")
    assert "## 资源" in index and "NEXUS/资源/" in index
    assert "资源 1 篇" in index                      # 头部统计行已刷新
    papers = list((raw_file / "_triggers").glob("review_*.md"))
    assert len(papers) == 1 and "pending_review/" in papers[0].read_text(encoding="utf-8")

    # 每个批次一条 compile_session trace，且标注引擎（可区分 api / claude_cli）
    with db.get_conn() as conn:
        trace = conn.execute(
            "SELECT trace_id, status, latency_ms, detail FROM trace_events "
            "WHERE span_type='compile_session' ORDER BY id DESC LIMIT 1").fetchone()
    assert trace is not None
    assert trace[0] == batch["trace_id"] and trace[1] == "ok"
    assert trace[3]["compiled"] == 1 and trace[3]["engine"] == "api"


def test_contract_violation_is_fed_back_then_fails_without_writing(raw_file):
    """违例 → 回灌清单重试；两次都不合法 → failed 且**不落盘**。"""
    bad = _valid_output()
    del bad["resource"]["source_type"]           # 触发枚举违例
    port = ScriptedPort([bad, bad])
    result = compile_service.compile_one(RAW_REL, port=port)

    assert result.status == "failed" and "source_type" in result.error
    assert len(port.calls) == 2
    assert "未通过契约校验" not in port.calls[0]["user_prompt"]
    assert "未通过契约校验" in port.calls[1]["user_prompt"]      # 违例清单已回灌
    assert not list((raw_file / "NEXUS/资源").glob("*.md"))
    assert not list((raw_file / "pending_review").glob("*.md"))
    assert _entries("NEXUS/资源/") == [] and _entries("pending_review/") == []


def test_retry_recovers_on_second_attempt(raw_file):
    """第一次契约违例 → 第二次按清单修正 → 编译成功。"""
    bad = _valid_output()
    del bad["resource"]["department"]
    batch = compile_service.compile_batch([RAW_REL], port=ScriptedPort([bad, _valid_output()]))
    assert batch["compiled"] == 1 and batch["failed"] == 0


def test_fingerprint_idempotency_skips_llm(raw_file):
    """同路径同指纹已 done → 跳过，不再调用模型（省钱且幂等）。"""
    compile_service.compile_one(RAW_REL, port=ScriptedPort([_valid_output()]))
    port = ScriptedPort([_valid_output()])
    again = compile_service.compile_one(RAW_REL, port=port)
    assert again.status == "skipped" and port.calls == []

    # 内容变化（指纹变）→ 重新编译
    (raw_file / RAW_REL).write_text(RAW_TEXT + "\n补充：联调时间提前一周。\n", encoding="utf-8")
    changed = compile_service.compile_one(RAW_REL, port=ScriptedPort([_valid_output("示例会议纪要2")]))
    assert changed.status == "done"


def test_blocked_document_never_reaches_model(raw_file):
    """门禁 blocked：直接 failed，模型零调用（敏感信息不进 LLM）。"""
    (raw_file / RAW_REL).write_text("会议纪要（内部资料）\n身份证 110101199003071234\n", encoding="utf-8")
    port = ScriptedPort([_valid_output()])
    result = compile_service.compile_one(RAW_REL, port=port)
    assert result.status == "failed" and "门禁" in result.error
    assert port.calls == []


def test_scan_new_raw_excludes_seen_paths(raw_file):
    """RAW 增量扫描：进过 compile_tasks 的路径不再算新（失败任务也不会形成风暴）。"""
    assert compile_service.scan_new_raw() == [RAW_REL]
    compile_service.compile_one(RAW_REL, port=ScriptedPort([_valid_output()]))
    assert compile_service.scan_new_raw() == []


def test_worker_switches_engine(raw_file, monkeypatch):
    """COMPILE_ENGINE=claude_cli 时写触发纸条而不调模型；非法值直接报错。"""
    import compile_worker
    monkeypatch.setenv("COMPILE_ENGINE", "claude_cli")
    assert compile_worker._engine() == "claude_cli"
    batch = compile_worker.run_once(limit=1, engine="claude_cli")
    assert batch["queued"] == 1
    assert list((raw_file / "_triggers").glob("compile_*.md"))

    monkeypatch.setenv("COMPILE_ENGINE", "no_such_engine")
    with pytest.raises(ValueError):
        compile_worker._engine()


def test_freeform_tags_are_sanitized_to_schema_namespace(raw_file):
    """回归（真实数据暴露）：模型会给出不在命名空间内的标签，落盘前必须清洗，否则违反落库契约。"""
    payload = _valid_output()
    payload["resource"]["tags"] = ["政策规划", "十五五规划", "共享层", "国家战略"]
    payload["concepts"][0]["tags"] = ["新质生产力", "高质量发展", "项目管理"]
    result = compile_service.compile_one(RAW_REL, port=ScriptedPort([payload]))
    assert result.status == "done"

    import yaml
    res_fm = yaml.safe_load((raw_file / result.resource_path).read_text(encoding="utf-8").split("---", 2)[1])
    con_fm = yaml.safe_load((raw_file / result.concept_paths[0]).read_text(encoding="utf-8").split("---", 2)[1])
    assert res_fm["tags"] == ["售前", "共享层"]                # 自由标签被丢弃，部门标签补首位
    assert con_fm["tags"] == ["售前", "项目管理"]
    assert output_schema.validate_entry_frontmatter(res_fm) == []
    assert output_schema.validate_entry_frontmatter(con_fm) == []


def test_write_markdown_rejects_noncompliant_frontmatter(raw_file):
    """frontmatter 不合契约时**抛错不落盘**（门禁进 loop）。"""
    bad = {"type": "resource", "title": "x", "status": "active", "source": "RAW/x.md",
           "version": "1.0", "tags": ["不合规标签"]}
    with pytest.raises(ValueError):
        compile_service._write_markdown(raw_file / "NEXUS/资源/坏条目.md", bad, "正文")


def test_missing_raw_file_fails_cleanly(raw_file):
    result = compile_service.compile_one("RAW/会议/不存在.md", port=ScriptedPort([]))
    assert result.status == "failed" and "不存在" in result.error
