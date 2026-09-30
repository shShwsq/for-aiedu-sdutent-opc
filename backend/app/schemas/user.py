"""用户相关请求/响应模型"""
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


# ============================================================
# 请求模型
# ============================================================


class RegisterRequest(BaseModel):
    """邮箱密码注册"""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    """邮箱密码登录"""

    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    """刷新 access token"""

    refresh_token: str


class VerifyEmailRequest(BaseModel):
    """邮箱验证(token)"""

    token: str


class ResendVerificationRequest(BaseModel):
    """重发验证邮件"""

    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    """忘记密码(发重置邮件)"""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """重置密码(token + 新密码)"""

    token: str
    new_password: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    """修改密码(已登录用户)

    - current_password: 当前密码;OAuth 用户(无密码)可不传
    - new_password: 新密码
    """

    current_password: str | None = None
    new_password: str = Field(min_length=8, max_length=128)


class GitOAuthRequest(BaseModel):
    """Git 平台 OAuth 登录(github / gitee,传 code)"""

    code: str


# ============================================================
# 响应模型
# ============================================================


class UserResponse(BaseModel):
    """用户信息"""

    id: uuid.UUID
    email: str
    email_verified: bool
    git_providers: list[str]  # 已关联的 git provider id 列表(github / gitee)
    has_password: bool
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_user(cls, user) -> "UserResponse":
        """从 User ORM 模型构造(转 email_verified_at → bool)

        git_providers 从 user_git_bindings 聚合:需在请求级 session 内调用,
        通过 user.git_bindings 关系访问(见 models 反向引用)。
        若关系未加载,默认空列表(兼容无 session 场景)。
        """
        bindings = getattr(user, "git_bindings", None) or []
        return cls(
            id=user.id,
            email=user.email,
            email_verified=user.is_email_verified,
            git_providers=[b.provider for b in bindings],
            has_password=bool(user.password_hash),
            created_at=user.created_at,
        )


class TokenResponse(BaseModel):
    """登录成功响应"""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    user: UserResponse


class RefreshResponse(BaseModel):
    """刷新成功响应"""

    access_token: str
    token_type: str = "Bearer"


class MessageResponse(BaseModel):
    """通用消息响应"""

    message: str


class DeleteAccountRequest(BaseModel):
    """删除账号请求体

    要求用户输入完整邮箱作为二次确认,后端校验匹配后才执行硬删除。
    """

    email: str
