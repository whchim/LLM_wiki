"""敏感数值的受控加密器（SA-01：精确金额不得明文进入 Agent / 日志 / 向量索引）。

设计要点：
- **AEAD（AES-256-GCM）**：加密与完整性一起保证；不手搓密码学构造，直接用 cryptography
- **key_version**：密文带版本号，落库时一并保存，支持密钥轮换——
  `SENSITIVE_FIELD_KEY` 可配多个版本（逗号分隔），新数据用最后一个版本加密，
  旧版本保留用于解密历史数据；删掉旧版本即等于让对应历史密文不可解
- **明文生命周期**：明文只在 `encrypt()` 调用栈内存在，由 sales_preprocess 的回调传来；
  本模块不写日志、不落库明文、不返回明文
- **fail-fast**：生产环境（APP_ENV=production）缺少密钥直接拒绝启动相关功能；
  开发环境未配置时 `is_available()` 为 False，调用方应拒绝让正文进 Agent（沿用门禁语义）

配置格式（环境变量 SENSITIVE_FIELD_KEY）：
    v1:<base64url 32 字节密钥>
    v1:<...>,v2:<...>          # 轮换：加密用 v2，v1 仍可解密
生成密钥：
    python -c "import secrets,base64;print('v1:'+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
"""
from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENV_KEY = "SENSITIVE_FIELD_KEY"
NONCE_BYTES = 12          # AES-GCM 标准 nonce 长度
KEY_BYTES = 32            # AES-256
DEFAULT_KEY_VERSION = "v1"


class CipherError(Exception):
    """密钥缺失/格式错误/解密失败。调用方应转人工或拒绝入库，不得静默降级为明文。"""


def generate_key(version: str = DEFAULT_KEY_VERSION) -> str:
    """生成一条可直接放进环境变量的密钥（供部署时使用）。"""
    import secrets

    raw = base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode()
    return f"{version}:{raw}"


def _parse_keys(spec: str) -> "list[tuple[str, bytes]]":
    """解析 'v1:<b64>,v2:<b64>' → [(v1, key), (v2, key)]，顺序即轮换顺序（最后为当前）。"""
    parsed: list[tuple[str, bytes]] = []
    for chunk in str(spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise CipherError(f"密钥条目缺少版本前缀（应为 v1:<base64>）：{chunk[:16]}…")
        version, _, material = chunk.partition(":")
        version = version.strip()
        if not version:
            raise CipherError("密钥版本不能为空")
        try:
            key = base64.urlsafe_b64decode(material.strip() + "=" * (-len(material.strip()) % 4))
        except (binascii.Error, ValueError) as exc:
            raise CipherError(f"密钥版本 {version} 不是合法 base64") from exc
        if len(key) != KEY_BYTES:
            raise CipherError(f"密钥版本 {version} 长度应为 {KEY_BYTES} 字节，实际 {len(key)}")
        parsed.append((version, key))
    if not parsed:
        raise CipherError(f"未配置 {ENV_KEY}")
    return parsed


def is_available() -> bool:
    """是否已配置可用密钥（供调用方决定能否让正文进 Agent）。"""
    try:
        _parse_keys(os.environ.get(ENV_KEY, ""))
    except CipherError:
        return False
    return True


class SensitiveNumericCipher:
    """受控加密器：满足 sales_preprocess 的 encrypt_numeric 回调签名。

    回调签名：encrypt(ref_id, field_type, plaintext) -> ciphertext
    额外绑定 ref_id/field_type 作为 AAD，使密文不能被搬移到别的引用或字段上。
    """

    def __init__(self, spec: str | None = None):
        self._keys = _parse_keys(spec if spec is not None else os.environ.get(ENV_KEY, ""))

    @property
    def current_version(self) -> str:
        return self._keys[-1][0]

    def encrypt(self, ref_id: str, field_type: str, plaintext: str) -> str:
        version, key = self._keys[-1]
        nonce = os.urandom(NONCE_BYTES)
        aad = f"{ref_id}|{field_type}".encode("utf-8")
        blob = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
        return f"{version}:{base64.urlsafe_b64encode(nonce + blob).decode()}"

    def decrypt(self, ref_id: str, field_type: str, ciphertext: str) -> str:
        """解密（供授权角色在审计下取回精确值的底层能力；本模块不做权限判断）。"""
        if not isinstance(ciphertext, str) or ":" not in ciphertext:
            raise CipherError("密文格式非法")
        version, _, payload = ciphertext.partition(":")
        key = next((k for v, k in self._keys if v == version), None)
        if key is None:
            raise CipherError(f"缺少密钥版本 {version}，无法解密（可能已被轮换删除）")
        try:
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        except (binascii.Error, ValueError) as exc:
            raise CipherError("密文不是合法 base64") from exc
        if len(raw) <= NONCE_BYTES:
            raise CipherError("密文长度异常")
        aad = f"{ref_id}|{field_type}".encode("utf-8")
        try:
            return AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad).decode("utf-8")
        except InvalidTag as exc:
            raise CipherError("密文校验失败：密钥不匹配、密文被篡改，或引用/字段被搬移") from exc


    def encrypt_secret(self, ref_id: str, kind: str, plaintext: str) -> str:
        """加密**密钥类**明文（L4：租户自带模型 API Key）。

        与 `encrypt` 同一套 AES-GCM（AAD 绑定 ref_id|kind），只是语义命名更清楚：
        密钥与数值走同一条受控加密路径，轮换机制（v1/v2）也共用。
        """
        return self.encrypt(ref_id, f"secret:{kind}", plaintext)

    def decrypt_secret(self, ref_id: str, kind: str, ciphertext: str) -> str:
        return self.decrypt(ref_id, f"secret:{kind}", ciphertext)


def require_ready() -> None:
    """启动时校验：生产环境必须配置密钥，否则拒绝启动（与 JWT_SECRET 同策略）。"""
    if is_available():
        return
    if os.environ.get("APP_ENV", "development").lower() in {"prod", "production"}:
        raise CipherError(
            f"生产环境必须配置 {ENV_KEY}（否则含金额的纪要无法提交）；"
            f"生成方式：python -c \"import secrets,base64;print('v1:'+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\"")
