"""LLM 输出契约校验：把 prompt 里的输出契约代码化（确定性逻辑，不依赖 LLM）。

三组校验对应三个产物入口（接入点与契约来源见 docs/LLM_输出校验_设计说明.md）：
- validate_review_output    : 审核 Agent 六维度 JSON（契约：prompts/review_prompt.md 输出格式 + 判定逻辑）
- validate_compile_output   : 编译 Agent JSON（契约：prompts/compile_prompt.md 输出格式 + 编译规则）
- validate_entry_frontmatter: 落盘条目 YAML Frontmatter（约束：vault/SCHEMA.md）

约定：全部返回 list[str] 错误明细；空列表 = 合法。失败方（引擎/API/工具）自行决定
重试、标记 failed 或人工介入——本模块只做"输出可不可接受"的判定，不碰业务。
"""
from __future__ import annotations

import re

# ---- 枚举常量（与 prompts/SCHEMA.md 对齐）----
DEPARTMENTS = {"销售", "售前", "产品", "实施交付", "开发", "财务", "人事", "行政", "共享层"}
SOURCE_TYPES = {"个人_notes", "会议", "经验", "项目"}
ENTRY_TYPES = {"concept", "resource", "research", "glossary"}
ENTRY_STATUSES = {"draft", "pending", "active", "stale", "deprecated"}
# SCHEMA.md 三个标签命名空间（部门 9 + 领域 12 + 类型 8）
TAG_NAMESPACE = DEPARTMENTS | {
    "AI", "大数据", "云计算", "安全", "项目管理", "产品设计", "应急管理", "智慧城市", "物联网", "数字孪生",
    "实战经验", "技术方案", "产品文档", "会议纪要", "复盘总结", "行业研究", "标准规范", "培训材料",
}
COMPILE_SUMMARY_SECTIONS = ("## 摘要", "## 关键信息")
CONCEPT_CONTENT_SECTIONS = ("## 定义", "## 背景", "## 关键细节", "## 关联知识")

VERDICTS = {"approved", "rejected", "needs_human_review"}
COMPLETENESS = {"pass", "incomplete", "insufficient"}
DEDUP = {"pass", "duplicate", "similar"}
SENSITIVE = {"pass", "warning", "blocked"}
COMPLIANCE = {"pass", "flagged"}


def _check_str(d: dict, field: str, errs: list[str], required: bool = True) -> None:
    v = d.get(field)
    if not isinstance(v, str) or not v.strip():
        if required:
            errs.append(f"字段 {field} 缺失或非非空字符串")
    elif field == "title" and len(v) > 30:
        errs.append(f"字段 title 超长（>30 字：{len(v)}）")


def _check_enum(v, allowed: set, label: str, errs: list[str]) -> None:
    if v not in allowed:
        errs.append(f"{label} 非法值：{v!r}（应为 {'/'.join(sorted(allowed))}）")


def _check_str_list(v, field: str, errs: list[str], allow_empty: bool = True) -> None:
    if not isinstance(v, list):
        errs.append(f"字段 {field} 应为字符串数组")
        return
    for i, item in enumerate(v):
        if not isinstance(item, str):
            errs.append(f"字段 {field}[{i}] 非字符串：{item!r}")


