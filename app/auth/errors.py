"""认证领域错误；接口层统一映射。"""


class AuthError(Exception):
    """认证相关错误基类。"""


class InvalidCredentialsError(AuthError):
    """用户名/密码错误或账号已禁用；对外使用统一文案。"""


class UsernameTakenError(AuthError):
    """用户名已存在。"""


class InviteInvalidError(AuthError):
    """邀请码不存在、已使用或已过期。"""


class AuthStorageError(AuthError):
    """认证数据库不可用或损坏。"""