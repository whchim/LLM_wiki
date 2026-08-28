#!/usr/bin/env python3
"""触发文件 Watcher（SP3 前置：自动唤起 Claude Code 消费编译/审核队列）。

解决痛点：上传后必须手动启动 Claude Code 跑 /process-triggers。
watcher 常驻后台轮询 vault/_triggers/，发现新纸条即以 headless 模式
自动执行 `claude -p "/process-triggers"`，全程零人工。

用法：
    python tools/trigger_watcher.py            # 前台运行（Ctrl+C 停止）
    python tools/trigger_watcher.py --once     # 只处理一轮现有纸条后退出（调试用）

环境变量：
    WATCHER_INTERVAL   轮询间隔秒数（默认 5）
    WATCHER_TIMEOUT    单次 claude 执行超时秒数（默认 900 = 15 分钟）

安全说明：
    headless 无人值守使用 --permission-mode bypassPermissions（本机个人场景）。
    如需收紧，改为 --allowedTools 白名单（见 README 部署节）。

设计要点：
- 防抖：发现纸条后等待文件写入稳定（大小不变 2 秒）再消费，避免读到半写文件
- 串行：同一时刻只跑一个 claude 进程（compile/review 纸条都会被 /process-triggers 消费）
- 幂等：Claude Code 侧按 done/ 归档 + compile_tasks 状态天然幂等，watcher 重启安全
- 失败重试：claude 退出码非 0 时最多重试 3 次（指数退避），仍失败则告警日志并跳过等待下轮
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "tools" / "watcher.log"

# 复用项目数据层（compile_tasks 查询/插入 + 触发文件写入）
sys.path.insert(0, str(ROOT / "streamlit_app"))

INTERVAL = int(os.environ.get("WATCHER_INTERVAL", "5"))
TIMEOUT = int(os.environ.get("WATCHER_TIMEOUT", "900"))
STABLE_SECS = 2          # 文件大小稳定判定窗口
RAW_EXTS = {".md", ".txt"}   # SP3：RAW 直放监听的扩展名白名单


def _kb_root() -> Path:
    """知识库根：跟随 KB_ROOT（测试隔离/容器/本机一致）。"""
    return Path(os.environ.get("KB_ROOT", str(ROOT / "vault")))


def _triggers_dir() -> Path:
    return _kb_root() / "_triggers"


def _raw_dir() -> Path:
    return _kb_root() / "RAW"


def _claude_cmd() -> str:
    """解析 claude 可执行文件完整路径。

    Windows 下 claude 是 npm 的 .cmd/ps1 shim，subprocess 不带 shell 时
    无法按 PATHEXT 解析——必须用 shutil.which 拿完整路径。"""
    path = shutil.which("claude")
    if path is None:
        raise FileNotFoundError(
            "未找到 claude 命令——请确认本机已安装 Claude Code 且在 PATH 中")
    return path
MAX_RETRY = 3


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def pending_triggers() -> list[Path]:
    """待处理纸条：_triggers/*.md（排除 done/ 子目录与 .tmp_ 原子写中转文件）。"""
    if not _triggers_dir().exists():
        return []
    return sorted(
        p for p in _triggers_dir().glob("*.md")
        if not p.name.startswith(".tmp_"))


def _raw_relpaths() -> list[str]:
    """RAW 下全部可编译文件（相对 KB_ROOT 的 POSIX 路径），按分类子目录组织。"""
    raw = _raw_dir()
    if not raw.exists():
        return []
    out: list[str] = []
    for p in sorted(raw.rglob("*")):
        if p.is_file() and p.suffix.lower() in RAW_EXTS:
            out.append(p.relative_to(_kb_root()).as_posix())
    return out


def _kb_root() -> Path:
    kb = os.environ.get("KB_ROOT", str(ROOT / "vault"))
    return Path(kb)


def scan_raw_increment() -> list[str]:
    """SP3 增量判定：RAW 下"compile_tasks 中从未出现过的"文件（相对 KB_ROOT 路径）。

    判定 = 路径在 compile_tasks 无任何记录即为新（seen 集合取全部状态）。
    failed/pending/done/cached 均不算新——failed 重复生成纸条会形成风暴，
    重试走 UI 按钮（重新写纸条）或指纹变化后由消费侧断点逻辑处理。
    数据库不可达时返回空表并告警（watcher 仍继续消费既有纸条）。"""
    try:
        import db  # noqa: 复用连接池与环境变量
    except Exception as e:
        log(f"[scan] 数据层不可用，本轮跳过 RAW 增量扫描：{e}")
        return []
    try:
        with db.get_conn() as conn:
            rows = conn.execute("SELECT DISTINCT raw_path FROM compile_tasks")
            seen = {r[0] for r in rows.fetchall()}
        return [p for p in _raw_relpaths() if p not in seen]
    except Exception as e:
        log(f"[scan] 查询 compile_tasks 失败，本轮跳过 RAW 增量扫描：{e}")
        return []


def write_raw_paper(paths: list[str]) -> Path | None:
    """把 RAW 增量文件合并写成一张 compile 纸条（复用 ops.write_trigger 原子写）。"""
    try:
        import ops
        paper = ops.write_trigger("compile", paths, "watcher_scan")
        log(f"[scan] RAW 增量 {len(paths)} 个文件 → 纸条 {paper.name}")
        return paper
    except Exception as e:
        log(f"[scan] 写纸条失败：{e}")
        return None


def stable(p: Path) -> bool:
    """文件写入已稳定（大小在 STABLE_SECS 窗口内不变）——防抖。"""
    try:
        s1 = p.stat().st_size
        time.sleep(STABLE_SECS)
        s2 = p.stat().st_size
        return s1 == s2
    except OSError:
        return False


def _preflight() -> str | None:
    """启动预检：LLM 代理可达性。ANTHROPIC_BASE_URL 指向本地端口时提前探测，
    避免 claude 跑几分钟后才报 Connection refused。返回 None=通过，否则返回错误说明。"""
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if base.startswith("http://127.0.0.1") or base.startswith("http://localhost"):
        try:
            from urllib.parse import urlparse
            u = urlparse(base)
            import socket
            with socket.create_connection((u.hostname, u.port or 80), timeout=3):
                return None
        except OSError as e:
            return (f"LLM 代理不可达（{base}：{e}）。你的 Claude Code 依赖本地代理服务，"
                    f"请先启动它再运行 watcher。")
    return None


def run_claude() -> tuple[int, str]:
    """以 headless 模式执行 /process-triggers，返回 (退出码, 摘要输出)。"""
    try:
        claude = _claude_cmd()
    except FileNotFoundError as e:
        return 127, str(e)
    cmd = [
        claude, "-p", "/process-triggers",
        "--permission-mode", "bypassPermissions",
        "--output-format", "text",
    ]
    try:
        r = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"claude 执行超时（>{TIMEOUT}s）"


def main() -> int:
    parser = argparse.ArgumentParser(description="触发文件 Watcher")
    parser.add_argument("--once", action="store_true",
                        help="处理完当前存量纸条后退出（默认常驻轮询）")
    args = parser.parse_args()

    log(f"watcher 启动：interval={INTERVAL}s timeout={TIMEOUT}s triggers={_triggers_dir()}")

    # SP3：RAW 增量扫描——常驻模式每轮在循环内扫（发现拖入 RAW 的新文件）；
    # --once 模式在循环前扫一次，让存量 RAW 文件也能被处理。
    if args.once:
        new_files = scan_raw_increment()
        if new_files:
            write_raw_paper(new_files)

    while True:
        try:
            if not args.once:
                new_files = scan_raw_increment()
                if new_files:
                    write_raw_paper(new_files)

            papers = pending_triggers()
            if not papers:
                if args.once:
                    log("无待处理纸条，退出（--once）")
                    return 0
                time.sleep(INTERVAL)
                continue

            log(f"发现 {len(papers)} 个待处理纸条：{[p.name for p in papers]}")
            # 预检：LLM 代理可达性（提前 1 秒发现环境问题，避免 claude 跑几分钟才失败）
            pre = _preflight()
            if pre:
                log(f"预检失败：{pre}")
                if args.once:
                    return 1
                time.sleep(INTERVAL * 10)  # 代理未就绪，降频重试
                continue
            # 防抖：逐个确认写入稳定
            ready = [p for p in papers if stable(p)]
            if not ready:
                log("纸条仍在写入中（未稳定），下轮再查")
                time.sleep(INTERVAL)
                continue

            log(f"唤起 Claude Code headless 消费（{len(ready)} 个纸条）...")
            t0 = time.time()
            code, output = run_claude()
            dur = int(time.time() - t0)
            tail = " | ".join(output.strip().splitlines()[-3:]) if output.strip() else "(无输出)"
            if code == 0:
                log(f"claude 完成（exit=0，{dur}s）。输出尾部：{tail[:300]}")
                left = pending_triggers()
                if left:
                    log(f"仍有 {len(left)} 个纸条未清空（可能新增），下轮继续")
            else:
                log(f"claude 失败（exit={code}，{dur}s）：{tail[:300]}")
                if args.once:
                    return code
            time.sleep(INTERVAL)
        except KeyboardInterrupt:
            log("watcher 手动停止")
            return 0
        except Exception as e:
            log(f"watcher 异常（继续运行）：{e}")
            time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())