# ---- 审核 Agent 输出（review_prompt.md）----
def validate_review_output(d: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(d, dict):
        return ["审核输出应为 JSON 对象"]

    _check_enum(d.get("verdict"), VERDICTS, "verdict", errs)
    _check_enum(d.get("department"), DEPARTMENTS, "department", errs)

    scores = d.get("scores")
    if not isinstance(scores, dict):
        errs.append("scores 缺失或非对象")
        return errs
    _check_enum(scores.get("completeness"), COMPLETENESS, "scores.completeness", errs)
    _check_enum(scores.get("dedup"), DEDUP, "scores.dedup", errs)
    _check_enum(scores.get("sensitive"), SENSITIVE, "scores.sensitive", errs)
    _check_enum(scores.get("compliance"), COMPLIANCE, "scores.compliance", errs)
    q = scores.get("quality")
    if not isinstance(q, int) or isinstance(q, bool) or not 1 <= q <= 5:
        errs.append(f"scores.quality 应为 1-5 整数，实际：{q!r}")

    _check_str_list(d.get("duplicates"), "duplicates", errs)
    _check_str_list(d.get("concerns"), "concerns", errs)
    _check_str(d, "summary", errs)

    # ---- 判定逻辑一致性（与 review_prompt.md 判定逻辑对应）----
    verdict = d.get("verdict")
    if scores.get("sensitive") == "blocked" and verdict != "rejected":
        errs.append("判定一致性：scores.sensitive=blocked（一票否决）时 verdict 应为 rejected")
    if scores.get("completeness") == "insufficient" and verdict == "approved":
        errs.append("判定一致性：scores.completeness=insufficient 时 verdict 不应为 approved")
    if scores.get("dedup") == "duplicate" and verdict != "rejected":
        errs.append("判定一致性：scores.dedup=duplicate 时 verdict 应为 rejected")
    if (scores.get("quality") is not None and isinstance(scores["quality"], int)
            and not isinstance(scores["quality"], bool) and scores["quality"] <= 2
            and verdict == "approved"):
        errs.append("判定一致性：scores.quality<=2 时 verdict 不应为 approved（应为 needs_human_review）")
    return errs


# ---- 编译 Agent 输出（compile_prompt.md）----
def validate_compile_output(d: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(d, dict):
        return ["编译输出应为 JSON 对象"]

    resource = d.get("resource")
    if not isinstance(resource, dict):
        errs.append("resource 缺失或非对象")
    else:
        _check_str(resource, "title", errs)
        _check_str(resource, "description", errs)
        _check_enum(resource.get("department"), DEPARTMENTS, "resource.department", errs)
        _check_enum(resource.get("source_type"), SOURCE_TYPES, "resource.source_type", errs)
        summary = resource.get("summary")
        if not isinstance(summary, str):
            errs.append("resource.summary 缺失或非字符串")
        else:
            for sec in COMPILE_SUMMARY_SECTIONS:
                if sec not in summary:
                    errs.append(f"resource.summary 缺少必需章节：{sec}")
        _check_str_list(resource.get("tags"), "resource.tags", errs)
        _check_str_list(resource.get("key_points"), "resource.key_points", errs)

    concepts = d.get("concepts")
    if not isinstance(concepts, list):
        errs.append("concepts 缺失或非数组（内容过少时可为空数组 []）")
        return errs
    titles: set[str] = set()
    for i, c in enumerate(concepts):
        if not isinstance(c, dict):
            errs.append(f"concepts[{i}] 非对象")
            continue
        title = c.get("title")
        _check_str(c, "title", errs)
        if isinstance(title, str) and title.strip():
            if title in titles:
                errs.append(f"concepts 标题重复：{title}（契约要求全文档唯一）")
            titles.add(title)
        _check_str(c, "description", errs, required=False)
        _check_enum(c.get("department"), DEPARTMENTS, f"concepts[{i}].department", errs)
        content = c.get("content")
        if not isinstance(content, str):
            errs.append(f"concepts[{i}].content 缺失或非字符串")
        else:
            for sec in CONCEPT_CONTENT_SECTIONS:
                if sec not in content:
                    errs.append(f"concepts[{i}].content 缺少必需章节：{sec}")
        _check_str_list(c.get("related_to"), f"concepts[{i}].related_to", errs)
    return errs


# ---- 落盘条目 YAML Frontmatter（SCHEMA.md）----
def validate_entry_frontmatter(fm: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(fm, dict):
        return ["Frontmatter 解析结果应为对象"]

    _check_enum(fm.get("type"), ENTRY_TYPES, "type", errs)
    _check_enum(fm.get("status"), ENTRY_STATUSES, "status", errs)
    _check_str(fm, "title", errs)
    _check_str(fm, "source", errs)
    if "version" in fm:
        v = fm["version"]
        if not isinstance(v, str) or not re.fullmatch(r"V\d+\.\d+", v):
            errs.append(f"version 格式应为 V{{major}}.{{minor}}（SCHEMA.md），实际：{v!r}")
    if "department" in fm:
        _check_enum(fm.get("department"), DEPARTMENTS, "department", errs)
    tags = fm.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            errs.append("tags 应为字符串数组")
        else:
            for t in tags:
                if t not in TAG_NAMESPACE:
                    errs.append(f"tags 含未预定义命名空间的标签：{t!r}（SCHEMA.md 三类命名空间）")
    return errs