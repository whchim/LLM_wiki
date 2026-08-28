#!/usr/bin/env python3
"""SP2.5 LLM 可观测性最小探针（验证用，非侵入）。

目的：证明"把 Claude Code 编译的 token 元数据送到 Langfuse"这条链路可行，
为将来决定"是否值得为 LLM 深度 trace 重构 Agent 驱动"提供依据。

用法（需先配置 LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY/LANGFUSE_HOST）：
    python tools/langfuse_probe.py --trace-id <uuid> --operation compile \
        --input-tokens 1200 --output-tokens 800 [--cost-usd 0.01]

不配置环境变量时退出 1 并提示（默认零侵入）。
"""
import argparse
import os
import sys
import time

LANGFUSE_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")


def main() -> int:
    missing = [k for k in LANGFUSE_ENV if not os.environ.get(k)]
    if missing:
        print(f"[langfuse-probe] 未配置 {missing}——跳过（探针默认关闭，零侵入）")
        print("[langfuse-probe] 提示：这是最小验证探针，不改变现有编译链路；"
              "将来需要 LLM 深度 trace 时再开启。")
        return 1

    parser = argparse.ArgumentParser(description="Langfuse 最小探针（LLM trace 验证）")
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--operation", default="compile")
    parser.add_argument("--input-tokens", type=int, default=0)
    parser.add_argument("--output-tokens", type=int, default=0)
    parser.add_argument("--cost-usd", type=float, default=0.0)
    args = parser.parse_args()

    try:
        from langfuse import Langfuse
        langfuse = Langfuse()
        start = time.perf_counter()
        trace = langfuse.trace(
            name=f"compile:{args.operation}",
            trace_id=args.trace_id,
            input={"operation": args.operation},)
        trace.generation(
            name="claude_code_compile",
            model="claude",
            usage={"input": args.input_tokens, "output": args.output_tokens},
            metadata={"cost_usd": args.cost_usd, "latency_ms":
                      int((time.perf_counter() - start) * 1000)},
        )
        langfuse.flush()
        print(f"[langfuse-probe] 已上报 trace（{args.trace_id}）到 Langfuse")
        return 0
    except Exception as e:
        print(f"[langfuse-probe] 上报失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())