"""security 单元测试:密码哈希 + JWT 签发/校验 + Fernet 对称加密。

不连真实 DB,不读 .env(用 monkeypatch 固定 settings 关键字段)。

覆盖:
- hash_password / verify_password:加盐哈希 + 校验
- create_access_token / create_refresh_token / create_token_pair:双 token 签发
- decode_token:正常 / 过期 / 签名错 / 类型错
- extract_user_id_from_token:sub → UUID
- encrypt_secret / decrypt_secret:加解密往返 + 密文损坏 + 密钥不匹配
"""
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app import security
from app.security import (
    TokenInvalidError,
    create_access_token,
    create_refresh_token,
    create_token_pair,
    decode_token,
    decrypt_secret,
    encrypt_secret,
    extract_user_id_from_token,
    hash_password,
    verify_password,
)


# ============================================================
# fixtures:固定 settings,避免 .env 干扰
# ============================================================

@pytest.fixture
def fixed_settings(monkeypatch):
    """固定 JWT_SECRET 和 GITHUB_TOKEN_SECRET,保证测试可重现。

    GITHUB_TOKEN_SECRET 必须是合法 Fernet key(32 字节 base64)。
    留空会让 _get_fernet 每次随机生成,encrypt/decrypt 往返虽仍可行
    (同进程内 key 缓存于 settings),但显式固定更安全且可跨测试复用。
    """
    monkeypatch.setattr(security.settings, "JWT_SECRET", "test-jwt-secret-fixed")
    monkeypatch.setattr(security.settings, "JWT_ALGORITHM", "HS256")
    fernet_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(security.settings, "GITHUB_TOKEN_SECRET", fernet_key)
    yield fernet_key
    # 清 Fernet 单例缓存,避免实例绑定上一个测试的密钥泄漏到下一个测试
    security._get_fernet.cache_clear()


# ============================================================
# 密码哈希
# ============================================================

def test_hash_password_returns_bcrypt_hash_with_salt():
    """hash_password 返回 bcrypt 格式哈希($2b$ rounds$salt$hash)。"""
    h = hash_password("mySecret123")
    assert h.startswith("$2b$12$")  # rounds=12
    assert h != "mySecret123"
    assert len(h) >= 50


def test_hash_password_generates_different_salts():
    """同密码两次哈希结果不同(salt 随机)。"""
    h1 = hash_password("samePassword")
    h2 = hash_password("samePassword")
    assert h1 != h2


def test_verify_password_correct():
    """正确密码校验通过。"""
    h = hash_password("correctP@ss")
    assert verify_password("correctP@ss", h) is True


def test_verify_password_wrong():
    """错误密码校验失败。"""
    h = hash_password("correctP@ss")
    assert verify_password("wrongPassword", h) is False


def test_verify_password_empty_strings():
    """空密码与空哈希:不抛异常,返回 False(兜底)。"""
    assert verify_password("", "") is False


def test_verify_password_invalid_hash_returns_false():
    """非法哈希格式(非 bcrypt)应被 catch,返回 False 而非抛异常。"""
    assert verify_password("any", "not-a-valid-hash") is False


# ============================================================
# JWT 签发
# ============================================================

def test_create_access_token_has_access_type(fixed_settings):
    """access token 的 type 字段为 'access'。"""
    user_id = uuid.uuid4()
    token = create_access_token(user_id)
    payload = decode_token(token)
    assert payload["type"] == "access"
    assert payload["sub"] == str(user_id)


def test_create_refresh_token_has_refresh_type(fixed_settings):
    """refresh token 的 type 字段为 'refresh'。"""
    user_id = uuid.uuid4()
    token = create_refresh_token(user_id)
    payload = decode_token(token)
    assert payload["type"] == "refresh"
    assert payload["sub"] == str(user_id)


def test_create_token_pair_returns_two_distinct_tokens(fixed_settings):
    """create_token_pair 返回 (access, refresh) 两个不同 token。"""
    user_id = uuid.uuid4()
    access, refresh = create_token_pair(user_id)
    assert access != refresh
    assert decode_token(access, expected_type="access")["sub"] == str(user_id)
    assert decode_token(refresh, expected_type="refresh")["sub"] == str(user_id)


def test_access_token_expiry_is_15_minutes(fixed_settings):
    """access token exp - iat ≈ 15 分钟(900 秒)。"""
    user_id = uuid.uuid4()
    token = create_access_token(user_id)
    payload = decode_token(token)
    assert payload["exp"] - payload["iat"] == 900


def test_refresh_token_expiry_is_7_days(fixed_settings):
    """refresh token exp - iat ≈ 7 天(604800 秒)。"""
    user_id = uuid.uuid4()
    token = create_refresh_token(user_id)
    payload = decode_token(token)
    assert payload["exp"] - payload["iat"] == 7 * 86400


# ============================================================
# JWT 校验:decode_token
# ============================================================

