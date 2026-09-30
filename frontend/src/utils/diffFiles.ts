/**
 * git diff patch 变更文件解析工具
 *
 * 数据来源是任务的 git_diff artifact(任务完成时持久化的全量 patch)。
 * 用于工作区不可用(会话已过期)时,在侧栏展示"变更文件列表",
 * 点击跳转到主区"工作区变更"中对应文件的 diff 块。
 */

/** diff 文件块头部元信息行前缀(diff --git 与 ---/+++ 之间可能出现的行) */
const DIFF_HEADER_META_PREFIXES = [
  'index ',
  'old mode',
  'new mode',
  'new file',
  'deleted file',
  'similarity',
  'dissimilarity',
  'rename ',
  'copy ',
  'Binary files',
  'GIT binary patch',
]

/** 去除 git 对特殊路径的双引号包裹(如 "a b.txt") */
function unquoteGitPath(path: string): string {
  if (path.length >= 2 && path.startsWith('"') && path.endsWith('"')) {
    return path.slice(1, -1)
  }
  return path
}

/** 去除 a/ 或 b/ 路径前缀(仅当存在时) */
function stripGitSidePrefix(path: string, prefix: string): string {
  return path.startsWith(prefix) ? path.slice(prefix.length) : path
}

/** 单个文件 diff 块:变更文件相对路径 + 该块 `diff --git` 头在行数组中的行号 */
export interface DiffFileSegment {
  path: string
  lineIndex: number
}

/**
 * 按文件块解析 patch 行,返回每个块的路径与起始行号(按出现顺序)
 *
 * 解析规则:`diff --git` 进入头部状态,`--- a/P` 记录旧路径,
 * `+++ b/Q` 决定最终路径——普通修改/重命名取新路径 Q,
 * 删除文件(Q 为 /dev/null)取旧路径 P;`@@` 或其他非头部行结束头部状态。
 * 仅在头部状态内识别 ---/+++,避免把 hunk 内容行误判为文件头。
 */
export function parseDiffFileSegments(lines: string[]): DiffFileSegment[] {
  const segments: DiffFileSegment[] = []
  let headerLine = -1 // 当前 diff --git 头行号;-1 表示不在文件头部状态
  let pendingOld = ''
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (line.startsWith('diff --git ')) {
      headerLine = i
      pendingOld = ''
      continue
    }
    if (headerLine < 0) continue
    if (line.startsWith('--- ')) {
      const p = line.slice(4).trim()
      pendingOld = p === '/dev/null' ? '' : unquoteGitPath(stripGitSidePrefix(p, 'a/'))
      continue
    }
    if (line.startsWith('+++ ')) {
      const q = line.slice(4).trim()
      const path =
        q === '/dev/null' ? pendingOld : unquoteGitPath(stripGitSidePrefix(q, 'b/'))
      const startLine = headerLine
      headerLine = -1
      if (path) segments.push({ path, lineIndex: startLine })
      continue
    }
    if (line.startsWith('@@')) {
      headerLine = -1
      continue
    }
    if (!DIFF_HEADER_META_PREFIXES.some((pfx) => line.startsWith(pfx))) {
      // 非头部元信息行(如 Binary files 提示后的内容),退出头部状态
      headerLine = -1
    }
  }
  return segments
}

/**
 * 从统一 diff patch 文本提取变更文件相对路径列表(按出现顺序去重)
 *
 * 空 patch 返回空列表。删除文件记旧路径,新增/修改/重命名记新路径。
 */
export function parseDiffChangedFiles(patch: string): string[] {
  if (!patch) return []
  const seen = new Set<string>()
  const files: string[] = []
  for (const seg of parseDiffFileSegments(patch.split('\n'))) {
    if (!seen.has(seg.path)) {
      seen.add(seg.path)
      files.push(seg.path)
    }
  }
  return files
}
