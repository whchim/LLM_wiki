"""敏感数值受控加密器测试（SA-01：精确金额不得明文进入 Agent/日志/向量索引）。

锁住的边界：
1. 加解密往返正确；密文不含明文
2. AAD 绑定 ref_id 与 field_type——密文不能搬到别的引用或字段
3. 密钥轮换：新数据用当前版本，旧版本仍可解密；缺失版本明确报错
4. 配置错误（缺前缀/长度不对/base64 非法）必须显式失败，不得静默降级
5. 生产环境未配置密钥 → 启动 fail-fast
"""
import base64
import secrets

import pytest

import sensitive_cipher
from sensitive_cipher import CipherError, SensitiveNumericCipher

V1 = "v1:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
V2 = "v2:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def test_round_trip_encrypt_decrypt():
    cipher = SensitiveNumericCipher(V1)
    plaintext = "预算大约12.5万元"
    blob = cipher.encrypt("nv-abc", "amount", plaintext)
    assert plaintext not in blob, "密文里不得出现明文"
    assert blob.startswith("v1:")
    assert cipher.decrypt("nv-abc", "amount", blob) == plaintext


def test_ciphertext_differs_between_calls():
    """每次加密使用随机 nonce：同一明文两次密文不同（避免可比较性）。"""
    cipher = SensitiveNumericCipher(V1)
    a = cipher.encrypt("nv-1", "amount", "10万元")
    b = cipher.encrypt("nv-1", "amount", "10万元")
    assert a != b
    assert cipher.decrypt("nv-1", "amount", a) == "10万元"
    assert cipher.decrypt("nv-1", "amount", b) == "10万元"


def test_aad_binds_ref_and_field_type():
    """密文搬运到别的 ref/field 必须失败（AEAD 的 AAD 绑定）。"""
    cipher = SensitiveNumericCipher(V1)
    blob = cipher.encrypt("nv-1", "amount", "10万元")
    with pytest.raises(CipherError):
        cipher.decrypt("nv-2", "amount", blob)
    with pytest.raises(CipherError):
        cipher.decrypt("nv-1", "discount", blob)


def test_wrong_key_cannot_decrypt():
    blob = SensitiveNumericCipher(V1).encrypt("nv-1", "amount", "10万元")
    other = SensitiveNumericCipher(V2)
    with pytest.raises(CipherError) as exc:
        other.decrypt("nv-1", "amount", blob)
    assert "缺少密钥版本 v1" in str(exc.value)


def test_key_rotation_new_data_uses_latest_old_still_decryptable():
    old = SensitiveNumericCipher(V1).encrypt("nv-1", "amount", "旧数据")
    rotated = SensitiveNumericCipher(f"{V1},{V2}")
    assert rotated.current_version == "v2"
    new = rotated.encrypt("nv-2", "amount", "新数据")
    assert new.startswith("v2:")
    # 旧密文仍可解（v1 保留在配置里）
    assert rotated.decrypt("nv-1", "amount", old) == "旧数据"
    assert rotated.decrypt("nv-2", "amount", new) == "新数据"


def test_dropping_old_version_makes_old_ciphertext_unreadable():
    """删掉旧版本 = 历史密文不可解，这是不可逆操作，必须显式报错而不是返回空。"""
    old = SensitiveNumericCipher(V1).encrypt("nv-1", "amount", "旧数据")
    only_v2 = SensitiveNumericCipher(V2)
    with pytest.raises(CipherError) as exc:
        only_v2.decrypt("nv-1", "amount", old)
    assert "v1" in str(exc.value)


def test_tampered_ciphertext_is_rejected():
    cipher = SensitiveNumericCipher(V1)
    blob = cipher.encrypt("nv-1", "amount", "10万元")
    version, _, payload = blob.partition(":")
    raw = bytearray(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    raw[-1] ^= 0x01  # 篡改最后一字节
    tampered = version + ":" + base64.urlsafe_b64encode(bytes(raw)).decode()
    with pytest.raises(CipherError):
        cipher.decrypt("nv-1", "amount", tampered)


# ---- 配置错误必须显式失败 ----

@pytest.mark.parametrize("spec", [
    pytest.param("", id="empty"),
    pytest.param("abcdef", id="missing-version-prefix"),
    pytest.param("v1:not-base64!!", id="bad-base64"),
    # 密钥长度不足（16 字节）
    pytest.param("v1:" + base64.urlsafe_b64encode(secrets.token_bytes(16)).decode(), id="short-key"),
])
def test_invalid_key_spec_is_rejected(spec):
    with pytest.raises(CipherError):
        SensitiveNumericCipher(spec)


def test_is_available_reflects_config(monkeypatch):
    monkeypatch.delenv(sensitive_cipher.ENV_KEY, raising=False)
    assert sensitive_cipher.is_available() is False
    monkeypatch.setenv(sensitive_cipher.ENV_KEY, V1)
    assert sensitive_cipher.is_available() is True


def test_require_ready_fails_fast_in_production(monkeypatch):
    monkeypatch.delenv(sensitive_cipher.ENV_KEY, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(CipherError) as exc:
        sensitive_cipher.require_ready()
    assert "SENSITIVE_FIELD_KEY" in str(exc.value)


def test_require_ready_allows_development_without_key(monkeypatch):
    """开发环境未配置不阻止启动——但门禁会拒绝含金额的纪要（不静默降级为明文）。"""
    monkeypatch.delenv(sensitive_cipher.ENV_KEY, raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    sensitive_cipher.require_ready()


def test_generated_key_is_valid():
    spec = sensitive_cipher.generate_key()
    assert SensitiveNumericCipher(spec).current_version == "v1"
    assert SensitiveNumericCipher(spec).decrypt(
        "nv-x", "amount", SensitiveNumericCipher(spec).encrypt("nv-x", "amount", "1万元")) == "1万元"
