#!/usr/bin/env python3
"""SP2.5 可观测性：编译过程 Trace 采集工具（确定性 CLI）。

被 compile_workflow.md 步骤 6 与 /process-triggers 命令末尾调用：
把一次编译会话的结果写入 trace_events（span_type=compile_session）。

用法：
    python tools/record_compile_trace.py \
        --trace-id <uuid> \
        --operation batch \
        --compiled 5 --cached 3 --failed 0 \
        --files '["NEXUS/资源/a.md", "pending_review/概念b.md"]' \
        [--latency-ms 12000]

参数缺失/值非法时：输出错误到 stderr 并退出 1（不写库）——
保证只有真实编译结果才落 trace，不做"空 trace"污染。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

import db  # 复用连接池 + 环境变量（DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS）


def main() -> int:
    parser = argparse.ArgumentParser(description="记录一次编译会话的 Trace")
    parser.add_argument("--trace-id", required=True, help="编译会话 UUID（trace_id 分组键）")
    parser.add_argument("--operation", default="batch", help="细分动作，默认 batch")
    parser.add_argument("--compiled", type=int, default=0, help="LLM 编译成功页数")
    parser.add_argument("--cached", type=int, default=0, help="指纹缓存命中的文件数")
    parser.add_argument("--failed", type=int, default=0, help="失败文件数")
    parser.add_argument("--files", default="[]", help='JSON 数组：本次产出的文件路径列表')
    parser.add_argument("--latency-ms", type=int, default=0, help="本次编译会话耗时（毫秒）")
    parser.add_argument("--status", default="ok", choices=["ok", "error"],
                        help="会话整体状态，默认 ok")
    args = parser.parse_args()

    try:
        files = json.loads(args.files)
        assert isinstance(files, list)
    except (json.JSONDecodeError, AssertionError):
        print(f"[trace] --files 不是合法 JSON 数组: {args.files!r}", file=sys.stderr)
        return 1

    detail = {
        "compiled": args.compiled,
        "cached": args.cached,
        "failed": args.failed,
        "files": files[:100],
    }
    try:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO trace_events (span_type, trace_id, operation, status, "
                "latency_ms, detail, operator) "
                "VALUES ('compile_session', %s, %s, %s, %s, %s, 'system')",
                (args.trace_id, args.operation, args.status, args.latency_ms,
                 json.dumps(detail)))
        print(f"[trace] 已记录 compile_session（trace_id={args.trace_id}）")
        return 0
    except Exception as e:
        print(f"[trace] 写入 trace_events 失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())