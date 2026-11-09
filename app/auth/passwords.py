"""密码慢哈希、用户名规范与输入规则。

密码使用 Argon2id；不在应用内实现哈希算法。登录用户不存在时也执行一次
dummy 校验，避免响应时间直接暴露用户名是否存在。
"""

from __future__ import annotations

import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 32

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)
# 固定 dummy 哈希：只用于“用户不存在”时消耗近似校验时间。
_DUMMY_HASH = _hasher.hash("f012-dummy-password-not-a-real-account")


class AuthPasswordError(ValueError):
    """密码或用户名校验失败。"""


def normalize_username(raw: str) -> str:
    """NFKC + 去除首尾空白 + casefold；只用于唯一比较。"""

    return unicodedata.normalize("NFKC", raw or "").strip().casefold()


def validate_username(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw or "").strip()
    if not (MIN_USERNAME_LENGTH <= len(text) <= MAX_USERNAME_LENGTH):
        raise AuthPasswordError(
            f"用户名长度需为 {MIN_USERNAME_LENGTH}–{MAX_USERNAME_LENGTH} 个字符。"
        )
    for char in text:
        if char in "_-":
            continue
        if not char.isalnum():
            raise AuthPasswordError("用户名只能使用字母、数字、下划线或连字符。")
    return text


def validate_password(raw: str) -> str:
    text = raw or ""
    if not (MIN_PASSWORD_LENGTH <= len(text) <= MAX_PASSWORD_LENGTH):
        raise AuthPasswordError(
            f"密码长度需为 {MIN_PASSWORD_LENGTH}–{MAX_PASSWORD_LENGTH} 个字符。"
        )
    return text


def hash_password(password: str) -> str:
    return _hasher.hash(validate_password(password))


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def dummy_verify(password: str) -> None:
    """用户不存在时执行；结果恒为 False，不向调用方返回。"""

    try:
        _hasher.verify(_DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return