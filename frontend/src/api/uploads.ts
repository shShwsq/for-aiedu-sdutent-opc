/**
 * 上传 API 模块
 *
 * 对应后端 app/routers/uploads.py(POST /uploads)。
 * 上传与任务创建解耦:先上传拿 upload_id,提交任务时随
 * TaskCreateRequest.upload_id 引用;上传内容长期保留在服务端,
 * 任务失败重试 / 完成后追问 resume 均可复用。
 */
import client from './client'

/** POST /uploads 响应(后端 UploadResponse) */
export interface UploadResult {
  /** 上传唯一标识(提交任务时引用) */
  upload_id: string
  /** 交付物类型:zip(解压成树)/ file(单文件) */
  kind: 'zip' | 'file'
  /** 客户端原始文件名 */
  filename: string
  /** 上传体字节数 */
  size: number
  /** 实际文件数(zip 为解压后条目数,单文件为 1) */
  file_count: number
}

/**
 * 上传任务交付物(ZIP 或单文件,需登录)
 *
 * 后端按文件名后缀分流:.zip 解压存树,其余按单文件原样存。
 * 大文件放宽超时(axios 默认 30s 对 100MB 上传偏紧)。
 */
export function uploadTaskFile(file: File): Promise<UploadResult> {
  const form = new FormData()
  form.append('file', file)
  return client
    .post<UploadResult>('/uploads', form, {
      timeout: 300_000,
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    .then((r) => r.data)
}
