"""本地工具实现(阶段 1)

阶段 1:直接在宿主机执行,不进沙箱
阶段 2 起会替换为沙箱实现,工具接口保持一致

实现 spec 8.4 的最小工具集:
- clone_repo:克隆 GitHub 仓库
- read_file:读取仓库内文件
- search_code:grep 搜索代码

OpenAI function-calling 工具定义放在 tools/schema.py,实现放在本文件
"""
import os
import re
import subprocess
from pathlib import Path

from app.config import settings


# ============================================================
# 工具 1:clone_repo
# ============================================================


def clone_repo(repo_url: str, branch: str | None = None) -> dict:
    """克隆 GitHub 仓库到本地临时目录

    自动把 HTTPS URL 转成 SSH URL,绕过 Windows schannel SSL 问题
    (用户机有 Clash 代理 + schannel backend 冲突,SSH 不受影响)
    如果传入的已经是 SSH URL 或其他形式,原样使用

    返回:{ "path": str, "files_count": int }
    """
    # 统一转成 SSH URL(只处理 github.com HTTPS)
    clone_url = _to_ssh_url(repo_url)

    # 从 URL 提取仓库名作为目录名
    # git@github.com:owner/repo(.git) -> repo
    match = re.search(r"/([^/]+?)(?:\.git)?$", clone_url)
    if not match:
        raise ValueError(f"无法从 URL 解析仓库名: {repo_url}")
    repo_name = match.group(1)

    # 仓库克隆根目录(确保存在)
    clone_root = Path(settings.REPO_CLONE_DIR).resolve()
    clone_root.mkdir(parents=True, exist_ok=True)

    # 用时间戳 + 仓库名作为唯一目录,避免冲突
    import time

    target_dir = clone_root / f"{repo_name}_{int(time.time())}"

    cmd = ["git", "clone"] + (["--depth", str(settings.REPO_CLONE_DEPTH)] if settings.REPO_CLONE_DEPTH > 0 else [])
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([clone_url, str(target_dir)])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=settings.REPO_CLONE_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone 失败: {result.stderr[:500]}")

    # 统计文件数(排除 .git)
    files_count = sum(
        1
        for _ in target_dir.rglob("*")
        if _.is_file() and ".git" not in _.parts
    )

    return {"path": str(target_dir), "files_count": files_count}


def _to_ssh_url(repo_url: str) -> str:
    """把 GitHub HTTPS URL 转成 SSH URL

    https://github.com/owner/repo(.git)  ->  git@github.com:owner/repo.git
    git@github.com:owner/repo.git        ->  原样返回
    其他 URL                              ->  原样返回(非 GitHub)
    """
    # 已经是 SSH 形式
    if repo_url.startswith("git@"):
        return repo_url

    # GitHub HTTPS → SSH
    m = re.match(r"^https?://github\.com/(.+?)(?:\.git)?/?$", repo_url)
    if m:
        path = m.group(1)
        return f"git@github.com:{path}.git"

    # 非 GitHub 或无法识别,原样返回
    return repo_url


# ============================================================
# 工具 2:read_file
# ============================================================


def read_file(repo_path: str, file_path: str, max_lines: int = 500) -> dict:
    """读取仓库内文件内容

    参数:
        repo_path: clone_repo 返回的 path
        file_path: 仓库内相对路径
        max_lines: 最大返回行数(避免大文件撑爆上下文)

    返回:{ "path": str, "content": str, "total_lines": int, "truncated": bool }
    """
    full_path = Path(repo_path) / file_path
    # 防路径穿越
    if not full_path.resolve().is_relative_to(Path(repo_path).resolve()):
        raise ValueError("非法路径:不能超出仓库根目录")

    if not full_path.is_file():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    # 尝试 UTF-8 解码,失败则跳过二进制
    try:
        content = full_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {
            "path": file_path,
            "content": "(二进制文件,无法显示)",
            "total_lines": 0,
            "truncated": False,
        }

    lines = content.splitlines()
    total_lines = len(lines)
    truncated = total_lines > max_lines
    if truncated:
        lines = lines[:max_lines]

    return {
        "path": file_path,
        "content": "\n".join(lines),
        "total_lines": total_lines,
        "truncated": truncated,
    }


# ============================================================
# 工具 3:search_code
# ============================================================


def search_code(
    repo_path: str,
    pattern: str,
    *,
    file_glob: str | None = None,
    case_sensitive: bool = False,
    max_matches: int = 50,
) -> dict:
    """在仓库内 grep 搜索代码

    参数:
        repo_path: clone_repo 返回的 path
        pattern: 正则表达式
        file_glob: 文件过滤(如 "*.py"),None 则搜所有文本文件
        case_sensitive: 是否大小写敏感
        max_matches: 最多返回匹配数,避免结果过大

    返回:{
        "matches": [
            {"file": str, "line": int, "content": str},
            ...
        ],
        "total_matches": int,
        "truncated": bool
    }
    """
    # 用 ripgrep 优先(快),fallback 用 grep
    rg_available = _is_rg_available()

    if rg_available:
        matches = _search_with_ripgrep(
            repo_path, pattern, file_glob, case_sensitive, max_matches
        )
    else:
        matches = _search_with_python(
            repo_path, pattern, file_glob, case_sensitive, max_matches
        )

    total = len(matches)
    truncated = total >= max_matches
    return {
        "matches": matches[:max_matches],
        "total_matches": total,
        "truncated": truncated,
    }


def _is_rg_available() -> bool:
    try:
        r = subprocess.run(["rg", "--version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _search_with_ripgrep(
    repo_path: str,
    pattern: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_matches: int,
) -> list[dict]:
    cmd = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color=never",
        "--max-count",
        str(max_matches),
    ]
    if not case_sensitive:
        cmd.append("-i")
    if file_glob:
        cmd.extend(["--glob", file_glob])
    cmd.extend(["-e", pattern, repo_path])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    matches = []
    for line in result.stdout.splitlines():
        # 格式:file:line:content
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file, line_no, content = parts
        # 去掉 repo_path 前缀,只保留相对路径
        rel_file = os.path.relpath(file, repo_path)
        matches.append(
            {"file": rel_file, "line": int(line_no), "content": content}
        )
    return matches


def _search_with_python(
    repo_path: str,
    pattern: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_matches: int,
) -> list[dict]:
    """纯 Python 实现的 grep fallback(rg 不可用时)"""
    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(pattern, flags)

    # 默认跳过 .git 和常见二进制目录
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    # 简单的文本文件扩展名过滤(避免读二进制)
    text_exts = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
        ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
        ".kt", ".scala", ".sh", ".bash", ".yaml", ".yml", ".json",
        ".xml", ".html", ".css", ".scss", ".md", ".txt", ".toml",
        ".cfg", ".ini", ".env",
    }

    matches = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in text_exts:
                continue
            if file_glob and not _match_glob(fname, file_glob):
                continue

            fpath = os.path.join(root, fname)
            try:
                with open(fpath, encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            rel = os.path.relpath(fpath, repo_path)
                            matches.append(
                                {"file": rel, "line": i, "content": line.rstrip()}
                            )
                            if len(matches) >= max_matches:
                                return matches
            except (PermissionError, OSError):
                continue
    return matches


def _match_glob(filename: str, pattern: str) -> bool:
    """简单实现 glob 匹配(只支持 *.ext 形式)"""
    import fnmatch

    return fnmatch.fnmatch(filename, pattern)
