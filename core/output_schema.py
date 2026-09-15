"""LLM 输出契约校验：把 prompt 里的输出契约代码化（确定性逻辑，不依赖 LLM）。

四组校验对应四个产物入口（接入点与契约来源见 docs/VAL-01_LLM_输出校验_设计说明.md）：
- validate_review_output    : 审核 Agent 六维度 JSON（契约：prompts/review_prompt.md 输出格式 + 判定逻辑）
- validate_compile_output   : 编译 Agent JSON（契约：prompts/compile_prompt.md 输出格式 + 编译规则）
- validate_answer_output    : 问答 Agent JSON（契约：prompts/answer_prompt.md §输出契约；引用必须可溯源）
- validate_entry_frontmatter: 落盘条目 YAML Frontmatter（约束：vault/SCHEMA.md）

约定：全部返回 list[str] 错误明细；空列表 = 合法。失败方（引擎/API/工具）自行决定
重试、标记 failed 或人工介入——本模块只做"输出可不可接受"的判定，不碰业务。
"""
from __future__ import annotations

import re

# ---- 枚举常量（与 prompts/SCHEMA.md 对齐）----
DEPARTMENTS = {"销售", "售前", "产品", "实施交付", "开发", "财务", "人事", "行政", "共享层"}
# 来源类型：原先只有 个人_notes/会议/经验/项目（Demo 期口径）；接入真实企业语料后补齐
# 政策/行业研究/网络文摘/论文/客户沟通——否则"国家政策、市场答疑、论文"这类文档会被契约
# 反复拒绝（实测：政策类文档模型坚持输出"政策规划"，两次重试都不通过）。
SOURCE_TYPES = {"个人_notes", "会议", "经验", "项目",
                "政策", "行业研究", "网络文摘", "论文", "客户沟通"}
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
    if scores.get("sensitive") == "warning" and verdict == "approved":
        errs.append("判定一致性：scores.sensitive=warning 时 verdict 不应为 approved（需人工复核）")
    if scores.get("compliance") == "flagged" and verdict == "approved":
        errs.append("判定一致性：scores.compliance=flagged 时 verdict 不应为 approved（需人工复核）")
    concerns = d.get("concerns")
    if isinstance(concerns, list) and len(concerns) >= 3 and verdict == "approved":
        errs.append("判定一致性：concerns 数量>=3 时 verdict 不应为 approved（需人工复核）")
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


# ---- 问答 Agent 输出（answer_prompt.md §输出契约）----
# 契约要点：回答**必须基于检索结果**，因此除了形状校验，还要校验"引用可溯源"：
#   · 每条引用必须指向本次检索到的条目路径（防编造来源）；
#   · 引用的 quote 必须能在该条目正文里逐字找到（防编造原文）。
# 这两条是确定性的（路径集合、子串匹配），必须由代码判——模型自报"有引用"不算数。
def validate_answer_output(d: dict, retrieved_paths: set[str] | None = None,
                           bodies: dict[str, str] | None = None) -> list[str]:
    """校验问答输出；`retrieved_paths`/`bodies` 给定时同时校验引用可溯源。"""
    errs: list[str] = []
    if not isinstance(d, dict):
        return ["问答输出应为 JSON 对象"]

    answer = d.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        errs.append("字段 answer 缺失或为空字符串")

    insufficient = d.get("insufficient")
    if not isinstance(insufficient, bool):
        errs.append(f"字段 insufficient 应为布尔值，实际：{insufficient!r}")

    citations = d.get("citations")
    if not isinstance(citations, list):
        errs.append("字段 citations 应为数组（无来源时给 []）")
        citations = []
    for i, c in enumerate(citations):
        if not isinstance(c, dict):
            errs.append(f"citations[{i}] 非对象")
            continue
        path = c.get("path")
        if not isinstance(path, str) or not path.strip():
            errs.append(f"citations[{i}].path 缺失或非字符串")
            continue
        if retrieved_paths is not None and path not in retrieved_paths:
            errs.append(f"citations[{i}].path 不在本次检索结果中：{path!r}（禁止编造来源）")
            continue
        quote = c.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            errs.append(f"citations[{i}].quote 缺失或非字符串（引用必须带原文片段）")
            continue
        if bodies and path in bodies:
            body = bodies[path]
            if quote.strip() not in body:
                errs.append(f"citations[{i}].quote 在 {path} 正文中找不到（禁止编造原文）")

    _check_str_list(d.get("followups", []), "followups", errs)

    # 判定一致性：没检索到依据就必须承认 insufficient，不能"无来源却给确定答案"
    if not insufficient and retrieved_paths is not None and not retrieved_paths:
        errs.append("判定一致性：无任何检索结果时 insufficient 应为 true")
    if not insufficient and retrieved_paths and not citations:
        errs.append("判定一致性：insufficient=false 时至少要给出一条可溯源引用")
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
