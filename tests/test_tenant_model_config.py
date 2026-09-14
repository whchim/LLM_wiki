"""L4 测试：租户模型配置进库（密钥加密）+ 用量记账 + 配额拦截。

契约：
1. **配置优先级**：库里有配置 → 用它（base_url/model/解密后的 key）；没有 → 回落环境变量；
   两者都没有 → None（调用方决定报错）；
2. **密钥只存密文**：列表接口返回 has_key 而非明文；AES-GCM 的 AAD 绑定 `tenant:purpose`，
   把密文搬到别的租户/用途会解密失败（并回落 env，不静默用错 key）；
3. **purpose 分级**：精确匹配 (tenant, purpose) 优先，其次 (tenant, 'default')；
4. **用量记账**：每次调用写 llm_usage（含失败），记账失败不阻断调用；
5. **配额拦截**：当日 token 超额 → 调用前报错（不静默降级）；
6. **租户隔离**：A 租户看不到 B 的配置与用量（RLS）；
7. **权限**：非 admin 不得读写配置。
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("core", "api"):
    if str(ROOT / _sub) not in sys.path:
        sys.path.insert(0, str(ROOT / _sub))

import db  # noqa: E402
import model_port  # noqa: E402
import sensitive_cipher  # noqa: E402

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)
os.environ.setdefault("ADMIN_INIT_USER", "admin")
os.environ.setdefault("ADMIN_INIT_PASS", "admin123")

TENANT_A = "tenant-l4-a"
TENANT_B = "tenant-l4-b"
TEST_KEY = "v1:" + "A" * 43 + "="          # 合法 base64url 32 字节密钥（测试用）


@pytest.fixture(autouse=True)
def _cipher(monkeypatch):
    monkeypatch.setenv(sensitive_cipher.ENV_KEY, TEST_KEY)
    yield


def _put_config(**overrides) -> int:
    payload = dict(tenant_id=TENANT_A, purpose="default", model="tenant-a-model",
                   base_url="https://tenant-a.example/v1", max_tokens=1234,
                   daily_token_quota=None, updated_by="admin")
    payload.update(overrides)
    return db.upsert_tenant_model_config(**payload)


# ---------- 1/3. 配置优先级与 purpose 分级 ----------

def test_tenant_config_wins_over_env(monkeypatch):
    """库里有配置 → 用它（含解密后的 key），env 仅作回落。"""
    monkeypatch.setenv("MODEL_API_KEY", "env-key")
    monkeypatch.setenv("MODEL_BASE_URL", "https://env.example/v1")
    cipher = sensitive_cipher.SensitiveNumericCipher()
    _put_config(api_key_ciphertext=cipher.encrypt_secret(f"{TENANT_A}:default", "model_api_key", "sk-tenant-a"),
                api_key_key_version=cipher.current_version)

    port = model_port.for_tenant(TENANT_A)
    assert port._model_override == "tenant-a-model"
    assert port._base_url_override == "https://tenant-a.example/v1"
    assert port._api_key_override == "sk-tenant-a"

    legacy = model_port.for_tenant(TENANT_A, purpose="state")
    assert legacy._model_override == "tenant-a-model"       # 回落 (tenant, default)
    assert legacy._purpose == "default"


def test_falls_back_to_env_then_none(monkeypatch):
    """没配置 → 回落环境变量；环境也没有 → None。"""
    monkeypatch.setenv("MODEL_API_KEY", "env-key")
    port = model_port.for_tenant(TENANT_B)
    assert port is not None and port._api_key_override is None       # 用 env 的 key
    assert port._tenant_id == TENANT_B

    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert model_port.for_tenant(TENANT_B) is None


def test_purpose_specific_config_beats_default():
    """purpose 精确匹配优先于 default（可给审核配便宜模型、编译配强模型）。"""
    _put_config(purpose="default", model="model-default")
    _put_config(purpose="review", model="model-cheap")
    assert model_port.tenant_config(TENANT_A)["model"] == "model-default"
    assert model_port.tenant_config(TENANT_A, purpose="review")["model"] == "model-cheap"
    assert model_port.for_tenant(TENANT_A, purpose="review")._model_override == "model-cheap"


# ---------- 2. 密钥只存密文 + 搬移失效 ----------

def test_key_is_stored_encrypted_and_masked_in_listing(monkeypatch):
    cipher = sensitive_cipher.SensitiveNumericCipher()
    _put_config(api_key_ciphertext=cipher.encrypt_secret(f"{TENANT_A}:default", "model_api_key", "sk-secret"),
                api_key_key_version=cipher.current_version)
    configs = db.list_tenant_model_configs(TENANT_A)
    assert configs[0]["has_key"] is True
    assert "ciphertext" not in json.dumps(configs, default=str)     # 列表不暴露密文
    with db.get_conn() as conn:
        stored = conn.execute("SELECT api_key_ciphertext FROM tenant_model_configs "
                              "WHERE tenant_id=%s", (TENANT_A,)).fetchone()[0]
    assert "sk-secret" not in stored and stored.startswith("v1:")


def test_ciphertext_cannot_be_moved_across_tenants_or_purposes():
    """AAD 绑定 tenant:purpose：密文搬到别处解密失败 → 回落 env，不静默用错 key。"""
    cipher = sensitive_cipher.SensitiveNumericCipher()
    blob = cipher.encrypt_secret(f"{TENANT_A}:default", "model_api_key", "sk-a")
    with pytest.raises(sensitive_cipher.CipherError):
        cipher.decrypt_secret(f"{TENANT_B}:default", "model_api_key", blob)
    with pytest.raises(sensitive_cipher.CipherError):
        cipher.decrypt_secret(f"{TENANT_A}:review", "model_api_key", blob)

    # 明文搬到 B 租户 → for_tenant 解密失败即回落（env 无 key 时返回 None）
    _put_config(tenant_id=TENANT_B, api_key_ciphertext=blob)
    assert model_port.for_tenant(TENANT_B) is None


def test_omitting_key_keeps_existing(monkeypatch):
    """api_key 省略 → 保留原密钥；显式空串 → 清空。"""
    cipher = sensitive_cipher.SensitiveNumericCipher()
    _put_config(api_key_ciphertext=cipher.encrypt_secret(f"{TENANT_A}:default", "model_api_key", "sk-keep"),
                api_key_key_version=cipher.current_version)
    _put_config(model="renamed-model")                       # 不传 key
    assert model_port.for_tenant(TENANT_A)._api_key_override == "sk-keep"
    _put_config(api_key_ciphertext="")                       # 显式清空
    config = model_port.tenant_config(TENANT_A)
    assert config["api_key"] is None


# ---------- 4/5. 记账与配额 ----------

class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _payload(text: str = '{"ok": true}') -> bytes:
    return json.dumps({"id": "req-1", "model": "fake-model",
                       "choices": [{"message": {"content": text}}],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 40}}).encode()


def _stub_http(monkeypatch, port):
    monkeypatch.setattr(model_port.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(_payload()))


def test_usage_is_recorded_for_success_and_failure(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "env-key")
    port = model_port.for_tenant(TENANT_A)
    _stub_http(monkeypatch, port)
    token = db.bind_tenant(TENANT_A)
    try:
        port.complete(system_prompt="s", user_prompt="u", max_tokens=10)
        usage = db.llm_usage_summary(TENANT_A)
        assert usage and usage[0]["calls"] == 1
        assert usage[0]["input_tokens"] == 100 and usage[0]["output_tokens"] == 40
        assert db.llm_usage_today(TENANT_A) == 140

        def _boom(req, timeout=None):
            raise model_port.urllib.error.URLError("refused")

        monkeypatch.setattr(model_port.urllib.request, "urlopen", _boom)
        with pytest.raises(model_port.ModelPortError):
            port.complete(system_prompt="s", user_prompt="u", max_tokens=10)
        usage = db.llm_usage_summary(TENANT_A)
        assert usage[0]["calls"] == 2 and usage[0]["errors"] == 1     # 失败也记账
    finally:
        db.reset_tenant(token)


def test_usage_recording_failure_does_not_break_call(monkeypatch):
    """记账失败不阻断调用（可观测性/账务不绑架主链路）。"""
    monkeypatch.setenv("MODEL_API_KEY", "env-key")
    port = model_port.for_tenant(TENANT_A)
    _stub_http(monkeypatch, port)

    def _boom(**kwargs):
        raise RuntimeError("账务表不可写")

    monkeypatch.setattr(db, "record_llm_usage", _boom)
    response = port.complete(system_prompt="s", user_prompt="u", max_tokens=10)
    assert response.raw_text == '{"ok": true}'


def test_quota_blocks_call_before_hitting_model(monkeypatch):
    """配额用尽 → 调用前报错，模型零调用（不静默降级）。"""
    monkeypatch.setenv("MODEL_API_KEY", "env-key")
    token = db.bind_tenant(TENANT_A)
    try:
        db.record_llm_usage(tenant_id=TENANT_A, purpose="default", model="m",
                            input_tokens=500, output_tokens=500)
        _put_config(daily_token_quota=1000)
        port = model_port.for_tenant(TENANT_A)
        called = {"n": 0}
        monkeypatch.setattr(model_port.urllib.request, "urlopen",
                            lambda req, timeout=None: called.__setitem__("n", called["n"] + 1))
        with pytest.raises(model_port.ModelPortError) as exc:
            port.complete(system_prompt="s", user_prompt="u", max_tokens=10)
        assert "配额已用尽" in str(exc.value) and called["n"] == 0

        _put_config(daily_token_quota=100000)                # 提高配额后恢复可用
        _stub_http(monkeypatch, port)
        assert model_port.for_tenant(TENANT_A).complete(
            system_prompt="s", user_prompt="u", max_tokens=10).raw_text == '{"ok": true}'
    finally:
        db.reset_tenant(token)


# ---------- 6/7. 隔离与权限 ----------

def test_config_is_tenant_scoped(monkeypatch):
    _put_config(tenant_id=TENANT_A, model="model-a")
    _put_config(tenant_id=TENANT_B, model="model-b")
    assert [c["model"] for c in db.list_tenant_model_configs(TENANT_A)] == ["model-a"]
    assert [c["model"] for c in db.list_tenant_model_configs(TENANT_B)] == ["model-b"]


def test_admin_api_requires_admin_and_masks_key():
    from fastapi.testclient import TestClient
    from api.main import app
    from api import auth as auth_mod

    client = TestClient(app)
    admin = client.post("/auth/login", json={"username": "admin", "password": "admin123"}).json()
    headers = {"Authorization": f"Bearer {admin['access_token']}"}

    with db.get_conn() as conn:
        conn.execute("INSERT INTO users (username, password_hash, role, tenant_id) "
                     "VALUES (%s,%s,'user',%s)",
                     ("l4-plain", auth_mod.hash_password("l4plain123"), db.DEFAULT_TENANT))
    plain = client.post("/auth/login", json={"username": "l4-plain", "password": "l4plain123"}).json()
    denied = client.get("/admin/model-configs", headers={"Authorization": f"Bearer {plain['access_token']}"})
    assert denied.status_code == 403

    created = client.put("/admin/model-configs", headers=headers, json={
        "purpose": "compile", "model": "qwen-max", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "sk-live-secret", "daily_token_quota": 5000})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["key_updated"] is True and body["purpose"] == "compile"

    listing = client.get("/admin/model-configs", headers=headers).json()
    row = next(r for r in listing if r["purpose"] == "compile")
    assert row["has_key"] is True and row["daily_token_quota"] == 5000
    assert "sk-live-secret" not in json.dumps(listing, ensure_ascii=False)   # 明文绝不出接口

    usage = client.get("/admin/llm-usage", headers=headers).json()
    assert usage["tenant_id"] == db.DEFAULT_TENANT and isinstance(usage["by_purpose"], list)
