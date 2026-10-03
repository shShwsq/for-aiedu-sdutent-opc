/**
 * 工作区路径归一单元测试
 *
 * 覆盖点击文件链接跳转前的路径预处理(两种沙箱模式 × 绝对/相对路径):
 * 1. normalizeSlashes:Windows 反斜杠归一
 * 2. stripWorkspaceRoot:未知工作区根时的前缀启发式(local 临时目录 / sandbox repos)
 * 3. toWorkspaceRelative:按权威 repo_path 剥离(大小写不敏感 + 段边界)+ 兜底
 */
import { describe, expect, it } from 'vitest'

import {
  normalizeSlashes,
  stripWorkspaceRoot,
  toWorkspaceRelative,
} from './workspacePath'

describe('normalizeSlashes', () => {
  it('反斜杠统一成正斜杠并去首尾空白', () => {
    expect(normalizeSlashes('  C:\\a\\b\\c.md  ')).toBe('C:/a/b/c.md')
  })

  it('正斜杠路径保持原样', () => {
    expect(normalizeSlashes('src/main.py')).toBe('src/main.py')
  })
})

describe('stripWorkspaceRoot', () => {
  it('剥掉 sandbox 模式的 /repos/<仓库>/ 前缀', () => {
    expect(stripWorkspaceRoot('/home/user/repos/openclaw-manager/services/app.py'))
      .toBe('services/app.py')
  })

  it('剥掉 local 模式的临时目录工作区根(Windows 反斜杠)', () => {
    // 真实取值:Qoder CLI 在 local 模式 Read 工具上报的绝对路径
    expect(
      stripWorkspaceRoot(
        'C:\\Users\\njwjx\\AppData\\Local\\Temp\\sandbox_local_5_bgvr6o'
        + '\\uploaded_files\\决策文档.md',
      ),
    ).toBe('决策文档.md')
  })

  it('local 模式克隆仓库路径同样剥成仓库相对路径', () => {
    expect(
      stripWorkspaceRoot(
        '/tmp/sandbox_local_ab12/myrepo/backend/app/routers/tasks.py',
      ),
    ).toBe('backend/app/routers/tasks.py')
  })

  it('已是仓库相对路径时保持原样', () => {
    expect(stripWorkspaceRoot('src/main.py')).toBe('src/main.py')
  })

  it('相对路径中段恰好含 repos/、sandbox_local_*/ 段不误剥(仓库内同名目录)', () => {
    // 回归:启发式若不限绝对路径,docs/repos/list/all.md 会被剥成 all.md,
    // 按段名查树落空 → 点击文件链接静默无反应(bug 的另一种触发方式)
    expect(stripWorkspaceRoot('docs/repos/list/all.md')).toBe('docs/repos/list/all.md')
    expect(stripWorkspaceRoot('tests/sandbox_local_abc/data/x.txt'))
      .toBe('tests/sandbox_local_abc/data/x.txt')
  })

  it('非工作区根的绝对路径原样返回(交 repo_path 分支处理)', () => {
    expect(stripWorkspaceRoot('/etc/hosts')).toBe('/etc/hosts')
  })
})

describe('toWorkspaceRelative', () => {
  it('按 repo_path 剥掉 local 模式绝对路径(Windows 大小写不一致也可)', () => {
    const repo = 'C:\\Users\\njwjx\\AppData\\Local\\Temp\\sandbox_local_5_bgvr6o\\uploaded_files'
    // 后端 repo_path 可能经 Path.resolve() 改写盘符大小写
    expect(
      toWorkspaceRelative(
        'c:\\users\\njwjx\\appdata\\local\\temp\\sandbox_local_5_bgvr6o\\uploaded_files\\doc.md',
        repo,
      ),
    ).toBe('doc.md')
  })

  it('repo_path 带尾斜杠时不影响剥离', () => {
    expect(
      toWorkspaceRelative('/home/user/repos/x/src/a.py', '/home/user/repos/x/'),
    ).toBe('src/a.py')
  })

  it('目标路径带尾斜杠时一并去掉(树内条目路径不带尾斜杠)', () => {
    expect(
      toWorkspaceRelative('/home/user/repos/x/src/a.py/', '/home/user/repos/x'),
    ).toBe('src/a.py')
  })

  it('相对路径原样返回', () => {
    expect(toWorkspaceRelative('backend/app.py', '/home/user/repos/x')).toBe('backend/app.py')
  })

  it('相对路径含 repos/ 段时不被启发式误剥(applyLocate 回归)', () => {
    // 源码查阅 locateFile 传仓库相对路径时,旧实现直接透传可定位;
    // 不能因中段恰好叫 repos/ 就剥掉前缀
    expect(toWorkspaceRelative('docs/repos/list/all.md', '/home/user/repos/x'))
      .toBe('docs/repos/list/all.md')
  })

  it('repo_path 为空时退回启发式剥离', () => {
    expect(toWorkspaceRelative('/home/user/repos/x/app.py', '')).toBe('app.py')
  })

  it('repo_path 与实际路径不同源时不被误剥成空(走启发式兜底)', () => {
    expect(
      toWorkspaceRelative('/tmp/sandbox_local_9/repo2/x.py', '/tmp/sandbox_local_9/repo'),
    ).toBe('x.py')
  })

  it('空路径返回空串', () => {
    expect(toWorkspaceRelative('  ', '/home/user/repos/x')).toBe('')
  })
})
