"""L3.5 文件分区测试（`core/paths.py`）：单实例服务多租户时文件按租户分区。

契约：
1. **默认租户路径不变**：`default`/未绑定 → `KB_ROOT` 本身（单租户部署与本地开发无感）；
2. 其他租户 → `<KB_ROOT>/tenants/<id>/`；`ensure_tenant_tree` 自愈建目录树；
3. **租户 id 必须过白名单**：`../evil`、`a/b`、空串、超长 → 抛错（否则是目录穿越漏洞）；
4. 编译/审核/ops/上传/检索都走按租户解析——用编译落盘与索引更新做端到端验证。
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("core", "tools", "api"):
    if str(ROOT / _sub) not in sys.path:
        sys.path.insert(0, str(ROOT / _sub))

import db  # noqa: E402
import paths  # noqa: E402
from sales_clarification_runtime import ModelResponse  # noqa: E402

RAW_REL = "RAW/会议/分区测试.md"
RAW_TEXT = "分区测试纪要\n\n会议时间：2026-01-05\n结论：进入联调。\n"


def _compile_output(title: str = "分区测试纪要") -> dict:
    return {
        "resource": {"title": title, "description": "分区测试", "tags": ["会议纪要"],
                     "department": "售前", "source_type": "会议",
                     "summary": "## 摘要\n\n分区测试用。\n\n## 关键信息\n\n- 一点", "key_points": ["一点"]},
        "concepts": [],
    }


class _Port:
    def complete(self, *, system_prompt, user_prompt, max_tokens=4000, **kwargs):
        return ModelResponse(json.dumps(_compile_output(), ensure_ascii=False), "fake", 5, 5, "r")


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
    return tmp_path / "vault"


# ---------- 1/2. 路径解析 ----------

def test_default_tenant_keeps_legacy_root(kb):
    """默认租户/未绑定 → KB_ROOT 本身（旧路径不变）。"""
    assert paths.kb_root() == kb
    assert paths.kb_root("default") == kb


def test_other_tenant_gets_subtree(kb):
    """其他租户 → <KB_ROOT>/tenants/<id>/，且目录树可自愈。"""
    root = paths.kb_root("tenant-a")
    assert root == kb / "tenants" / "tenant-a"
    ensured = paths.ensure_tenant_tree("tenant-a")
    assert ensured == root
    for rel in ("RAW", "pending_review", "NEXUS/资源", "NEXUS/概念", "_triggers/done"):
        assert (root / rel).is_dir()


def test_kb_root_follows_bound_tenant(kb):
    """未显式传参时跟随当前租户上下文（HTTP 中间件 / worker 绑定）。"""
    token = db.bind_tenant("tenant-b")
    try:
        assert paths.kb_root() == kb / "tenants" / "tenant-b"
    finally:
        db.reset_tenant(token)
    assert paths.kb_root() == kb


# ---------- 3. 安全：租户 id 白名单 ----------

@pytest.mark.parametrize("bad", ["../evil", "a/b", "..", "tenant a", "t" * 65, "租户", "../.."])
def test_unsafe_tenant_id_is_rejected(bad):
    """租户 id 会被拼进文件路径，必须白名单校验——否则是目录穿越漏洞。"""
    with pytest.raises(ValueError):
        paths.safe_tenant_id(bad)
    with pytest.raises(ValueError):
        paths.kb_root(bad)


def test_empty_tenant_means_default(kb):
    """空串/None = 默认租户（与 db.bind_tenant 同口径），**不是**穿越也不是报错。"""
    assert paths.kb_root("") == kb
    assert paths.kb_root(None) == kb
    with pytest.raises(ValueError):
        paths.safe_tenant_id("")            # 白名单本身仍要求 1-64 字符


# ---------- 4. 端到端：编译产物落进租户子树 ----------

def test_compile_writes_into_tenant_subtree(kb):
    """同一 KB_ROOT 下，两个租户各自编译**同名同内容**文件互不干扰（文件系统 + 落库双隔离）。

    这条用例抓到过一个真 bug：编译去重只按 (raw_path, fingerprint)，B 租户的同名文件
    命中了 A 租户的 done 记录，被误判 `cached` 而**永不编译**（本地超级用户连接下 RLS
    不生效，所以必须靠显式 tenant_id 过滤）。
    """
    import compile_service

    for tenant in ("tenant-a", "tenant-b"):
        root = paths.ensure_tenant_tree(tenant)
        (root / "NEXUS/index.md").write_text("# 知识库索引\n\n## 资源\n\n## 概念\n", encoding="utf-8")
        (root / RAW_REL).parent.mkdir(parents=True, exist_ok=True)
        (root / RAW_REL).write_text(RAW_TEXT, encoding="utf-8")

    for tenant in ("tenant-a", "tenant-b"):
        token = db.bind_tenant(tenant)
        try:
            result = compile_service.compile_batch([RAW_REL], port=_Port())
        finally:
            db.reset_tenant(token)
        assert result["compiled"] == 1, f"{tenant} 应各自编译一次，实际 {result}"
        produced = Path(kb) / "tenants" / tenant / result["files"][0]
        assert produced.exists(), f"{tenant} 的产物应落在自己的子树里"
        # 索引也更新到本租户子树
        index = (kb / "tenants" / tenant / "NEXUS/index.md").read_text(encoding="utf-8")
        assert "资源 1 篇" in index

    # 默认租户根下不应出现任何**产物文件**（目录树由 ensure_schema 建，属正常）
    assert not list((kb / "NEXUS/资源").glob("*.md"))
    assert not list((kb / "pending_review").glob("*.md"))

    # 落库身份 = (tenant_id, path)：同名条目在库中各存一行、各归其主
    # （PK 还是单列 path 时，B 租户的写入会撞 A 租户的行——这条断言就是那个 bug 的锁）
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT tenant_id, count(*) FROM knowledge_entries "
            "WHERE path LIKE 'NEXUS/资源/%' GROUP BY tenant_id ORDER BY tenant_id").fetchall()
    assert rows == [("tenant-a", 1), ("tenant-b", 1)], f"同名条目应各租户一行，实际 {rows}"


def test_ops_uses_tenant_root(kb):
    """ops 的触发文件写入也按租户分区（审批/触发链路同样隔离）。"""
    import ops

    token = db.bind_tenant("tenant-a")
    try:
        paths.ensure_tenant_tree("tenant-a")
        paper = ops.write_trigger("compile", [RAW_REL], "test")
        assert Path(paper).parent == kb / "tenants" / "tenant-a" / "_triggers"
    finally:
        db.reset_tenant(token)


def test_upload_router_kb_root_is_tenant_scoped(kb):
    """上传接口的落盘根按租户解析（容器内 KB_ROOT 不变，租户子树自动分区）。"""
    from api.routers import upload_router

    assert upload_router._kb_root() == str(kb)
    token = db.bind_tenant("tenant-a")
    try:
        assert upload_router._kb_root() == str(kb / "tenants" / "tenant-a")
    finally:
        db.reset_tenant(token)
