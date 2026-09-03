"""LLM 输出契约校验测试（output_schema.py，纯函数、无 DB/网络）。"""
import pytest

from output_schema import (validate_compile_output, validate_entry_frontmatter,
                           validate_review_output)

# ---- 合法样本（对齐 prompts/*.md 契约与真实页面样例）----

VALID_REVIEW = {
    "verdict": "approved",
    "department": "产品",
    "scores": {
        "completeness": "pass",
        "dedup": "pass",
        "quality": 4,
        "sensitive": "pass",
        "compliance": "pass",
    },
    "duplicates": [],
    "concerns": [],
    "summary": "条目质量合格，建议通过审核",
}


def _valid_compile() -> dict:
    return {
        "resource": {
            "title": "示例监测产品产品白皮书",
            "description": "示例监测产品复合监测系统产品白皮书",
            "tags": ["应急管理", "技术方案"],
            "department": "产品",
            "summary": "## 摘要\n\n示例监测产品是面向多灾种的预警产品。\n\n## 关键信息\n\n- 四种形态\n- 三类模型算法",
            "key_points": ["四种形态", "三类模型"],
            "source_type": "项目",
        },
        "concepts": [
            {
                "title": "示例监测产品",
                "description": "多灾种监测预警产品",
                "tags": ["应急管理"],
                "department": "产品",
                "content": "## 定义\n\n示例监测产品概念定义。\n\n## 背景\n\n背景说明。\n\n## 关键细节\n\n细节。\n\n## 关联知识\n\n- [[概念-叫应体系]]",
                "related_to": ["叫应体系"],
            }
        ],
    }


VALID_FRONTMATTER = {
    "type": "concept",
    "title": "叫应体系",
    "status": "active",
    "source": "RAW/产品资料/样例_示例监测产品白皮书.md",
    "department": "产品",
    "description": "预警联动机制",
    "tags": ["应急管理", "技术方案"],
    "version": "V1.0",
}


# ---- 审核输出 ----

def test_review_valid():
    assert validate_review_output(VALID_REVIEW) == []


def test_review_sensitive_blocked_must_reject():
    d = dict(VALID_REVIEW, verdict="approved")
    d["scores"] = dict(d["scores"], sensitive="blocked")
    errs = validate_review_output(d)
    assert any("sensitive=blocked" in e for e in errs)


def test_review_insufficient_not_approved():
    d = dict(VALID_REVIEW)
    d["scores"] = dict(d["scores"], completeness="insufficient")
    errs = validate_review_output(d)
    assert any("completeness=insufficient" in e for e in errs)


def test_review_duplicate_must_reject():
    d = dict(VALID_REVIEW, verdict="approved")
    d["scores"] = dict(d["scores"], dedup="duplicate")
    errs = validate_review_output(d)
    assert any("dedup=duplicate" in e for e in errs)


def test_review_low_quality_not_approved():
    d = dict(VALID_REVIEW)
    d["scores"] = dict(d["scores"], quality=2)
    errs = validate_review_output(d)
    assert any("quality<=2" in e for e in errs)


def test_review_invalid_verdict_enum():
    d = dict(VALID_REVIEW, verdict="maybe")
    assert any("verdict" in e for e in validate_review_output(d))


def test_review_quality_type_error():
    d = dict(VALID_REVIEW)
    d["scores"] = dict(d["scores"], quality="4")
    assert any("quality" in e for e in validate_review_output(d))


def test_review_missing_scores():
    d = {k: v for k, v in VALID_REVIEW.items() if k != "scores"}
    assert any("scores" in e for e in validate_review_output(d))


def test_review_missing_summary():
    d = {k: v for k, v in VALID_REVIEW.items() if k != "summary"}
    assert any("summary" in e for e in validate_review_output(d))


# ---- 编译输出 ----

def test_compile_valid():
    assert validate_compile_output(_valid_compile()) == []


def test_compile_empty_concepts_allowed():
    d = _valid_compile()
    d["concepts"] = []
    assert validate_compile_output(d) == []


def test_compile_missing_summary_section():
    d = _valid_compile()
    d["resource"]["summary"] = "## 摘要\n\n只有摘要没有关键信息。"
    errs = validate_compile_output(d)
    assert any("## 关键信息" in e for e in errs)


def test_compile_concept_duplicate_title():
    d = _valid_compile()
    d["concepts"].append(dict(d["concepts"][0]))  # 复制→同名
    errs = validate_compile_output(d)
    assert any("标题重复" in e for e in errs)


def test_compile_invalid_department():
    d = _valid_compile()
    d["resource"]["department"] = "市场部"
    assert any("department" in e for e in validate_compile_output(d))


def test_compile_invalid_source_type():
    d = _valid_compile()
    d["resource"]["source_type"] = "产品资料"
    assert any("source_type" in e for e in validate_compile_output(d))


def test_compile_concept_missing_sections():
    d = _valid_compile()
    d["concepts"][0]["content"] = "## 定义\n\n只有定义。"
    errs = validate_compile_output(d)
    assert any("## 背景" in e and "缺少" in e for e in errs)


# ---- 落盘条目 Frontmatter（SCHEMA.md）----

def test_frontmatter_valid():
    assert validate_entry_frontmatter(VALID_FRONTMATTER) == []


def test_frontmatter_bad_version():
    fm = dict(VALID_FRONTMATTER, version="v1")
    assert any("version" in e for e in validate_entry_frontmatter(fm))


def test_frontmatter_bad_status():
    fm = dict(VALID_FRONTMATTER, status="published")
    assert any("status" in e for e in validate_entry_frontmatter(fm))


def test_frontmatter_bad_type():
    fm = dict(VALID_FRONTMATTER, type="blog")
    assert any("type" in e for e in validate_entry_frontmatter(fm))


def test_frontmatter_tag_outside_namespace():
    fm = dict(VALID_FRONTMATTER, tags=["随便写的标签"])
    assert any("tags" in e for e in validate_entry_frontmatter(fm))


def test_frontmatter_missing_required():
    fm = {k: v for k, v in VALID_FRONTMATTER.items() if k != "source"}
    assert any("source" in e for e in validate_entry_frontmatter(fm))


def test_frontmatter_title_too_long():
    fm = dict(VALID_FRONTMATTER, title="这是一个非常长的概念标题已经远远超过了三十个字符的上限约束用于测试长度校验")
    assert any("title" in e for e in validate_entry_frontmatter(fm))