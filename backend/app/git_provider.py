"""Git Provider 统一抽象层

把不同 Git 托管平台(GitHub / Gitee)的 OAuth 与 API 差异收敛到统一的
`GitProvider` 接口背后,上层路由 / 克隆工具只面对 provider id 与统一返回结构,
不再硬编码任一平台的 URL、scope 或 token 注入格式。

两套流程(沿用原 GitHub 设计):
- 登录流程(scope 仅用户信息):拿 provider_user_id + email,用于登录/创建账号
- 绑定流程(scope 额外含仓库):额外拿 access_token 落库,用于克隆私有仓库

平台差异要点:
- GitHub:token 注入 `https://x-access-token:{token}@github.com/...`;有
  /user/emails(verified primary)端点,支持邮箱同步。
- Gitee:token 注入 `https://oauth2:{token}@gitee.com/...`(用户名必须为字面量
  oauth2);无 verified-emails 端点,不支持邮箱同步;repos 接口无 clone_url,
  需由 full_name 构造。
"""
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class GitProviderError(Exception):
    """Git provider OAuth / API 错误"""


@dataclass
class ProviderUserInfo:
    """统一用户信息(各平台 /user 接口归一化后)"""

    provider_user_id: str  # 平台用户 ID(GitHub id / Gitee id)
    email: str | None  # 可能为 None(邮箱私密且平台无可验证邮箱端点)
    login: str | None  # 用户名(如 octocat),一般有值
    name: str | None  # 显示名(很多人不填,可能为 None)
    avatar_url: str | None


@dataclass
class OAuthTokenSet:
    """OAuth 换 token 的完整结果

    - GitHub:token 默认不过期,无 refresh_token → refresh_token=None, expires_in=None
    - Gitee:access_token 有有效期(expires_in 秒),refresh_token 用于续期;
      刷新接口会旋转 refresh_token,必须用响应里的新值覆盖。
    """

    access_token: str
    refresh_token: str | None = None  # None=平台不支持刷新(GitHub)
    expires_in: int | None = None  # 秒;None=不过期(GitHub)或响应未带


