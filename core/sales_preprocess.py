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
# 客户标识是"代号"不是"客户信息"：必须稳定（同一客户每次同 id，否则状态机会拆成多个客户），
# 且不得是可定位到具体人/机构的明文。格式限 ASCII 字母数字与 - _ . :，首字符须为字母或数字。
CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
# 提示与校验共用同一份描述，避免"文档说能用、代码却不允许"的漂移（测试会校验示例合法）
CUSTOMER_ID_FORMAT_HINT = "只能用字母、数字和 - _ . :（不能用中文、空格、@），且首字符须为字母或数字"
CUSTOMER_ID_EXAMPLES = ("customer-001", "cust_2026_01")

# 这些模式只做明显的指令混入拦截，不试图判断普通业务文本是否“可信”。
INJECTION_PATTERNS = (
    re.compile(r"(?:忽略|无视|跳过).{0,20}(?:之前|上面|系统).{0,20}(?:指令|规则|提示)", re.I),
    re.compile(r"\b(?:ignore|disregard|override)\b.{0,40}\b(?:system|developer|previous)\b", re.I),
    re.compile(r"(?:调用工具|执行命令|运行脚本|读取密钥|删除文件|发送邮件)", re.I),
    re.compile(r"(?:<\s*(?:system|developer|tool)\s*>|\b(?:bash|powershell|shell)\s*:\s*)", re.I),
)

# 分类词 + 数值：命中一个就整体脱敏。
# 词表刻意**宽**：真实销售口语里的连接词远不止"为/是/约"（"大约是""给到""降到了"
# "说预算"…），词表偏窄会直接漏掉明文金额。实测旧版 7/13 条真实口语句式漏网。
# 注意：**光靠分类词不够**，因此另有 bare_amount 兜底（见 NUMERIC_PATTERN 注释）。
MONEY_FIELDS = ("合同金额", "合同价", "金额", "预算", "报价", "价格", "单价", "费用", "总价", "成本", "款项")
DISCOUNT_FIELDS = ("折让", "折扣", "优惠", "折")   # 长词在前：交替匹配取最长
QUANTITY_FIELDS = ("用户数", "并发数", "数量", "数目", "台数", "席位")
CONNECTORS = ("大约是", "大概为", "大约为", "大概是", "给到", "降到", "谈到", "涨到",
              "说到", "至", "到", "为", "是", "约", ":", "：", "=", " ")
_CONN = r"(?:" + "|".join(CONNECTORS) + r")"
_SP = r"[^0-9\u3400-\u9fff]{0,3}"   # 仅用于兜住空格/标点这类短噪声

NUMERIC_PATTERN = re.compile(
    r"(?P<amount>(?:" + "|".join(MONEY_FIELDS) + r")" + _SP + _CONN + r"?\s*"
    r"\d[\d,]*(?:\.\d+)?\s*(?:万元|万|元|块|人民币|￥|¥)?)"
    r"|(?P<discount>(?:" + "|".join(DISCOUNT_FIELDS) + r")" + _SP + r"(?:" + _CONN + r")?\s*"
    r"\d+(?:\.\d+)?\s*(?:%|折|个点)?)"
    r"|(?P<quantity>(?:" + "|".join(QUANTITY_FIELDS) + r")" + _SP + r"(?:" + _CONN + r")?\s*"
    r"\d[\d,]*\s*(?:个|台|套|席位|人)?)"
    # 金额兜底：**数值 + 明确货币单位**，不要求出现分类词。
    # 真实纪要常只写"50万""12.5万元"而不带"预算"二字；单位留在组外，
    # 靠"数字后必须紧跟货币单位"限定边界，避免 2026 年/编号这类被误吞。
    r"|(?P<bare_amount>\d[\d,]*(?:\.\d+)?\s*(?:万元|万|元|块|人民币|￥|¥))",
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
        field_type = next(name for name in ("amount", "discount", "quantity", "bare_amount")
                          if match.group(name))
        # 兜底组统一归类为金额（落库 field_type 受 schema 约束，没有 bare_amount 这一档）
        field_type = "amount" if field_type == "bare_amount" else field_type
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
    # 空 / 超长 / 字符非法 / 内容敏感 分四种提示：
    # 原来四种共用一个文案，用户填中文或公司名时完全不知道问题出在哪。
    if not customer_id:
        errors.append("customer_id 必填：请填写该客户的稳定脱敏标识")
    elif len(customer_id) > MAX_CUSTOMER_ID_CHARS:
        errors.append(f"customer_id 过长（{len(customer_id)} 字符，上限 {MAX_CUSTOMER_ID_CHARS}）")
    elif not CUSTOMER_ID_RE.fullmatch(customer_id):
        errors.append(
            f"customer_id 格式非法：{CUSTOMER_ID_FORMAT_HINT}。"
            f"当前值 {customer_id[:40]!r}（含中文客户名请改用代号，"
            f"示例：{CUSTOMER_ID_EXAMPLES[0]}）")
    elif rules.check_sensitive(customer_id) != "pass":
        errors.append("customer_id 疑似包含未脱敏敏感信息（如手机号、身份证号），请改用代号")
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
