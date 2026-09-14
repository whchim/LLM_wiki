#!/usr/bin/env python3
"""审核 Worker：对 `pending_review/` 下未审条目跑六维度审核（应用内引擎）。

用法：
    python tools/review_worker.py --once            # 审完当前待审条目后退出
    python tools/review_worker.py --once --limit 3  # 只审 3 条（试跑）
    python tools/review_worker.py --file pending_review/某项.md
    python tools/review_worker.py                   # 常驻轮询（默认间隔 15s）

环境变量：
    REVIEW_ENGINE=api（默认，应用内）| claude_cli（写触发纸条，交 watcher 走 review_workflow.md）
    KB_ROOT / REVIEW_MAX_TOKENS / WORKER_INTERVAL

审核只写 `pending_reviews`（AI 判定 + 六维分数）；**通过/驳回永远由人在工作台点**
（`/reviews/{id}/approve|reject` → `ops.approve_entry` 移文件 + 双写），模型不直接发布条目。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import review_service  # noqa: E402

ENGINES = {"api", "claude_cli"}


def _engine() -> str:
    engine = os.environ.get("REVIEW_ENGINE", "api").strip().lower()
    if engine not in ENGINES:
        raise ValueError(f"REVIEW_ENGINE 只能是 {'/'.join(sorted(ENGINES))}，实际：{engine!r}")
    return engine


def run_once(limit: int | None, engine: str, files: list[str] | None = None) -> dict:
    if engine == "claude_cli":
        import ops
        targets = files if files else review_service.scan_pending(limit)
        if not targets:
            print("[worker] 无待审条目，退出")
            return {"reviewed": 0, "skipped": 0, "failed": 0, "results": []}
        paper = ops.write_trigger("review", targets, "review_worker")
        print(f"[worker] engine=claude_cli 已写触发纸条 {paper.name}（{len(targets)} 条）；"
              f"请运行 tools/trigger_watcher.py 让 headless Claude Code 消费")
        return {"reviewed": 0, "skipped": 0, "failed": 0, "results": [],
                "trigger": paper.name, "queued": len(targets)}

    targets = files if files else review_service.scan_pending(limit)
    if not targets:
        print("[worker] 无待审条目，退出")
        return {"reviewed": 0, "skipped": 0, "failed": 0, "results": []}
    max_tokens = int(os.environ.get("REVIEW_MAX_TOKENS", review_service.DEFAULT_MAX_TOKENS))
    batch = review_service.review_batch(targets, submitter="ai_review")
    print(f"[worker] engine=api reviewed={batch['reviewed']} skipped={batch['skipped']} "
          f"failed={batch['failed']} latency={batch['latency_ms']}ms verdicts={batch['verdicts']}")
    for item in batch["results"]:
        line = f"  - {item['nexus_path']} → {item['status']}"
        if item["verdict"]:
            line += f" | verdict={item['verdict']}"
            if item["verdict_model"] and item["verdict_model"] != item["verdict"]:
                line += f"（模型自报 {item['verdict_model']}）"
            line += f" | 部门={item['department']} | 质量={item['scores'].get('quality')}"
        if item["concerns"]:
            line += f" | concerns={len(item['concerns'])}"
        if item["error"]:
            line += f" | 错误：{item['error']}"
        print(line)
    return batch


def main() -> int:
    parser = argparse.ArgumentParser(description="审核 Worker（应用内引擎 / CLI 引擎）")
    parser.add_argument("--once", action="store_true", help="审一轮即退出（默认常驻轮询）")
    parser.add_argument("--limit", type=int, default=None, help="本轮最多审核条数")
    parser.add_argument("--file", action="append", default=None, help="指定待审条目路径（可重复）")
    parser.add_argument("--json", action="store_true", help="额外输出机器可读 JSON 汇总")
    args = parser.parse_args()

    try:
        engine = _engine()
    except ValueError as exc:
        print(f"[worker] {exc}", file=sys.stderr)
        return 2

    interval = int(os.environ.get("WORKER_INTERVAL", "15"))
    print(f"[worker] KB_ROOT={review_service.kb_root()} engine={engine}")
    while True:
        batch = run_once(args.limit, engine, args.file)
        if args.json:
            print(json.dumps(batch, ensure_ascii=False))
        if args.once:
            return 1 if batch.get("failed") else 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
