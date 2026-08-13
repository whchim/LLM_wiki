"""纯操作逻辑：触发文件、上传校验、审核动作。UI 只调用这些函数，不做业务。"""
import os
import re
import hashlib
from datetime import datetime
from pathlib import Path

from db import (get_conn, update_status, move_entry, insert_search_log,
                set_human_decision, resubmit_review)

KB_ROOT = os.environ.get("KB_ROOT", os.path.join(os.path.dirname(__file__), "..", "vault"))
ALLOWED_EXTS = {".md", ".txt", ".pdf", ".docx"}
MAX_SIZE = 10 * 1024 * 1024  # 10MB


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_upload(filename: str, size: int) -> str | None:
    """返回 None=合法；否则返回错误文案（PRD 9.3 文案）。"""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return f"不支持的文件格式：{ext}。支持的格式：.md, .txt, .pdf, .docx"
    if size > MAX_SIZE:
        return f"文件大小超过限制（10MB）。当前文件大小：{size / 1024 / 1024:.1f}MB"
    return None


def write_trigger(kind: str, paths: list[str], source: str) -> Path:
    """原子写触发文件（.tmp + mv），返回最终路径。kind: compile|review"""
    trig_dir = Path(KB_ROOT) / "_triggers"
    trig_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    final = trig_dir / f"{kind}_{ts}.md"
    tmp = trig_dir / f".tmp_{final.name}"
    body = f"""---
type: trigger
kind: {kind}
created: "{datetime.now().isoformat(timespec='seconds')}"
source: {source}
---
""" + "\n".join(f"- {p}" for p in paths) + "\n"
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(final)  # 原子重命名
    return final


def _yaml_edit(path: Path, field: str, value: str) -> None:
    """就地修改 Markdown 文件的 YAML Frontmatter 中某字段。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(rf"^{field}: .*$", f"{field}: {value}", text, count=1, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")


def approve_entry(review_id: int, old_path: str, new_path: str) -> None:
    """通过：移动文件 + YAML status=active + db 双写 + 追加 index.md。"""
    src = Path(KB_ROOT) / old_path
    dst = Path(KB_ROOT) / new_path
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():  # 同名冲突：追加 -2 后缀
        stem = dst.stem
        dst = dst.with_name(f"{stem}-2.md")
    src.replace(dst)
    _yaml_edit(dst, "status", "active")
    move_entry(old_path, dst.relative_to(Path(KB_ROOT)).as_posix(), "active")
    set_human_decision(review_id, "approved")
    _append_index(f"[[概念-{dst.stem}]] → {dst.relative_to(Path(KB_ROOT)).as_posix()}")


def reject_entry(review_id: int, path: str, reason: str) -> None:
    """驳回：YAML status=draft + db 双写。"""
    p = Path(KB_ROOT) / path
    _yaml_edit(p, "status", "draft")
    update_status(path, "draft")
    set_human_decision(review_id, "rejected", reason)


def resubmit(review_id: int, path: str) -> None:
    """重新提交：YAML status=pending + db 双写。"""
    p = Path(KB_ROOT) / path
    _yaml_edit(p, "status", "pending")
    update_status(path, "pending")
    resubmit_review(review_id)


def _append_index(line: str) -> None:
    idx = Path(KB_ROOT) / "NEXUS" / "index.md"
    if not idx.exists():
        return
    text = idx.read_text(encoding="utf-8")
    if line in text:  # 幂等
        return
    # 追加到「## 概念」节末尾
    if "## 概念" in text:
        text = re.sub(r"(## 概念\n)", r"\1- " + line + "\n", text, count=1)
    else:
        text += f"\n## 概念\n- {line}\n"
    idx.write_text(text, encoding="utf-8")
