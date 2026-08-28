#!/usr/bin/env python3
"""SP5：_triggers/done/ 归档清理——mtime > 90 天的文件移入 archive_<YYYY-MM>/。

审计留痕保留（不删除），目录不无限膨胀。watcher 启动时顺带执行。
"""
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAYS = 90


def archive(done_dir: Path, days: int = DAYS, now: float | None = None) -> int:
    """归档 done/ 下超过 days 天的文件，返回移动数。"""
    if not done_dir.exists():
        return 0
    now = now if now is not None else time.time()
    cutoff = now - days * 86400
    moved = 0
    for p in sorted(done_dir.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.stat().st_mtime > cutoff:
            continue
        month = time.strftime("%Y-%m", time.localtime(p.stat().st_mtime))
        target = done_dir / f"archive_{month}"
        target.mkdir(exist_ok=True)
        shutil.move(str(p), str(target / p.name))
        moved += 1
    return moved


def main() -> int:
    kb = Path(os.environ.get("KB_ROOT", str(ROOT / "vault")))
    done = kb / "_triggers" / "done"
    n = archive(done)
    print(f"[archive] 归档 {n} 个超期文件 → {done}/archive_*/")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())