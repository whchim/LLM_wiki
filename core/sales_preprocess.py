"""销售文字记录的确定性预处理与安全门禁。

本模块不调用 LLM，也不写数据库。它负责把输入变成 Agent 可以消费的最小
脱敏上下文；敏感数值必须交给调用方提供的受控加密器，模块本身不会返回明文。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

import rules

MAX_CONTENT_CHARS = 12_000
MIN_CONTENT_CHARS = 10
MAX_IDEMPOTENCY_KEY_CHARS = 200
MAX_CUSTOMER_ID_CHARS = 128
MAX_SUBMITTED_BY_CHARS = 128
FUTURE_SKEW = timedelta(minutes=15)

SOURCE_TYPES = {"meeting_note", "transcript", "chat_summary"}
CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

# 这些模式只做明显的指令混入拦截，不试图判断普通业务文本是否“可信”。
INJECTION_PATTERNS = (
    re.compile(r"(?:忽略|无视|跳过).{0,20}(?:之前|上面|系统).{0,20}(?:指令|规则|提示)", re.I),
    re.compile(r"\b(?:ignore|disregard|override)\b.{0,40}\b(?:system|developer|previous)\b", re.I),
    re.compile(r"(?:调用工具|执行命令|运行脚本|读取密钥|删除文件|发送邮件)", re.I),
    re.compile(r"(?:<\s*(?:system|developer|tool)\s*>|\b(?:bash|powershell|shell)\s*:\s*)", re.I),
)

# 只提取有业务上下文的数字，避免把日期、年份和普通序号误当成敏感值。
NUMERIC_PATTERN = re.compile(
    r"(?P<amount>(?:金额|预算|报价|价格|费用|合同价|总价|成本)\s*(?:为|是|约|大约)?\s*"
    r"(?:人民币|￥|¥)?\d[\d,]*(?:\.\d+)?\s*(?:万元?|万|元|块)?)"
    r"|(?P<discount>(?:折扣|折让|优惠)\s*(?:为|到|至)?\s*\d+(?:\.\d+)?\s*(?:%|折)?)"
    r"|(?P<quantity>(?:数量|数目|台数|席位|用户数|并发数)\s*(?:为|是|约)?\s*\d[\d,]*\s*(?:个|台|套|席位|人)?)",
    re.I,
)

EncryptNumeric = Callable[[str, str, str], str]


def _parse_occurred_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurred_at 必须是合法 ISO-8601 时间") from exc
    else:
        raise ValueError("occurred_at 必填")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("occurred_at 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _comparison_bucket(field_type: str, text: str) -> str:
    """只输出粗粒度区间；精确值不进入结果对象、日志或 Agent 上下文。"""
    number = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    value = float(number.group(0).replace(",", "")) if number else 0
    if field_type == "discount":
        return "low" if value < 5 else "medium" if value <= 15 else "high"
    if field_type == "quantity":
        return "small" if value < 10 else "medium" if value <= 100 else "large"
    # 这里不把“万”换算成精确金额，只使用业务可用的粗区间。
    if "万" in text:
        value *= 10_000
    return "lt_1w" if value < 10_000 else "1w_10w" if value < 100_000 else "10w_100w" if value < 1_000_000 else "gte_100w"


def _redact_numeric(content: str, idempotency_key: str,
                    encrypt_numeric: EncryptNumeric | None) -> tuple[str, list[dict], list[str]]:
    refs: list[dict] = []
    protected: list[str] = []
    ordinal = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal ordinal
        field_type = next(name for name in ("amount", "discount", "quantity") if match.group(name))
        original = match.group(0)
        ordinal += 1
        digest = hashlib.sha256(f"{idempotency_key}:{field_type}:{ordinal}:{original}".encode()).hexdigest()[:16]
        ref_id = f"nv-{digest}"
        bucket = _comparison_bucket(field_type, original)
        refs.append({"ref_id": ref_id, "field_type": field_type, "comparison_bucket": bucket})
        if encrypt_numeric is None:
            protected.append(ref_id)
        else:
            ciphertext = encrypt_numeric(ref_id, field_type, original)
            if not isinstance(ciphertext, str) or not ciphertext:
                raise ValueError("受控加密器必须返回非空密文")
            refs[-1]["ciphertext"] = ciphertext
        return f"[{field_type.upper()}_REF:{ref_id}]"

    return NUMERIC_PATTERN.sub(replace, content), refs, protected


def preprocess_sales_input(payload: Mapping[str, Any], *, now: datetime | None = None,
                           encrypt_numeric: EncryptNumeric | None = None) -> dict:
    """校验并规范化一条销售文字记录，返回可审计的门禁结果。

    ``accepted=False`` 时不得调用 Agent；``accepted=True`` 的结果只含脱敏正文和
    受控密文，调用方可将 ``numeric_refs`` 映射到 ``add_sensitive_numeric``。
    """
    errors: list[str] = []
    risk_flags: list[str] = []
    required = ("idempotency_key", "customer_id", "content", "occurred_at", "submitted_by", "source_type")
    for field in required:
        if field not in payload or payload[field] is None:
            errors.append(f"缺少必填字段：{field}")

    key = str(payload.get("idempotency_key", "")).strip()
    customer_id = str(payload.get("customer_id", "")).strip()
    submitted_by = str(payload.get("submitted_by", "")).strip()
    content = payload.get("content")
    source_type = payload.get("source_type")
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_CHARS or any(ord(ch) < 32 for ch in key):
        errors.append("idempotency_key 为空、过长或包含控制字符")
    if not customer_id or len(customer_id) > MAX_CUSTOMER_ID_CHARS or not CUSTOMER_ID_RE.fullmatch(customer_id):
        errors.append("customer_id 必须是稳定脱敏标识，不得包含明文敏感信息")
    elif rules.check_sensitive(customer_id) != "pass":
        errors.append("customer_id 疑似包含未脱敏敏感信息")
    if not submitted_by or len(submitted_by) > MAX_SUBMITTED_BY_CHARS:
        errors.append("submitted_by 为空或过长")
    if source_type not in SOURCE_TYPES:
        errors.append(f"source_type 非法：{source_type!r}")
    if not isinstance(content, str) or not content.strip():
        errors.append("content 必须是非空字符串")
    elif len(content) < MIN_CONTENT_CHARS or len(content) > MAX_CONTENT_CHARS:
        errors.append(f"content 长度必须在 {MIN_CONTENT_CHARS} 到 {MAX_CONTENT_CHARS} 字符之间")
    elif not re.search(r"[\u3400-\u9fff]", content):
        errors.append("第一版只接受中文销售文字记录")
    if payload.get("language") not in (None, "zh-CN"):
        errors.append("第一版 language 只接受 zh-CN")

    occurred_at: datetime | None = None
    try:
        occurred_at = _parse_occurred_at(payload.get("occurred_at"))
        reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if occurred_at > reference_now + FUTURE_SKEW:
            errors.append("occurred_at 不能明显晚于当前时间")
    except ValueError as exc:
        errors.append(str(exc))

    if isinstance(content, str):
        for pattern in INJECTION_PATTERNS:
            if pattern.search(content):
                errors.append("输入疑似包含 Prompt injection 或越权指令")
                risk_flags.append("prompt_injection_suspected")
                break

    redacted_content = None
    numeric_refs: list[dict] = []
    if not errors and isinstance(content, str):
        try:
            redacted_content, numeric_refs, missing_protection = _redact_numeric(content, key, encrypt_numeric)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if missing_protection:
                errors.append("检测到敏感数值，但未配置受控加密器；禁止进入 Agent")
                risk_flags.append("numeric_protection_missing")
            # 数值已替换为引用后，再检查身份证、手机号、邮箱、密钥等禁止进入下游的内容。
            if rules.check_sensitive(redacted_content) != "pass":
                errors.append("正文包含未脱敏的个人信息、密钥或内部标记")
                risk_flags.append("sensitive_content_detected")

    accepted = not errors
    normalized = None
    if accepted:
        normalized = {
            "idempotency_key": key,
            "customer_id": customer_id,
            "content_redacted": redacted_content,
            "occurred_at": occurred_at,
            "submitted_by": submitted_by,
            "source_type": source_type,
            "source_ref": payload.get("source_ref"),
            "language": "zh-CN",
            "content_hash": hashlib.sha256(redacted_content.encode("utf-8")).hexdigest(),
        }
    return {
        "accepted": accepted,
        "errors": errors,
        "risk_flags": risk_flags,
        "normalized": normalized,
        "numeric_refs": numeric_refs if accepted else [],
    }
