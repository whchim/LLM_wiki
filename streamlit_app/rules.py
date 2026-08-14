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
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "blocked", "身份证号"),
    (re.compile(r"(?<!\d)1\d{10}(?!\d)"), "warning", "手机号"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "warning", "邮箱"),
    (re.compile(r"(?<![A-Za-z0-9_])(?:sk-[A-Za-z0-9_-]{8,}|api[_-]?key\s*[:=：＝]\s*\S+|token\s*[:=：＝]\s*\S+|secret\s*[:=：＝]\s*\S+)", re.IGNORECASE), "blocked", "密钥"),
    (re.compile(r"(?:password|密码)\s*[:=：＝]\s*\S+", re.IGNORECASE), "blocked", "明文密码"),
    (re.compile(r"(?<![\d.])(?:\d{7,}|\d{1,3}(?:,\d{3}){2,})\s*元|(?<![\d.])[1-9]\d{2,}(?:\.\d+)?万"), "warning", "大额金额"),
    (re.compile(r"(机密|绝密|confidential|内部(?:资料|文件|机密|专用|使用|标记|水印))", re.IGNORECASE), "blocked", "内部标记"),
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
