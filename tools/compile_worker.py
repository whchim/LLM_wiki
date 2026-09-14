#!/usr/bin/env python3
"""编译 Worker：消费 RAW 增量并驱动编译引擎（应用内 API 引擎 / Claude Code CLI 二选一）。

用法：
    python tools/compile_worker.py --once            # 扫一次 RAW 增量并编译后退出
    python tools/compile_worker.py --once --limit 5  # 只编译前 5 篇（试跑）
    python tools/compile_worker.py --file RAW/会议/xx.md   # 指定文件
    python tools/compile_worker.py                   # 常驻轮询（默认间隔 10s）

环境变量：
    COMPILE_ENGINE   api（默认，应用内引擎，云端生产用）| claude_cli（本地开发/开放任务用）
    KB_ROOT          知识库根（默认仓库内 vault/；可指向仓库外的真实语料 vault）
    COMPILE_MAX_TOKENS  单篇编译输出上限（默认 4000）
    WORKER_INTERVAL  常驻模式轮询间隔秒数（默认 10）

两种引擎的关系（详见 docs/WIKI-70）：
- api：`core/compile_service.py` 直调 ModelPort，走同一份契约（compile_prompt + output_schema），
  可容器化、可计量、可单测、可多租户隔离——**云端生产路径**；
- claude_cli：沿用触发文件 + `claude -p /process-triggers`（`tools/trigger_watcher.py`），
  自主性更强（Agent 自己决定怎么拆页/互链/修正），但依赖宿主机 CLI 与登录态——**本地开发路径**。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import compile_service  # noqa: E402

ENGINES = {"api", "claude_cli"}


def _engine() -> str:
    engine = os.environ.get("COMPILE_ENGINE", "api").strip().lower()
    if engine not in ENGINES:
        raise ValueError(f"COMPILE_ENGINE 只能是 {'/'.join(sorted(ENGINES))}，实际：{engine!r}")
    return engine


def _report(batch: dict, engine: str) -> None:
    """输出处理报告（编译/缓存/失败 + 逐篇状态）。"""
    print(f"[worker] engine={engine} trace_id={batch['trace_id']} "
          f"compiled={batch['compiled']} cached={batch['cached']} failed={batch['failed']} "
          f"latency={batch['latency_ms']}ms")
    for item in batch["results"]:
        line = f"  - {item['raw_path']} → {item['status']}"
        if item["resource_path"]:
            line += f" | 资源 {item['resource_path']}"
        if item["concepts"]:
            line += f" | 概念 {item['concepts']} 篇"
        if item["error"]:
            line += f" | 错误：{item['error']}"
        print(line)


def run_queue(limit: int | None = None, worker_id: str | None = None,
              tenant_id: str | None = None, lease_seconds: int | None = None) -> dict:
    """**队列模式（L2）**：从 `compile_tasks` 认领任务（FOR UPDATE SKIP LOCKED）并编译。

    - 认领即占租约（默认 600s）：worker 崩溃后租约过期，任务自动回到可认领态（自愈）；
    - 失败按指数退避重排（`attempts` 用尽才落终态 failed，人工「重试」走 requeue）；
    - 多个 worker 进程/容器可并发跑同一队列，互不抢任务。
    """
    import db
    import uuid as _uuid

    worker_id = worker_id or f"{os.environ.get('HOSTNAME', 'local')}-{os.getpid()}"
    lease = lease_seconds or db.DEFAULT_LEASE_SECONDS
    max_tokens = int(os.environ.get("COMPILE_MAX_TOKENS", compile_service.DEFAULT_MAX_TOKENS))
    results: list = []
    started = time.perf_counter()
    while limit is None or len(results) < limit:
        task = db.claim_compile_task(worker_id, lease_seconds=lease, tenant_id=tenant_id)
        if task is None:
            break
        print(f"[worker] 认领 #{task['id']} {task['raw_path']}"
              f"（第 {task['attempts']} 次尝试，tenant={task['tenant_id']}）")
        result = compile_service.compile_one(task["raw_path"], max_tokens=max_tokens,
                                            task_id=task["id"])
        results.append(result)
        if result.status == "done":
            db.finish_compile_task(task["id"], "done", nexus_path=result.resource_path)
        elif result.status == "skipped":
            db.finish_compile_task(task["id"], "cached")
        else:
            exhausted = task["attempts"] >= db.DEFAULT_MAX_ATTEMPTS
            if exhausted:
                db.finish_compile_task(task["id"], "failed", error_msg=result.error)
            else:
                backoff = 60 * (2 ** (task["attempts"] - 1))     # 1/2/4 分钟指数退避
                db.finish_compile_task(task["id"], "pending", error_msg=result.error,
                                       retry_backoff_seconds=backoff)
                print(f"[worker] #{task['id']} 失败，退避 {backoff}s 后重试：{result.error}")

    latency_ms = int((time.perf_counter() - started) * 1000)
    trace_id = _uuid.uuid4().hex
    compile_service._record_session_trace(trace_id=trace_id, results=results, latency_ms=latency_ms)
    return {
        "mode": "queue", "worker_id": worker_id, "tenant_id": tenant_id or "default",
        "compiled": sum(1 for r in results if r.status == "done"),
        "cached": sum(1 for r in results if r.status in ("cached", "skipped")),
        "failed": sum(1 for r in results if r.status == "failed"),
        "files": [p for r in results for p in r.produced],
        "latency_ms": latency_ms, "trace_id": trace_id,
        "queue": db.queue_stats(tenant_id),
        "results": [r.audit_dict() for r in results],
    }


def run_once(limit: int | None, engine: str, files: list[str] | None = None) -> dict:
    """扫一轮 RAW 增量（或指定文件）并编译。"""
    if engine == "claude_cli":
        return _run_claude_cli(limit)
    targets = files if files else compile_service.scan_new_raw(limit)
    if not targets:
        print("[worker] 无新的 RAW 文件，退出")
        return {"compiled": 0, "cached": 0, "failed": 0, "results": [], "trace_id": None}
    max_tokens = int(os.environ.get("COMPILE_MAX_TOKENS", compile_service.DEFAULT_MAX_TOKENS))
    batch = compile_service.compile_batch(targets, max_tokens=max_tokens)
    _report(batch, engine)
    return batch


def _run_claude_cli(limit: int | None) -> dict:
    """CLI 引擎：写触发纸条 → 由 watcher 唤起 headless Claude Code 消费（本函数不直接调 CLI）。"""
    import ops
    from pathlib import Path as _Path
    targets = compile_service.scan_new_raw(limit)
    if not targets:
        print("[worker] 无新的 RAW 文件，退出")
        return {"compiled": 0, "cached": 0, "failed": 0, "results": [], "trace_id": None}
    paper = ops.write_trigger("compile", targets, "compile_worker")
    print(f"[worker] engine=claude_cli 已写触发纸条 {paper.name}（{len(targets)} 篇）；"
          f"请运行 tools/trigger_watcher.py 让 headless Claude Code 消费")
    return {"compiled": 0, "cached": 0, "failed": 0, "results": [],
            "trace_id": None, "trigger": str(_Path(paper).name), "queued": len(targets)}


def main() -> int:
    parser = argparse.ArgumentParser(description="编译 Worker（应用内引擎 / CLI 引擎 / 队列模式）")
    parser.add_argument("--once", action="store_true", help="扫一轮即退出（默认常驻轮询）")
    parser.add_argument("--queue", action="store_true",
                        help="队列模式（L2）：从 compile_tasks 认领任务而不是扫 RAW 增量")
    parser.add_argument("--limit", type=int, default=None, help="本轮最多处理篇数")
    parser.add_argument("--file", action="append", default=None,
                        help="指定 RAW 相对路径（相对 KB_ROOT；可重复传）")
    parser.add_argument("--tenant", default=None, help="只认领该租户的任务（默认全部）")
    parser.add_argument("--lease-seconds", type=int, default=None, help="任务租约秒数（默认 600）")
    parser.add_argument("--json", action="store_true", help="额外输出机器可读的 JSON 汇总")
    args = parser.parse_args()

    try:
        engine = _engine()
    except ValueError as exc:
        print(f"[worker] {exc}", file=sys.stderr)
        return 2
    if args.queue and engine == "claude_cli":
        print("[worker] 队列模式只支持 engine=api（CLI 驱动走触发文件，不用 PG 队列）", file=sys.stderr)
        return 2

    interval = int(os.environ.get("WORKER_INTERVAL", "10"))
    print(f"[worker] KB_ROOT={compile_service.kb_root()} engine={engine}"
          f"{' mode=queue' if args.queue else ''}")
    while True:
        batch = (run_queue(args.limit, tenant_id=args.tenant, lease_seconds=args.lease_seconds)
                 if args.queue else run_once(args.limit, engine, args.file))
        if args.json:
            print(json.dumps(batch, ensure_ascii=False))
        elif args.queue:
            print(f"[worker] 队列本轮 compiled={batch['compiled']} cached={batch['cached']} "
                  f"failed={batch['failed']} 队列快照={batch['queue']}")
        if args.once:
            return 1 if batch.get("failed") else 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
