/**
 * Git Provider 绑定与仓库访问 API
 *
 * 对应后端 app/routers/git_provider.py 的端点(统一 GitHub / Gitee):
 * - POST   /git/{provider}/bind        用 code 换 token 并加密落库
 * - GET    /git/{provider}/status      查看绑定状态
 * - DELETE /git/{provider}/bind        解绑(清除 token)
 * - GET    /git/{provider}/repos       列出当前用户仓库(含私有)
 * - PATCH  /git/{provider}/sync-email  同步平台邮箱(仅支持可验证邮箱的平台)
 * - POST   /git/{provider}/refresh     用 refresh_token 刷新 access_token(仅 Gitee 支持)
 *
 * 绑定流程:
 * - 前端跳到平台授权页(scope 含仓库访问)
 * - 用户授权后平台回调到 redirect_uri?code=XXX
 * - 前端把 code 提交到 POST /git/{provider}/bind
 *
 * 登录用 scope 与绑定用 scope 不同:getOAuthAuthorizeURL(provider, 'login')
 * 用于登录页,getGitBindURL(provider) 用于设置页绑定(含仓库权限)。
 */
import client from './client'
import type {
  GitBindRequest,
  GitProvider,
  GitProviderStatus,
  GitReposResponse,
  SyncEmailResponse,
} from '@/types/git_provider'

/** 各 provider 的 OAuth 元信息(用于前端拼接授权 URL,与后端 git_provider.py 保持一致) */
const PROVIDER_META: Record<
  GitProvider,
  {
    authorizeUrl: string
    /** 登录用 scope(仅用户信息) */
    scopeLogin: string
    /** 绑定用 scope(含仓库访问) */
    scopeBind: string
    /** 前端环境变量:client_id */
    envClientId: 'VITE_GITHUB_OAUTH_CLIENT_ID' | 'VITE_GITEE_OAUTH_CLIENT_ID'
    /** 前端环境变量:redirect_uri */
    envRedirectUri: 'VITE_GITHUB_OAUTH_REDIRECT_URI' | 'VITE_GITEE_OAUTH_REDIRECT_URI'
  }
> = {
  github: {
    authorizeUrl: 'https://github.com/login/oauth/authorize',
    scopeLogin: 'user:email',
    scopeBind: 'user:email repo',
    envClientId: 'VITE_GITHUB_OAUTH_CLIENT_ID',
    envRedirectUri: 'VITE_GITHUB_OAUTH_REDIRECT_URI',
  },
  gitee: {
    authorizeUrl: 'https://gitee.com/oauth/authorize',
    scopeLogin: 'user_info',
    scopeBind: 'user_info projects',
    envClientId: 'VITE_GITEE_OAUTH_CLIENT_ID',
    envRedirectUri: 'VITE_GITEE_OAUTH_REDIRECT_URI',
  },
}

/** 绑定 Git 平台:用 OAuth code 换 token 并加密落库 */
export function bindGitProvider(
  provider: GitProvider,
  req: GitBindRequest,
): Promise<GitProviderStatus> {
  return client.post(`/git/${provider}/bind`, req).then((r) => r.data)
}

/** 查询当前用户的某平台绑定状态 */
export function getGitProviderStatus(provider: GitProvider): Promise<GitProviderStatus> {
  return client.get(`/git/${provider}/status`).then((r) => r.data)
}

/** 解绑某平台(清除 access_token,保留 provider_user_id 关联) */
export function unbindGitProvider(provider: GitProvider): Promise<GitProviderStatus> {
  return client.delete(`/git/${provider}/bind`).then((r) => r.data)
}

/**
 * 用 refresh_token 刷新 access_token(仅 Gitee 支持,GitHub 返回 400)
 *
 * 用于设置页「刷新 token」按钮:token 过期或用户主动续期时调用。
 * 成功后后端已更新 access_token / refresh_token / expires_at,前端刷新 status 即可。
 */
export function refreshGitProviderToken(provider: GitProvider): Promise<GitProviderStatus> {
  return client.post(`/git/${provider}/refresh`).then((r) => r.data)
}

/** 同步邮箱:将账号邮箱更新为平台可验证邮箱(仅 GitHub 支持,Gitee 返回 400) */
export function syncGitProviderEmail(provider: GitProvider): Promise<SyncEmailResponse> {
  return client.patch(`/git/${provider}/sync-email`).then((r) => r.data)
}

/** 列出当前用户某平台仓库(含私有,按更新时间倒序)
 *
 * @param provider 平台标识
 * @param refresh 强制刷新(跳过后端 30s 缓存,直接调平台 API),
 *                用于「刷新」按钮:用户在平台上新建仓库后立即拉取
 */
export function listGitProviderRepos(
  provider: GitProvider,
  refresh = false,
): Promise<GitReposResponse> {
  const params = refresh ? { refresh: 'true' } : undefined
  return client
    .get(`/git/${provider}/repos`, { params })
    .then((r) => r.data)
}

/**
 * 拼接某平台 OAuth 授权链接(登录用,scope 仅用户信息)
 *
 * 前端直接跳转到此 URL,用户授权后平台回调到 redirect_uri?code=XXX
 */
export function getOAuthAuthorizeURL(provider: GitProvider): string {
  const meta = PROVIDER_META[provider]
  const clientId = import.meta.env[meta.envClientId]
  const redirectUri = import.meta.env[meta.envRedirectUri]
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: meta.scopeLogin,
    // Gitee 需要 response_type=code(GitHub 默认即 code,显式传也无害)
    response_type: 'code',
  })
  return `${meta.authorizeUrl}?${params.toString()}`
}

/**
 * 拼接某平台绑定授权链接(scope 含仓库访问权限)
 *
 * 与登录用 scope 区分,额外申请仓库访问权限。复用对应 provider 的 redirect_uri
 * (如 /auth/github/callback),回调页检测已登录后走绑定流程,未登录走登录流程。
 */
export function getGitBindURL(provider: GitProvider): string {
  const meta = PROVIDER_META[provider]
  const clientId = import.meta.env[meta.envClientId]
  const redirectUri = import.meta.env[meta.envRedirectUri]
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: meta.scopeBind,
    response_type: 'code',
  })
  return `${meta.authorizeUrl}?${params.toString()}`
}