class GitProvider(ABC):
    """Git 托管平台抽象基类

    子类需实现平台相关的 OAuth 换 token、用户信息、仓库列表等。
    URL 转换(to_ssh_url / to_https_url / inject_token_in_https)基于 `host`
    与 `token_username` 做通用实现,子类只需声明这两个属性。
    """

    id: str  # "github" / "gitee"
    display_name: str  # "GitHub" / "Gitee"(前端展示)
    host: str  # "github.com" / "gitee.com"
    token_username: str  # HTTPS 克隆鉴权的用户名部分
    authorize_url: str  # OAuth 授权页 URL(前端拼接用,后端仅做元信息)
    token_url: str  # OAuth 换 token 端点
    user_url: str  # /user 接口
    repos_url: str  # /user/repos 接口
    scope_login: str  # 登录用 scope
    scope_bind: str  # 绑定用 scope(含仓库访问)

    # ---- OAuth / API(子类实现) ----

    @abstractmethod
    def exchange_code_for_token(self, code: str) -> OAuthTokenSet:
        """用授权码换 token,失败抛 GitProviderError

        返回 OAuthTokenSet(access_token + refresh_token + expires_in)。
        不支持刷新的平台(GitHub)refresh_token / expires_in 为 None。
        """

    @abstractmethod
    def refresh_access_token(self, refresh_token: str) -> OAuthTokenSet:
        """用 refresh_token 换新的 access_token,失败抛 GitProviderError

        不支持刷新的平台(GitHub)直接抛 GitProviderError。
        支持的平台(Gitee)返回新的 OAuthTokenSet(refresh_token 可能被旋转)。
        """

    @abstractmethod
    def get_user_info(self, access_token: str) -> ProviderUserInfo:
        """用 access_token 调 /user 拿用户信息,失败抛 GitProviderError"""

    @abstractmethod
    def get_user_emails(self, access_token: str) -> list[str]:
        """拿可用的(可验证)邮箱列表,无则返回空"""

    @abstractmethod
    def list_repos(self, access_token: str) -> list[dict]:
        """列出当前用户仓库(含私有),返回统一字段:
        [{ "full_name", "name", "private", "html_url", "clone_url", "default_branch" }, ...]
        """

    @abstractmethod
    def revoke_token(self, access_token: str) -> None:
        """在平台端撤销 access_token,使其立即失效

        用于解绑场景:仅清本地 token 不够,平台端旧 token 仍有效会导致
        重新绑定时 OAuth 授权状态粘性(平台跳过授权页 → 换 token 失败)。
        失败应抛 GitProviderError,由上层捕获并降级(不阻塞解绑)。
        """

    @property
    @abstractmethod
    def supports_verified_email(self) -> bool:
        """是否支持可验证邮箱(决定能否做邮箱同步)"""

    @property
    def supports_token_refresh(self) -> bool:
        """是否支持用 refresh_token 续期 access_token

        默认 False(GitHub token 不过期,无需刷新);Gitee 重写为 True。
        """
        return False

    # ---- 通用流程 ----

    def oauth_login(self, code: str) -> ProviderUserInfo:
        """完整 OAuth 登录流程:code → access_token → /user,并补充邮箱

        失败抛 GitProviderError。登录流程不落库 token,仅用 access_token 拉用户信息。
        """
        token_set = self.exchange_code_for_token(code)
        info = self.get_user_info(token_set.access_token)

        # 若公开 email 为空,尝试拿可验证邮箱
        if not info.email:
            emails = self.get_user_emails(token_set.access_token)
            if emails:
                info.email = emails[0]

        return info

    # ---- URL 转换(基于 host / token_username 的通用实现) ----

    def to_ssh_url(self, repo_url: str) -> str:
        """把 HTTPS URL 转成 SSH URL(已是 SSH 则原样返回)"""
        if repo_url.startswith("git@"):
            return repo_url
        m = re.match(
            rf"^https?://{re.escape(self.host)}/(.+?)(?:\.git)?/?$", repo_url
        )
        if m:
            return f"git@{self.host}:{m.group(1)}.git"
        return repo_url

    def to_https_url(self, repo_url: str) -> str:
        """把 SSH URL 转成 HTTPS URL(已是 HTTPS 则原样返回)"""
        m = re.match(rf"^git@{re.escape(self.host)}:(.+?)(?:\.git)?$", repo_url)
        if m:
            return f"https://{self.host}/{m.group(1)}.git"
        return repo_url

    def inject_token_in_https(self, https_url: str, token: str) -> str:
        """把 HTTPS URL 注入 access_token,形成带认证的 clone URL

        GitHub: https://x-access-token:{token}@github.com/owner/repo.git
        Gitee:  https://oauth2:{token}@gitee.com/owner/repo.git

        若 URL 非本平台 HTTPS 或已含认证信息,原样返回。
        """
        if not token or not https_url.startswith(f"https://{self.host}/"):
            return https_url
        # 已含认证信息(user:pass@),不重复注入
        authority = https_url.split("://", 1)[1].split("/", 1)[0]
        if "@" in authority:
            return https_url
        return https_url.replace(
            "https://",
            f"https://{self.token_username}:{token}@",
            1,
        )


# ============================================================
# GitHub
# ============================================================


