"""代码质量工具(run_lint / run_coverage)

对齐 run_semgrep 的 local/sandbox 双模式设计:
- local 模式:shutil.which 检测宿主机是否安装对应工具,缺失返回 note 指引(不静默失败)
- sandbox 模式:缺失时 pip install --user --break-system-packages 自动安装

run_lint:静态风格/缺陷检查(Python 走 ruff;JS/TS 仅在有 eslint 配置时尝试)
run_coverage:跑测试并解析覆盖率(Python 走 pytest-cov;JS 检测到 vitest 时尝试)
"""
import json
import logging
import shlex
import shutil
import subprocess
from pathlib import Path

from app.sandbox.client import SandboxSession
from app.tools.sandbox_tools import _get_or_create_session

logger = logging.getLogger(__name__)


# lint/coverage 结果条数上限(控 token)
_MAX_ISSUES = 100
_MAX_UNCOVERED_FILES = 20
# 沙箱内 pip --user 安装后需要把 ~/.local/bin 挂到 PATH(同 semgrep 的做法)
_PATH_PREFIX = 'export PATH="$HOME/.local/bin:$PATH"; '
# ruff / pytest 执行超时(秒)
_LINT_TIMEOUT = 180
_COVERAGE_TIMEOUT = 300


# ============================================================
# 语言/框架探测(纯逻辑,local 与 sandbox 共用,依赖文件存在性检查原语)
# ============================================================


def _make_exists(ctx: dict):
    """返回文件存在性检查函数(local 用 Path,sandbox 用 test -f)"""
    if ctx["mode"] == "local":
        return lambda abs_path: Path(abs_path).is_file()
    session: SandboxSession = ctx["session"]

    def _exists(abs_path: str) -> bool:
        return "OK" in session.run_command(
            f"test -f {shlex.quote(abs_path)} && echo OK || echo MISSING"
        )

    return _exists


def _make_read(ctx: dict):
    """返回文件读取函数(失败返回 None)"""
    if ctx["mode"] == "local":
        def _read(abs_path: str) -> str | None:
            try:
                return Path(abs_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        return _read
    session: SandboxSession = ctx["session"]

    def _read(abs_path: str) -> str | None:
        try:
            return session.read_file(abs_path)
        except Exception:
            return None

    return _read


def _join(repo_path: str, rel: str) -> str:
    return f"{repo_path.rstrip('/')}/{rel}"


def _detect_python_project(repo_path: str, exists) -> bool:
    return any(exists(_join(repo_path, f)) for f in (
        "pyproject.toml", "requirements.txt", "setup.py", "pytest.ini",
    ))


def _read_package_json(repo_path: str, read) -> dict | None:
    text = read(_join(repo_path, "package.json"))
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ============================================================
# 纯函数解析器(单测直接覆盖)
# ============================================================


def parse_ruff_json(output: str, repo_path: str) -> dict:
    """解析 ruff check --output-format json 输出为结构化 issues"""
    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        return {"issues": [], "total": 0, "truncated": False,
                "error": f"ruff 输出解析失败: {e}"}
    if not isinstance(data, list):
        return {"issues": [], "total": 0, "truncated": False,
                "error": "ruff 输出格式异常(期望 JSON 数组)"}
    issues = []
    for item in data[:_MAX_ISSUES]:
        path = item.get("filename", "")
        # 去掉 repo_path 前缀,返回仓库内相对路径
        if path.startswith(repo_path):
            path = path[len(repo_path):].lstrip("/\\")
        loc = item.get("location") or {}
        issues.append({
            "file": path,
            "line": loc.get("row", 0),
            "col": loc.get("column", 0),
            "code": item.get("code", ""),
            "message": (item.get("message") or "")[:200],
        })
    return {"issues": issues, "total": len(data), "truncated": len(data) > _MAX_ISSUES}


def parse_coverage_json(text: str) -> dict:
    """解析 pytest-cov 的 coverage.json(totals + 未覆盖行数 top 文件)"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return {"error": f"coverage.json 解析失败: {e}"}
    totals = data.get("totals") or {}
    files_section = data.get("files") or {}
    uncovered = []
    for path, info in files_section.items():
        missing = info.get("missing_lines") or []
        if missing:
            s = info.get("summary") or {}
            uncovered.append({
                "file": path,
                "uncovered_lines": len(missing),
                "coverage_percent": s.get("percent_covered", 0),
            })
    uncovered.sort(key=lambda x: x["uncovered_lines"], reverse=True)
    return {
        "totals": {
            "coverage_percent": totals.get("percent_covered", 0),
            "covered_lines": totals.get("covered_lines", 0),
            "missing_lines": totals.get("missing_lines", 0),
        },
        "uncovered_files": uncovered[:_MAX_UNCOVERED_FILES],
        "total_files": len(files_section),
    }


# ============================================================
# 工具 1:run_lint
# ============================================================


def run_lint(repo_path: str, language: str = "auto", task_id: str = "") -> dict:
    """运行静态 lint 检查,返回结构化问题清单

    参数:
        repo_path: clone_repo 返回的 path
        language: "auto"(按清单文件探测)/ "python" / "javascript"

    返回:{
        "linter": "ruff", "language": "python",
        "issues": [{"file", "line", "col", "code", "message"}],
        "total": int, "truncated": bool,
    }
    工具不可用(如 local 模式宿主机未装 ruff)时返回 {"note": ...} 指引替代方案。
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]
    exists = _make_exists(ctx)
    read = _make_read(ctx)

    # 语言探测:python 优先(有 py 清单文件);否则有 package.json 视为 js
    lang = (language or "auto").lower()
    if lang == "auto":
        if _detect_python_project(repo_path, exists):
            lang = "python"
        elif exists(_join(repo_path, "package.json")):
            lang = "javascript"
        else:
            return {"note": "未探测到 Python/JS 项目清单文件,无法自动选择 linter;"
                            "可显式传 language 参数,或用 run_semgrep 做静态分析。"}

    if lang == "python":
        if mode == "local":
            return _run_ruff_local(repo_path)
        return _run_ruff_sandbox(ctx, repo_path)

    if lang in ("javascript", "typescript", "js", "ts"):
        return _run_eslint(ctx, repo_path, exists, read)

    return {"note": f"暂不支持的语言: {language}。python/javascript 之外请用 run_semgrep。"}


