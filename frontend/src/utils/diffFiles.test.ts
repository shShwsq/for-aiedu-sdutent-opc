import { describe, expect, it } from 'vitest'

import { parseDiffChangedFiles, parseDiffFileSegments } from './diffFiles'

/** 构造单文件 diff 块 */
function fileBlock(
  path: string,
  opts: { added?: boolean; deleted?: boolean; quoted?: boolean } = {},
): string {
  const q = (p: string) => (opts.quoted ? `"${p}"` : p)
  const oldLine = opts.added ? '--- /dev/null' : `--- a/${q(path)}`
  const newLine = opts.deleted ? '+++ /dev/null' : `+++ b/${q(path)}`
  return [
    `diff --git a/${q(path)} b/${q(path)}`,
    'index 1111111..2222222 100644',
    oldLine,
    newLine,
    '@@ -1 +1 @@',
    '-old',
    '+new',
  ].join('\n')
}

describe('parseDiffChangedFiles', () => {
  it('空 patch 返回空列表', () => {
    expect(parseDiffChangedFiles('')).toEqual([])
  })

  it('解析普通修改文件', () => {
    expect(parseDiffChangedFiles(fileBlock('src/app.py'))).toEqual(['src/app.py'])
  })

  it('新增文件(--- /dev/null)取新路径', () => {
    expect(parseDiffChangedFiles(fileBlock('docs/new.md', { added: true }))).toEqual([
      'docs/new.md',
    ])
  })

  it('删除文件(+++ /dev/null)取旧路径', () => {
    expect(parseDiffChangedFiles(fileBlock('legacy/gone.js', { deleted: true }))).toEqual([
      'legacy/gone.js',
    ])
  })

  it('带引号路径去除引号', () => {
    expect(parseDiffChangedFiles(fileBlock('dir/a b.txt', { quoted: true }))).toEqual([
      'dir/a b.txt',
    ])
  })

  it('多文件按出现顺序去重', () => {
    const patch = [
      fileBlock('a.py'),
      fileBlock('b.py'),
      fileBlock('a.py'), // 同路径再次出现(理论上罕见,验证去重)
    ].join('\n')
    expect(parseDiffChangedFiles(patch)).toEqual(['a.py', 'b.py'])
  })

  it('不误判 hunk 内容里形似文件头的行', () => {
    // 新增内容行 "++ foo" 在 patch 中呈现为 "+++ foo",若脱离头部状态则不应被识别
    const patch = [
      fileBlock('real.py'),
      '+more content',
      '+++ foo should not count',
      '--- bar should not count',
    ].join('\n')
    expect(parseDiffChangedFiles(patch)).toEqual(['real.py'])
  })
})

describe('parseDiffFileSegments', () => {
  it('返回每个文件块的起始行号(diff --git 头所在行)', () => {
    const patch = [fileBlock('a.py'), fileBlock('b.py')].join('\n')
    const lines = patch.split('\n')
    const segments = parseDiffFileSegments(lines)
    expect(segments).toHaveLength(2)
    expect(segments[0]).toEqual({ path: 'a.py', lineIndex: 0 })
    // 第一个块 7 行,第二个块头在第 7 行(0-based)
    expect(segments[1]).toEqual({ path: 'b.py', lineIndex: 7 })
    expect(lines[segments[1].lineIndex].startsWith('diff --git ')).toBe(true)
  })

  it('重命名块取新路径', () => {
    const patch = [
      'diff --git a/old.py b/new.py',
      'similarity index 90%',
      'rename from old.py',
      'rename to new.py',
      '--- a/old.py',
      '+++ b/new.py',
      '@@ -1 +1 @@',
    ].join('\n')
    expect(parseDiffChangedFiles(patch)).toEqual(['new.py'])
  })
})