class GitHubProvider(GitProvider):
    id = "github"
    display_name = "GitHub"
    host = "github.com"
    token_username = "x-access-token"
    authorize_url = "https://github.com/login/oauth/authorize"
    token_url = "https://github.com/login/oauth/access_token"
    user_url = "https://api.github.com/user"
    repos_url = "https://api.github.com/user/repos"
    scope_login = "user:email"
    scope_bind = "user:email repo"

    @property
    def supports_verified_email(self) -> bool:
        return True

    def exchange_code_for_token(self, code: str) -> OAuthTokenSet:
        if not settings.GITHUB_OAUTH_CLIENT_ID or not settings.GITHUB_OAUTH_CLIENT_SECRET:
            raise GitProviderError(
                "GitHub OAuth 未配置:请在 .env 设置 GITHUB_OAUTH_CLIENT_ID 和 GITHUB_OAUTH_CLIENT_SECRET"
            )
        payload = {
            "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
            "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
        }
        headers = {"Accept": "application/json"}
        try:
            with httpx.Client(timeout=10) as client:
                r = client.post(self.token_url, data=payload, headers=headers)
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitProviderError(f"换取 access_token 失败: {e}") from e

        data = r.json()
        access_token = data.get("access_token")
        if not access_token:
            err = data.get("error_description") or data.get("error") or "未知错误"
            raise GitProviderError(f"换取 access_token 失败: {err}")
        # GitHub OAuth token 默认不过期,无 refresh_token
        return OAuthTokenSet(access_token=access_token)

    def refresh_access_token(self, refresh_token: str) -> OAuthTokenSet:
        """GitHub token 不支持刷新(默认不过期,无需续期)"""
        raise GitProviderError("GitHub token 不支持刷新")

    def get_user_info(self, access_token: str) -> ProviderUserInfo:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(self.user_url, headers=headers)
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitProviderError(f"获取用户信息失败: {e}") from e

        data = r.json()
        provider_user_id = str(data.get("id") or "")
        if not provider_user_id:
            raise GitProviderError("GitHub 用户信息缺少 id 字段")
        return ProviderUserInfo(
            provider_user_id=provider_user_id,
            email=data.get("email"),
            login=data.get("login"),
            name=data.get("name"),
            avatar_url=data.get("avatar_url"),
        )

    def get_user_emails(self, access_token: str) -> list[str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(
                    "https://api.github.com/user/emails", headers=headers
                )
                r.raise_for_status()
        except httpx.HTTPError:
            return []

        emails = []
        for item in r.json():
            if item.get("primary") and item.get("verified"):
                emails.append(item.get("email", ""))
        return [e for e in emails if e]

    def list_repos(self, access_token: str) -> list[dict]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        params = {
            "affiliation": "owner,collaborator",
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
        }
        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(self.repos_url, headers=headers, params=params)
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitProviderError(f"列出 GitHub 仓库失败: {e}") from e

        repos = []
        for item in r.json():
            repos.append({
                # 用 `or` 兜底:GitHub 空仓库 default_branch 也为 null,
                # dict.get 的 None 陷阱会导致下游 GitRepoItem Pydantic 校验失败 → 500
                "full_name": item.get("full_name") or "",
                "name": item.get("name") or "",
                "private": bool(item.get("private", False)),
                "html_url": item.get("html_url") or "",
                "clone_url": item.get("clone_url") or "",
                "default_branch": item.get("default_branch") or "main",
            })
        return repos

    def revoke_token(self, access_token: str) -> None:
        """撤销 GitHub access_token(DELETE /applications/{client_id}/token)

        用 client_id + client_secret 做 Basic Auth 鉴权(非用户 token 鉴权),
        撤销后该 token 立即失效,且会清除用户对该 OAuth App 的授权状态,
        下次绑定时 GitHub 会重新显示授权页(避免授权状态粘性导致换 token 失败)。

        参考:https://docs.github.com/en/rest/apps/oauth-applications#delete-an-app-token
        """
        if not settings.GITHUB_OAUTH_CLIENT_ID or not settings.GITHUB_OAUTH_CLIENT_SECRET:
            raise GitProviderError("GitHub OAuth 未配置,无法 revoke token")

        url = f"https://api.github.com/applications/{settings.GITHUB_OAUTH_CLIENT_ID}/token"
        try:
            with httpx.Client(timeout=10) as client:
                # GitHub 要求 Basic Auth(client_id:client_secret),不能用 Bearer token
                # httpx 的 client.delete() 不支持 json body,用 client.request() 代替
                r = client.request(
                    "DELETE",
                    url,
                    auth=(settings.GITHUB_OAUTH_CLIENT_ID, settings.GITHUB_OAUTH_CLIENT_SECRET),
                    headers={"Accept": "application/vnd.github+json"},
                    json={"access_token": access_token},
                )
                # GitHub 成功返回 204 No Content;404 表示 token 已不存在(视为已撤销)
                if r.status_code not in (204, 404):
                    r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitProviderError(f"撤销 GitHub token 失败: {e}") from e


# ============================================================
# Gitee
# ============================================================


class GiteeProvider(GitProvider):
    id = "gitee"
    display_name = "Gitee"
    host = "gitee.com"
    token_username = "oauth2"  # Gitee HTTPS 克隆鉴权用户名必须为字面量 oauth2
    authorize_url = "https://gitee.com/oauth/authorize"
    token_url = "https://gitee.com/oauth/token"
    user_url = "https://gitee.com/api/v5/user"
    repos_url = "https://gitee.com/api/v5/user/repos"
    scope_login = "user_info"
    scope_bind = "user_info projects"  # 克隆私有仓库需 projects

    @property
    def supports_verified_email(self) -> bool:
        return False  # Gitee 无 GitHub 式 verified-emails 端点,不支持邮箱同步

    @property
    def supports_token_refresh(self) -> bool:
        return True  # Gitee access_token 有有效期,支持用 refresh_token 续期

    def exchange_code_for_token(self, code: str) -> OAuthTokenSet:
        if not settings.GITEE_OAUTH_CLIENT_ID or not settings.GITEE_OAUTH_CLIENT_SECRET:
            raise GitProviderError(
                "Gitee OAuth 未配置:请在 .env 设置 GITEE_OAUTH_CLIENT_ID 和 GITEE_OAUTH_CLIENT_SECRET"
            )
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.GITEE_OAUTH_CLIENT_ID,
            "client_secret": settings.GITEE_OAUTH_CLIENT_SECRET,
            "redirect_uri": settings.GITEE_OAUTH_REDIRECT_URI,
        }
        headers = {"Accept": "application/json"}
        try:
            with httpx.Client(timeout=10) as client:
                r = client.post(self.token_url, data=payload, headers=headers)
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitProviderError(f"换取 access_token 失败: {e}") from e

        data = r.json()
        access_token = data.get("access_token")
        if not access_token:
            err = data.get("error_description") or data.get("error") or "未知错误"
            raise GitProviderError(f"换取 access_token 失败: {err}")
        # Gitee 返回 refresh_token + expires_in,用于续期
        refresh_token = data.get("refresh_token") or None
        expires_in = data.get("expires_in")
        try:
            expires_in = int(expires_in) if expires_in is not None else None
        except (TypeError, ValueError):
            expires_in = None
        return OAuthTokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )

    def refresh_access_token(self, refresh_token: str) -> OAuthTokenSet:
        """用 refresh_token 换新的 access_token

        Gitee 刷新接口会旋转 refresh_token,必须用响应里的新值覆盖落库。
        """
        if not settings.GITEE_OAUTH_CLIENT_ID or not settings.GITEE_OAUTH_CLIENT_SECRET:
            raise GitProviderError(
                "Gitee OAuth 未配置:请在 .env 设置 GITEE_OAUTH_CLIENT_ID 和 GITEE_OAUTH_CLIENT_SECRET"
            )
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        headers = {"Accept": "application/json"}
        try:
            with httpx.Client(timeout=10) as client:
                r = client.post(self.token_url, data=payload, headers=headers)
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitProviderError(f"刷新 access_token 失败: {e}") from e

        data = r.json()
        new_access_token = data.get("access_token")
        if not new_access_token:
            err = data.get("error_description") or data.get("error") or "未知错误"
            raise GitProviderError(f"刷新 access_token 失败: {err}")
        new_refresh_token = data.get("refresh_token") or None
        expires_in = data.get("expires_in")
        try:
            expires_in = int(expires_in) if expires_in is not None else None
        except (TypeError, ValueError):
            expires_in = None
        return OAuthTokenSet(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            expires_in=expires_in,
        )

    def get_user_info(self, access_token: str) -> ProviderUserInfo:
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(self.user_url, params={"access_token": access_token})
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitProviderError(f"获取用户信息失败: {e}") from e

        data = r.json()
        provider_user_id = str(data.get("id") or "")
        if not provider_user_id:
            raise GitProviderError("Gitee 用户信息缺少 id 字段")
        # Gitee email 可能为空字符串,统一成 None
        email = data.get("email") or None
        return ProviderUserInfo(
            provider_user_id=provider_user_id,
            email=email,
            login=data.get("login"),
            name=data.get("name"),
            avatar_url=data.get("avatar_url"),
        )

    def get_user_emails(self, access_token: str) -> list[str]:
        """Gitee 无独立的可验证邮箱端点,/user 已返回 email,这里不再二次拉取"""
        return []

    def list_repos(self, access_token: str) -> list[dict]:
        params = {
            "access_token": access_token,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
        }
        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(self.repos_url, params=params)
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitProviderError(f"列出 Gitee 仓库失败: {e}") from e

        repos = []
        for item in r.json():
            full_name = item.get("full_name", "")
            # Gitee repos 接口无 clone_url,由 full_name 构造 HTTPS 克隆地址
            clone_url = f"https://{self.host}/{full_name}.git" if full_name else ""
            repos.append({
                # 用 `or` 兜底:Gitee 空仓库 default_branch 为 null,
                # dict.get(key, default) 在 key 存在但值为 None 时返回 None 而非 default,
                # 会导致下游 GitRepoItem(default_branch: str) Pydantic 校验失败 → 500
                "full_name": full_name or "",
                "name": item.get("name") or "",
                "private": bool(item.get("private", False)),
                "html_url": item.get("html_url") or "",
                "clone_url": clone_url or "",
                "default_branch": item.get("default_branch") or "master",
            })
        return repos

    def revoke_token(self, access_token: str) -> None:
        """撤销 Gitee access_token(DELETE /applications/{client_id}/tokens/{token})

        用 client_secret 作为 query 参数鉴权。撤销后 token 立即失效,
        用户对该 OAuth App 的授权状态也会清除,下次绑定时会重新显示授权页。

        参考:https://gitee.com/api/v5/swagger#/deleteApplicationToken
        """
        if not settings.GITEE_OAUTH_CLIENT_ID or not settings.GITEE_OAUTH_CLIENT_SECRET:
            raise GitProviderError("Gitee OAuth 未配置,无法 revoke token")

        url = (
            f"https://gitee.com/api/v5/applications/"
            f"{settings.GITEE_OAUTH_CLIENT_ID}/tokens/{access_token}"
        )
        try:
            with httpx.Client(timeout=10) as client:
                r = client.delete(
                    url,
                    params={"client_secret": settings.GITEE_OAUTH_CLIENT_SECRET},
                )
                # Gitee 成功返回 200/204;404 表示 token 已不存在(视为已撤销)
                if r.status_code not in (200, 204, 404):
                    r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitProviderError(f"撤销 Gitee token 失败: {e}") from e


# ============================================================
# 注册表
# ============================================================


PROVIDERS: dict[str, GitProvider] = {
    "github": GitHubProvider(),
    "gitee": GiteeProvider(),
}


def get_provider(provider_id: str) -> GitProvider:
    """按 id 取 provider,未知 id 抛 GitProviderError"""
    p = PROVIDERS.get(provider_id)
    if p is None:
        raise GitProviderError(f"未知的 git provider: {provider_id}")
    return p


def get_provider_for_url(repo_url: str) -> GitProvider | None:
    """按 repo_url 的主机识别 provider,未知主机返回 None(走匿名/SSH 回退)"""
    for p in PROVIDERS.values():
        if f"://{p.host}/" in repo_url or f"@{p.host}:" in repo_url:
            return p
    return None
