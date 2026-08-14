"""审核确定性规则（不依赖 LLM）。维度1 完整性 + 维度5 敏感信息。"""
import re

REQUIRED_FIELDS = ["type", "title", "status", "source"]
MIN_BODY_CHARS = 100


def check_completeness(frontmatter_text: str, body_text: str) -> str:
    """返回 pass/incomplete/insufficient。"""
    missing = [f for f in REQUIRED_FIELDS if not re.search(rf"^{f}: .+$", frontmatter_text, re.MULTILINE)]
    if len(missing) >= 3 or len(body_text) < MIN_BODY_CHARS:
        return "insufficient"
    if missing:
        return "incomplete"
    return "pass"


_SENSITIVE_RULES = [
    (re.compile(r"\b\d{17}[\dXx]\b"), "blocked", "身份证号"),
    (re.compile(r"\b1\d{10}\b"), "warning", "手机号"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "warning", "邮箱"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|api[_-]?key\s*[:=]\s*\S+|token\s*[:=]\s*\S+|secret\s*[:=]\s*\S+)", re.IGNORECASE), "blocked", "密钥"),
    (re.compile(r"(?:password|密码)\s*[:=]\s*\S+", re.IGNORECASE), "blocked", "明文密码"),
    (re.compile(r"(?:\d[\d,]{6,}|\d+(?:\.\d+)?\s*万)\s*元"), "warning", "大额金额"),
    (re.compile(r"(机密|绝密|内部|confidential)", re.IGNORECASE), "blocked", "内部标记"),
]


def check_sensitive(text: str) -> str:
    """返回 pass/warning/blocked（blocked 优先级最高）。"""
    has_warning = False
    for pattern, level, _name in _SENSITIVE_RULES:
        if pattern.search(text):
            if level == "blocked":
                return "blocked"
            has_warning = True
    return "warning" if has_warning else "pass"
