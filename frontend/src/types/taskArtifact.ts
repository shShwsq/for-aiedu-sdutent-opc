/**
 * 任务工作区产物类型
 *
 * 对应后端 app/schemas/task_artifact.py 的 TaskArtifactOut。
 * metadata_ 沿用 TaskResult.metadata_ 约定(Python 属性名带下划线,对应 DB 列名 metadata)。
 */

/** 任务工作区产物(diff/patch 等) */
export interface TaskArtifact {
  id: string
  task_id: string
  /** 产物类型:"git_diff"(工作区变更 patch) */
  kind: string
  /** 产物正文(diff/patch 文本) */
  content: string
  /** 元信息,如 { files_changed: number, truncated: boolean, char_count: number } */
  metadata_?: Record<string, unknown> | null
  created_at: string
}

/** 列出产物响应(GET /tasks/{id}/artifacts) */
export interface TaskArtifactsResponse {
  artifacts: TaskArtifact[]
}
