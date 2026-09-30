/**
 * 认证相关类型定义
 *
 * 与后端 app/schemas/user.py 一一对应
 */

/** 用户信息(后端 UserResponse) */
export interface User {
  id: string
  email: string
  email_verified: boolean
  /** 已关联的 git provider id 列表(如 ['github'] / ['github', 'gitee']) */
  git_providers: string[]
  has_password: boolean
  created_at: string
}

/** 登录成功响应(后端 TokenResponse) */
export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  user: User
}

/** 刷新成功响应(后端 RefreshResponse) */
export interface RefreshResponse {
  access_token: string
  token_type: string
}

/** 通用消息响应(后端 MessageResponse) */
export interface MessageResponse {
  message: string
}

/** 修改密码请求(后端 ChangePasswordRequest) */
export interface ChangePasswordRequest {
  /** 当前密码;OAuth 用户(无密码)可不传 */
  current_password?: string
  new_password: string
}

/** 后端错误响应(FastAPI HTTPException 格式) */
export interface ApiError {
  detail: string
}
