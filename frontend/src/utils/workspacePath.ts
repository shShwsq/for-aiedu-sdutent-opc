/**
 * 工作区路径归一:把「执行器/模型给出的路径」转成「工作区相对路径」
 *
 * 为什么需要:前端文件树与上传回退树的条目路径恒为**相对 repo_path 的正斜杠
 * 路径**(后端 browse_tree 用 relpath 并把 \ 换成 /),但点击跳转要用的路径来自
 * 两个不可控来源,给的常是工作区**绝对路径**:
 * - CLI 执行器(Qoder / dsh / Codex)的 Read、Bash 工具
 * - agent2 结果 metadata 的 file_path 与题目的 source_file(模型照抄它读到的路径)
 *
 * 两种运行模式下绝对路径的前缀不同:
 * - sandbox:`/home/user/repos/<仓库>/...`,剥掉 `/repos/<仓库>/` 即可
 * - local(SANDBOX_MODE=local):宿主机临时目录 `<tmp>/sandbox_local_<随机>/<仓库>/...`,
 *   Windows 上还带反斜杠
 * 只认 `/repos/` 的规则在 local 模式下匹配不上,路径会原样带着绝对前缀去按段名
 * 查树节点,必然落空(表现为点击文件链接静默无反应)。故:
 * - 拿得到后端权威 repo_path 时优先按它剥离(toWorkspaceRelative)
 * - 拿不到时退回前缀启发式(stripWorkspaceRoot,用于展示层缩短路径)
 */

/** local 模式临时目录工作区根:`<...>/sandbox_local_<随机>/<仓库名>/<相对路径>`
 *  (目录名前缀来自后端 tempfile.mkdtemp(prefix="sandbox_local_")) */
const LOCAL_ROOT_RE = /(?:^|\/)sandbox_local_[^/]+\/[^/]+\/(.+)$/
/** sandbox 模式工作区根:`/home/user/repos/<仓库名>/<相对路径>` */
const SANDBOX_ROOT_RE = /(?:^|\/)repos\/[^/]+\/(.+)$/

/** 反斜杠统一为正斜杠并去首尾空白(Windows 宿主机路径与树内路径同构) */
export function normalizeSlashes(p: string): string {
  return p.trim().replace(/\\/g, '/')
}

/** 去掉尾部多余斜杠(仅跳转路径需要:树内条目路径不带尾斜杠,带着匹配不上;
 *  展示层不能去——目录摘要靠尾斜杠表达“这是个目录”) */
function dropTrailingSlash(p: string): string {
  return p.replace(/\/+$/, '')
}

/** 形似绝对路径:以 / 开头(POSIX / UNC)或 Windows 盘符(x:/) */
function looksAbsolute(norm: string): boolean {
  return norm.startsWith('/') || /^[A-Za-z]:\//.test(norm)
}

/**
 * 不知道工作区根时的启发式剥离:命中 local 临时目录根或 sandbox 仓库根则返回其
 * 后的相对路径,否则原样返回(本就相对的路径不需要剥)。
 *
 * 仅对形似绝对路径的输入尝试:相对路径即便恰好含 repos/、sandbox_local_xx 段
 * (仓库内存在同名目录,如 docs/repos/list/all.md)也不能误剥——剥了按段名
 * 查树必落空,这正是要修复的「点击无反应」bug 的另一种触发方式。
 *
 * 也用于展示:摘要里的路径文本去掉冗长的宿主机/沙箱前缀,只留仓库内位置
 * (保留尾斜杠等原文特征,不影响摘要可读性)。
 */
export function stripWorkspaceRoot(p: string): string {
  const norm = normalizeSlashes(p)
  if (!looksAbsolute(norm)) return norm
  const m = norm.match(LOCAL_ROOT_RE) ?? norm.match(SANDBOX_ROOT_RE)
  return m ? m[1] : norm
}

/**
 * 转成工作区相对路径:跳转/读文件前对来路不明的路径统一过一道
 *
 * @param p 任意路径(绝对或相对,`\` 或 `/`)
 * @param repoPath 后端 /workspace 接口返回的工作区根绝对路径;缺省则只走启发式
 */
export function toWorkspaceRelative(p: string, repoPath = ''): string {
  const norm = dropTrailingSlash(normalizeSlashes(p))
  if (!norm) return ''
  const root = dropTrailingSlash(normalizeSlashes(repoPath))
  const relative = stripRootPrefix(norm, root)
  if (relative) return relative
  return stripWorkspaceRoot(norm)
}

/**
 * norm 以 root 为前缀时返回剩余部分,否则返回 null。
 *
 * 两处宽松比对是必需的:
 * - 大小写不敏感:Windows 路径大小写不敏感,且后端 repo_path 可能经
 *   Path.resolve() 改写过盘符/目录名大小写(C:\\ 与 c:\\ 并存)
 * - 段边界对齐:剩余部分必须以 / 开头,避免 root=/tmp/repo 误剥 /tmp/repo2/x.py
 */
function stripRootPrefix(norm: string, root: string): string | null {
  if (!root || norm.length <= root.length) return null
  if (norm.slice(0, root.length).toLowerCase() !== root.toLowerCase()) return null
  const tail = norm.slice(root.length)
  if (!tail.startsWith('/')) return null
  return tail.slice(1)
}
