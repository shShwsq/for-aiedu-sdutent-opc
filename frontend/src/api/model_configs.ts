/**
 * 模型设置 API 模块(列表式)
 *
 * 对应后端 app/routers/settings.py 的端点。
 * - GET  /models/catalog        厂商清单
 * - GET  /models/configs        当前用户已存配置列表
 * - PUT  /models/configs        保存配置列表(整体替换)
 * - POST /models/llm/test       测试指定 LLM 配置(按 config_id)
 * - POST /models/embedding/test 测试指定 Embedding 配置(按 config_id)
 */
import client from './client'
import type {
  ModelsCatalog,
  SaveModelsRequest,
  TestRequest,
  TestResponse,
  UserModelsResponse,
} from '@/types/model_configs'

/** 获取厂商与模型清单(无需登录,前端选厂商用) */
export function getCatalog(): Promise<ModelsCatalog> {
  return client.get('/models/catalog').then((r) => r.data)
}

/** 获取当前用户已保存的模型配置列表(不含 api_key 原文) */
export function getMyModels(): Promise<UserModelsResponse> {
  return client.get('/models/configs').then((r) => r.data)
}

/** 保存模型配置列表(整体替换,api_key 空串 = 保留已存的 key) */
export function saveModels(req: SaveModelsRequest): Promise<UserModelsResponse> {
  return client.put('/models/configs', req).then((r) => r.data)
}

/** 测试指定 LLM 配置连通性(按 config_id,使用已保存的配置,需先保存) */
export function testLLM(req: TestRequest): Promise<TestResponse> {
  return client.post('/models/llm/test', req).then((r) => r.data)
}

/** 测试指定 Embedding 配置连通性(按 config_id,使用已保存的配置,需先保存) */
export function testEmbedding(req: TestRequest): Promise<TestResponse> {
  return client.post('/models/embedding/test', req).then((r) => r.data)
}
