/**
 * client.ts token 存储工具单元测试
 *
 * 覆盖 getAccessToken / getRefreshToken / setTokens / clearTokens 四个工具函数。
 * 这些函数封装 localStorage 读写,是认证状态的基础:
 * - 漏读 token → 请求不带 Authorization,后端 401
 * - 漏写 token → 登录后无法保持会话
 * - 漏清 token → 登出后仍可访问受保护资源
 *
 * 不测 axios 拦截器(涉及 HTTP mock,留作集成测试);只测纯 localStorage 操作。
 */
import { beforeEach, describe, expect, it } from 'vitest'

import { clearTokens, getAccessToken, getRefreshToken, setTokens } from './client'

describe('token storage utilities', () => {
  beforeEach(() => {
    // 每个测试前清空 localStorage,保证隔离
    localStorage.clear()
  })

  // ============================================================
  // getAccessToken / getRefreshToken
  // ============================================================

  it('无 token 时 getAccessToken 返回 null', () => {
    expect(getAccessToken()).toBeNull()
  })

  it('无 token 时 getRefreshToken 返回 null', () => {
    expect(getRefreshToken()).toBeNull()
  })

  // ============================================================
  // setTokens
  // ============================================================

  it('setTokens 后 getAccessToken 返回写入值', () => {
    setTokens('access-abc', 'refresh-xyz')
    expect(getAccessToken()).toBe('access-abc')
  })

  it('setTokens 后 getRefreshToken 返回写入值', () => {
    setTokens('access-abc', 'refresh-xyz')
    expect(getRefreshToken()).toBe('refresh-xyz')
  })

  it('setTokens 两次以最后一次为准(覆盖)', () => {
    setTokens('old-access', 'old-refresh')
    setTokens('new-access', 'new-refresh')
    expect(getAccessToken()).toBe('new-access')
    expect(getRefreshToken()).toBe('new-refresh')
  })

  it('setTokens 写入空字符串 token(边界:不应抛异常)', () => {
    expect(() => setTokens('', '')).not.toThrow()
    expect(getAccessToken()).toBe('')
    expect(getRefreshToken()).toBe('')
  })

  // ============================================================
  // clearTokens
  // ============================================================

  it('clearTokens 后 getAccessToken 返回 null', () => {
    setTokens('access', 'refresh')
    clearTokens()
    expect(getAccessToken()).toBeNull()
  })

  it('clearTokens 后 getRefreshToken 返回 null', () => {
    setTokens('access', 'refresh')
    clearTokens()
    expect(getRefreshToken()).toBeNull()
  })

  it('clearTokens 在未设置 token 时也不抛异常(幂等)', () => {
    expect(() => clearTokens()).not.toThrow()
    expect(getAccessToken()).toBeNull()
  })

  it('clearTokens 后可重新 setTokens(清空→重写循环)', () => {
    setTokens('a1', 'r1')
    clearTokens()
    setTokens('a2', 'r2')
    expect(getAccessToken()).toBe('a2')
    expect(getRefreshToken()).toBe('r2')
  })

  // ============================================================
  // 隔离性:access 与 refresh 独立
  // ============================================================

  it('access 与 refresh token 互不干扰(不同 key)', () => {
    setTokens('access-only-test', 'refresh-only-test')
    expect(getAccessToken()).toBe('access-only-test')
    expect(getRefreshToken()).toBe('refresh-only-test')
    // 两者应是不同的 localStorage key
    expect(getAccessToken()).not.toBe(getRefreshToken())
  })

  // ============================================================
  // 持久性(同 localStorage 实例内)
  // ============================================================

  it('token 在多次读取间持久(jsdom localStorage 行为契约)', () => {
    setTokens('persistent-access', 'persistent-refresh')
    expect(getAccessToken()).toBe('persistent-access')
    expect(getAccessToken()).toBe('persistent-access') // 二次读
    expect(getRefreshToken()).toBe('persistent-refresh')
  })
})