def _run_ruff_local(repo_path: str) -> dict:
    """local 模式:检测宿主机 ruff,有则执行,无则 note"""
    ruff_bin = shutil.which("ruff")
    if not ruff_bin:
        return {"note": (
            "local 模式未检测到 ruff。可执行 `pip install ruff` 安装后重试,"
            "或切换 SANDBOX_MODE=sandbox(沙箱会自动安装 ruff),"
            "或用 run_semgrep 做静态检查。"
        )}
    cmd = [ruff_bin, "check", "--output-format", "json", repo_path]
    logger.info(f"[local] ruff: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_LINT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"ruff 本地执行超时({_LINT_TIMEOUT}s)"}
    # ruff 退出码:0=无问题,1=有问题(正常结果),其他=执行错误
    if result.returncode not in (0, 1):
        return {"error": f"ruff 执行失败(退出码 {result.returncode}): {result.stderr[:300]}"}
    return {**parse_ruff_json(result.stdout, repo_path),
            "linter": "ruff", "language": "python"}


def _run_ruff_sandbox(ctx: dict, repo_path: str) -> dict:
    """sandbox 模式:缺 ruff 时自动 pip 安装后执行"""
    session: SandboxSession = ctx["session"]
    check = session.run_command(_PATH_PREFIX + "command -v ruff || echo MISSING")
    if "MISSING" in check:
        logger.info("[sandbox] ruff 未安装,尝试 pip install ruff")
        install_result = session.run_command(
            "pip install --user --break-system-packages ruff 2>&1 | tail -3",
            timeout=_LINT_TIMEOUT,
        )
        check2 = session.run_command(_PATH_PREFIX + "command -v ruff || echo MISSING")
        if "MISSING" in check2:
            return {"error": (
                f"ruff 安装失败。安装输出: {install_result.strip()[-300:]}"
            )}
    cmd = (
        _PATH_PREFIX
        + f"cd {shlex.quote(repo_path)} && ruff check --output-format json ."
    )
    logger.info(f"[sandbox] ruff: {cmd}")
    output = session.run_command(cmd, timeout=_LINT_TIMEOUT)
    return {**parse_ruff_json(output, repo_path),
            "linter": "ruff", "language": "python"}


_ESLINT_CONFIG_FILES = (
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
    ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml",
)


def _run_eslint(ctx: dict, repo_path: str, exists, read) -> dict:
    """JS/TS:仅在仓库自带 eslint 配置时尝试,否则 note 引导用 run_semgrep

    eslint 无配置无法运行,且现场装配置+插件成本高,不做自动安装。
    """
    has_config = any(exists(_join(repo_path, f)) for f in _ESLINT_CONFIG_FILES)
    if not has_config:
        pkg = _read_package_json(repo_path, read) or {}
        # package.json 里的 eslintConfig 字段也算配置
        if not pkg.get("eslintConfig"):
            return {"note": (
                "仓库未检测到 eslint 配置文件,跳过 JS lint。"
                "JS/TS 的缺陷检查可改用 run_semgrep(config='p/javascript' 或 'p/typescript')。"
            )}
    session: SandboxSession | None = ctx.get("session")
    if ctx["mode"] == "local":
        npx_bin = shutil.which("npx")
        if not npx_bin:
            return {"note": "local 模式未检测到 npx(Node),无法运行 eslint;"
                            "可改用 run_semgrep 做 JS 静态检查。"}
        cmd = f"npx --no-install eslint . --format json"
        try:
            result = subprocess.run(
                cmd, shell=True, cwd=repo_path,
                capture_output=True, text=True, timeout=_LINT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {"error": f"eslint 本地执行超时({_LINT_TIMEOUT}s)"}
        if result.returncode >= 2:
            return {"error": f"eslint 执行失败(退出码 {result.returncode}): "
                             f"{(result.stderr or result.stdout)[:300]}"}
        return {**_parse_eslint_json(result.stdout, repo_path),
                "linter": "eslint", "language": "javascript"}
    # sandbox:依赖仓库自带 node_modules 里的 eslint(不现场 npm install)
    cmd = (
        f"cd {shlex.quote(repo_path)} && "
        "(command -v npx >/dev/null && npx --no-install eslint . --format json "
        "|| echo ESLINT_UNAVAILABLE)"
    )
    output = session.run_command(cmd, timeout=_LINT_TIMEOUT)
    if "ESLINT_UNAVAILABLE" in output:
        return {"note": "沙箱内无 npx/eslint 可执行(仓库未带 node_modules),"
                        "JS 缺陷检查请改用 run_semgrep。"}
    return {**_parse_eslint_json(output, repo_path),
            "linter": "eslint", "language": "javascript"}


def _parse_eslint_json(output: str, repo_path: str) -> dict:
    """解析 eslint --format json 输出"""
    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        return {"issues": [], "total": 0, "truncated": False,
                "error": f"eslint 输出解析失败: {e}"}
    issues = []
    for file_report in data:
        path = file_report.get("filePath", "")
        if path.startswith(repo_path):
            path = path[len(repo_path):].lstrip("/\\")
        for msg in file_report.get("messages", []):
            issues.append({
                "file": path,
                "line": msg.get("line", 0),
                "col": msg.get("column", 0),
                "code": msg.get("ruleId") or "",
                "message": (msg.get("message") or "")[:200],
            })
            if len(issues) >= _MAX_ISSUES:
                break
        if len(issues) >= _MAX_ISSUES:
            break
    return {"issues": issues, "total": len(issues), "truncated": False}


# ============================================================
# 工具 2:run_coverage
# ============================================================


def run_coverage(repo_path: str, task_id: str = "") -> dict:
    """跑测试并解析覆盖率,返回总覆盖率 + 未覆盖行数 top 文件

    Python 项目走 pytest --cov(需 pytest-cov);检测到 vitest 的 JS 项目尝试 vitest。
    返回:{
        "framework": "pytest-cov",
        "totals": {"coverage_percent", "covered_lines", "missing_lines"},
        "uncovered_files": [{"file", "uncovered_lines", "coverage_percent"}],  # top20
        "total_files": int,
    }
    工具不可用/测试跑不起来时返回 {"note"/"error": ...}。
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]
    exists = _make_exists(ctx)
    read = _make_read(ctx)

    # 项目类型探测:python 优先;否则看 package.json 有无 vitest
    if _detect_python_project(repo_path, exists):
        if mode == "local":
            return _run_pytest_cov_local(repo_path, read)
        return _run_pytest_cov_sandbox(ctx, repo_path, read)

    pkg = _read_package_json(repo_path, read)
    if pkg is not None:
        dev_deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        if "vitest" in dev_deps:
            return _run_vitest_sandbox(ctx, repo_path) if mode != "local" else {
                "note": "local 模式暂不支持 vitest 覆盖率解析,"
                        "可用 run_command 执行 `npx vitest run --coverage` 自行查看输出。"
            }
        return {"note": "package.json 未检测到 vitest,暂不支持该测试框架的覆盖率解析;"
                        "可用 run_command 手动执行测试命令查看输出。"}

    return {"note": "未探测到 Python/JS 项目,无法运行覆盖率。"
                    "可用 run_command 手动执行项目的测试命令。"}


def _run_pytest_cov_local(repo_path: str, read) -> dict:
    """local 模式:检测宿主机 pytest,有则执行,无则 note"""
    pytest_bin = shutil.which("pytest")
    if not pytest_bin:
        return {"note": (
            "local 模式未检测到 pytest。可执行 `pip install pytest pytest-cov` 安装后重试,"
            "或切换 SANDBOX_MODE=sandbox(沙箱会自动安装)。"
        )}
    cov_file = Path(repo_path) / ".agent_coverage.json"
    cmd = [
        pytest_bin, "--cov", f"--cov-report=json:{cov_file}", "-q", "--no-header",
    ]
    logger.info(f"[local] pytest --cov: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True,
            timeout=_COVERAGE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"pytest 执行超时({_COVERAGE_TIMEOUT}s)"}
    text = None
    if cov_file.is_file():
        text = cov_file.read_text(encoding="utf-8", errors="replace")
        try:
            cov_file.unlink()
        except OSError:
            pass
    if text is None:
        # pytest 失败/无测试/缺 pytest-cov 都不会产出 coverage.json
        tail = (result.stdout + "\n" + result.stderr).strip()[-300:]
        return {"error": f"未生成覆盖率报告(退出码 {result.returncode})。输出尾部: {tail}"}
    return {**parse_coverage_json(text), "framework": "pytest-cov"}


def _run_pytest_cov_sandbox(ctx: dict, repo_path: str, read) -> dict:
    """sandbox 模式:缺 pytest/pytest-cov 时自动安装后执行"""
    session: SandboxSession = ctx["session"]
    check = session.run_command(_PATH_PREFIX + "command -v pytest || echo MISSING")
    if "MISSING" in check:
        logger.info("[sandbox] pytest 未安装,尝试 pip install pytest pytest-cov")
        session.run_command(
            "pip install --user --break-system-packages pytest pytest-cov 2>&1 | tail -3",
            timeout=_COVERAGE_TIMEOUT,
        )
    cov_rel = ".agent_coverage.json"
    cmd = (
        _PATH_PREFIX
        + f"cd {shlex.quote(repo_path)} && "
        + f"pytest --cov --cov-report=json:{cov_rel} -q --no-header 2>&1 | tail -15"
    )
    logger.info(f"[sandbox] pytest --cov: {cmd}")
    output = session.run_command(cmd, timeout=_COVERAGE_TIMEOUT)
    text = read(_join(repo_path, cov_rel))
    if text is None:
        return {"error": f"未生成覆盖率报告(测试失败/无测试/缺 pytest-cov)。"
                         f"输出尾部: {output.strip()[-300:]}"}
    return {**parse_coverage_json(text), "framework": "pytest-cov"}


def _run_vitest_sandbox(ctx: dict, repo_path: str) -> dict:
    """sandbox 模式: vitest run --coverage(依赖仓库自带 vitest 与 coverage provider)"""
    session: SandboxSession = ctx["session"]
    cmd = (
        f"cd {shlex.quote(repo_path)} && "
        "(npx --no-install vitest run --coverage --coverage.reporter=json "
        "--coverage.reportsDirectory=.agent_coverage 2>&1 | tail -15 "
        "|| echo VITEST_FAILED)"
    )
    logger.info(f"[sandbox] vitest coverage: {cmd}")
    output = session.run_command(cmd, timeout=_COVERAGE_TIMEOUT)
    # vitest istanbul-json 报告路径:coverage/coverage-final.json 或自定义目录
    read_fn = _make_read(ctx)
    text = None
    for rel in (".agent_coverage/coverage-final.json", "coverage/coverage-final.json"):
        text = read_fn(_join(repo_path, rel))
        if text is not None:
            break
    if text is None or "VITEST_FAILED" in output:
        return {"note": f"vitest 覆盖率执行失败(可能缺 @vitest/coverage-v8 依赖)。"
                        f"输出尾部: {output.strip()[-300:]}"}
    return {**_parse_istanbul_json(text), "framework": "vitest"}


def _parse_istanbul_json(text: str) -> dict:
    """解析 istanbul coverage-final.json(vitest/jest 共用格式)"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return {"error": f"coverage-final.json 解析失败: {e}"}
    total_covered = 0
    total_missing = 0
    uncovered = []
    for path, info in data.items():
        stmt_map = info.get("statementMap") or {}
        counts = info.get("s") or {}
        missing = [k for k, v in counts.items() if v == 0 and k in stmt_map]
        covered = len(counts) - len(missing)
        total_covered += covered
        total_missing += len(missing)
        if missing:
            pct = 100.0 * covered / len(counts) if counts else 0.0
            uncovered.append({
                "file": path,
                "uncovered_lines": len(missing),
                "coverage_percent": round(pct, 2),
            })
    uncovered.sort(key=lambda x: x["uncovered_lines"], reverse=True)
    total = total_covered + total_missing
    return {
        "totals": {
            "coverage_percent": round(100.0 * total_covered / total, 2) if total else 0.0,
            "covered_lines": total_covered,
            "missing_lines": total_missing,
        },
        "uncovered_files": uncovered[:_MAX_UNCOVERED_FILES],
        "total_files": len(data),
    }
