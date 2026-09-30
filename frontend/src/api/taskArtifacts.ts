/**
 * 任务工作区产物 API
 *
 * 对应后端 app/routers/tasks.py:
 * - GET /tasks/{id}/artifacts          列出产物
 * - GET /tasks/{id}/artifacts/{aid}    查询单个产物(含完整 content)
 */
import client from './client'
import type { TaskArtifact, TaskArtifactsResponse } from '@/types/taskArtifact'

/** 列出任务的工作区产物(diff/patch 等),按 created_at 升序 */
export function listArtifacts(taskId: string): Promise<TaskArtifactsResponse> {
  return client.get(`/tasks/${taskId}/artifacts`).then((r) => r.data)
}

/** 查询单个工作区产物(含完整 content) */
export function getArtifact(
  taskId: string,
  artifactId: string,
): Promise<TaskArtifact> {
  return client.get(`/tasks/${taskId}/artifacts/${artifactId}`).then((r) => r.data)
}
