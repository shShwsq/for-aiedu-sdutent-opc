/**
 * 工作区浏览相关类型
 *
 * 对应后端 app/routers/workspace.py 的端点
 */

/** 目录条目(文件或子目录) */
export interface WorkspaceEntry {
  name: string
  type: 'file' | 'dir'
  size: number
}

/** 列出目录的响应 */
export interface WorkspaceFilesResponse {
  path: string
  entries: WorkspaceEntry[]
  total: number
  truncated: boolean
}

/** 工作区信息 */
export interface WorkspaceInfo {
  available: boolean
  reason: string | null
  repo_path: string
  completed: boolean
  mode: string
}

/** 工作区恢复(POST /tasks/{id}/workspace/restore)的响应 */
export interface WorkspaceRestoreResponse {
  available: boolean
  repo_path: string
  mode: string
}

/** 整树快照条目(相对仓库根的路径) */
export interface WorkspaceTreeEntry {
  path: string
  type: 'file' | 'dir'
}

/** 整树快照的响应 */
export interface WorkspaceTreeResponse {
  entries: WorkspaceTreeEntry[]
  /** 超上限截断(未覆盖目录前端退回懒加载) */
  truncated: boolean
  /** 快照实际覆盖深度(降级时可能小于请求值) */
  max_depth: number
}

/** 读取文件的响应 */
export interface WorkspaceFileResponse {
  path: string
  content: string
  start_line: number
  end_line: number
  total_lines: number
  truncated: boolean
}