def test_decode_token_with_expected_type_matching(fixed_settings):
    """expected_type 与 token 类型匹配时正常解码。"""
    user_id = uuid.uuid4()
    access = create_access_token(user_id)
    refresh = create_refresh_token(user_id)
    assert decode_token(access, expected_type="access")["sub"] == str(user_id)
    assert decode_token(refresh, expected_type="refresh")["sub"] == str(user_id)


def test_decode_token_type_mismatch_raises_invalid(fixed_settings):
    """expected_type 与实际类型不符 → TokenInvalidError(防 access 当 refresh 用)。"""
    user_id = uuid.uuid4()
    access = create_access_token(user_id)
    with pytest.raises(TokenInvalidError):
        decode_token(access, expected_type="refresh")


def test_decode_token_expired_raises_expired(fixed_settings):
    """过期 token → TokenExpiredError(直接用 pyjwt 构造一个已过期的 token)。"""
    import jwt as pyjwt
    from datetime import datetime, timezone
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": int(now.timestamp()) - 100,
        "exp": int(now.timestamp()) - 10,  # 已过期 10 秒
    }
    expired_token = pyjwt.encode(
        payload, security.settings.JWT_SECRET, algorithm=security.settings.JWT_ALGORITHM
    )
    with pytest.raises(security.TokenExpiredError):
        decode_token(expired_token)


def test_decode_token_invalid_signature_raises_invalid(fixed_settings, monkeypatch):
    """签名错(用不同 secret 签发)→ TokenInvalidError。"""
    import jwt as pyjwt
    user_id = uuid.uuid4()
    # 用错误的 secret 签发
    bad_token = pyjwt.encode(
        {"sub": str(user_id), "type": "access", "exp": 9999999999, "iat": 1},
        "wrong-secret",
        algorithm="HS256",
    )
    with pytest.raises(TokenInvalidError):
        decode_token(bad_token)


def test_decode_token_garbage_string_raises_invalid(fixed_settings):
    """完全非 JWT 的字符串 → TokenInvalidError(不抛其他异常)。"""
    with pytest.raises(TokenInvalidError):
        decode_token("not.a.jwt.at.all")
    with pytest.raises(TokenInvalidError):
        decode_token("")


# ============================================================
# extract_user_id_from_token
# ============================================================

def test_extract_user_id_returns_uuid(fixed_settings):
    """正常 token 提取 user_id(返回 UUID 类型)。"""
    user_id = uuid.uuid4()
    token = create_access_token(user_id)
    extracted = extract_user_id_from_token(token, expected_type="access")
    assert extracted == user_id
    assert isinstance(extracted, uuid.UUID)


def test_extract_user_id_invalid_token_raises(fixed_settings):
    """无效 token → TokenInvalidError(不返回 None)。"""
    with pytest.raises(TokenInvalidError):
        extract_user_id_from_token("garbage")


# ============================================================
# Fernet 对称加密
# ============================================================

def test_encrypt_decrypt_round_trip(fixed_settings):
    """加密 → 解密 往返一致。"""
    plaintext = "ghp_abc123secretTokenValue"
    ciphertext = encrypt_secret(plaintext)
    assert ciphertext != plaintext
    assert decrypt_secret(ciphertext) == plaintext


def test_encrypt_generates_different_ciphertext_each_call(fixed_settings):
    """同明文两次加密结果不同(Fernet 自带随机 IV)。"""
    p = "same-input"
    c1 = encrypt_secret(p)
    c2 = encrypt_secret(p)
    assert c1 != c2
    assert decrypt_secret(c1) == decrypt_secret(c2) == p


def test_decrypt_tampered_ciphertext_raises_value_error(fixed_settings):
    """密文被篡改 → ValueError(InvalidToken 包装)。"""
    ciphertext = encrypt_secret("original")
    # 篡改末尾字符
    tampered = ciphertext[:-4] + "AAAA"
    with pytest.raises(ValueError):
        decrypt_secret(tampered)


def test_decrypt_with_wrong_key_raises_value_error(fixed_settings, monkeypatch):
    """用不同密钥解密 → ValueError(密钥不匹配)。"""
    ciphertext = encrypt_secret("secret-with-key-A")
    # 换一把新 key(同时清掉 Fernet 单例缓存,让实例按新 key 重建)
    new_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(security.settings, "GITHUB_TOKEN_SECRET", new_key)
    security._get_fernet.cache_clear()
    with pytest.raises(ValueError):
        decrypt_secret(ciphertext)


def test_encrypt_decrypt_empty_string(fixed_settings):
    """空字符串加解密(边界)。"""
    ciphertext = encrypt_secret("")
    assert decrypt_secret(ciphertext) == ""


def test_encrypt_decrypt_unicode_chinese(fixed_settings):
    """中文等多字节字符加解密(UTF-8 编码正确性)。"""
    plaintext = "中文密钥测试🔑"
    ciphertext = encrypt_secret(plaintext)
    assert decrypt_secret(ciphertext) == plaintext
