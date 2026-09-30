/**
 * Git Provider 绑定与仓库访问相关类型
 *
 * 对应后端 routers/git_provider.py 的请求/响应模型,统一支持 GitHub / Gitee。
 * provider ∈ 'github' | 'gitee',路由 /git/{provider}/* 按 provider 分派。
 */

/** 支持的 Git 平台 id(与后端 git_provider.PROVIDERS 一致) */
export type GitProvider = 'github' | 'gitee'

/** 平台绑定状态(GET /git/{provider}/status, POST /git/{provider}/bind) */
export interface GitProviderStatus {
  /** 平台 id */
  provider: GitProvider
  /** 是否已绑定(有 access_token,可访问私有仓库) */
  bound: boolean
  /** 平台用户 ID(可能登录时绑定但未授权 repo) */
  provider_user_id: string | null
  /** 平台用户名(实时查 /user,失败则空) */
  provider_login: string | null
  /** 头像 URL */
  avatar_url: string | null
  /** 邮箱不一致(仅 bind 响应可能为 true,status 查询恒为 false) */
  email_mismatch?: boolean
  /** 平台邮箱(不一致时用于弹窗展示;仅支持可验证邮箱的平台有值) */
  provider_email?: string | null
  /** 账号当前邮箱(不一致时用于弹窗展示) */
  current_email?: string | null
  /** access_token 过期时间(ISO 字符串);null=不过期(GitHub)/老数据未记录 */
  token_expires_at?: string | null
  /** access_token 是否已过期(后端基于 expires_at 判断) */
  token_expired?: boolean
  /** 是否支持手动刷新(有 refresh_token,仅 Gitee 为 true) */
  token_refreshable?: boolean
}

/** 邮箱同步结果(PATCH /git/{provider}/sync-email) */
export interface SyncEmailResponse {
  /** 更新后的邮箱 */
  email: string
  /** 是否已验证(GitHub verified primary 视为已验证;Gitee 不支持同步) */
  email_verified: boolean
}

/** 仓库列表项(GET /git/{provider}/repos) */
export interface GitRepoItem {
  /** owner/repo 全名 */
  full_name: string
  /** 仓库名(不含 owner) */
  name: string
  /** 是否私有 */
  private: boolean
  /** 网页 URL */
  html_url: string
  /** 克隆 URL(HTTPS) */
  clone_url: string
  /** 默认分支 */
  default_branch: string
}

/** 仓库列表响应(GET /git/{provider}/repos) */
export interface GitReposResponse {
  repos: GitRepoItem[]
}

/** 绑定请求体(POST /git/{provider}/bind) */
export interface GitBindRequest {
  code: string
}
