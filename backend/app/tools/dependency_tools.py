"""依赖清单解析工具(list_dependencies)

扫描仓库常见清单文件(manifest),返回结构化依赖清单,
供 react_agent 串联 query_cve 批量查已知漏洞,省去逐个 read_file 解析清单的迭代。

设计要点:
- 解析器全部是纯函数(输入文件内容文本,输出依赖条目),便于单测
- 文件发现/读取分 local/sandbox 双模式(复用 sandbox_tools 的 session)
- 版本语义:能精确解析出锁定版本(==、lockfile)时填 version;
  仅有范围约束(>=、^1.2)时 version 留空、constraint 保留原始约束串,
  LLM 据此判断能否直接喂给 query_cve(它需要精确版本)
"""
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib  # Python 3.11+ 标准库
except ImportError:  # 低版本降级为正则兜底解析
    tomllib = None  # type: ignore[assignment]

from app.sandbox.client import SandboxSession
from app.tools.sandbox_tools import _get_or_create_session

logger = logging.getLogger(__name__)


# 依赖条目总数上限(防巨型 monorepo 的 lockfile 冲爆上下文)
_MAX_DEPS = 300
# package-lock.json 解析条目上限(巨型锁文件只取前 N 个)
_LOCK_MAX_ENTRIES = 200

# 清单文件名 → (ecosystem, 解析器)。requirements*.txt 按前缀通配单独处理。
_MANIFEST_PARSERS: dict[str, tuple[str, Callable[[str], list[dict]]]] = {}

# OSV 生态名(query_cve 的 ecosystem 参数直接可用)
_ECOSYSTEM_PYPI = "PyPI"
_ECOSYSTEM_NPM = "npm"
_ECOSYSTEM_GO = "Go"
_ECOSYSTEM_MAVEN = "Maven"


# ============================================================
# 纯函数解析器(输入文件内容,输出 [{name, version, constraint}])
# ============================================================

# PEP 508 依赖串:name [extras] 操作符 版本(; 后是环境标记,忽略)
_PEP508_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*"
    r"(===|==|>=|<=|~=|!=|>|<)?\s*([A-Za-z0-9.*+!_-]*)"
)


def _split_pep508(spec: str) -> dict | None:
    """解析单个 PEP 508 依赖串为 {name, version, constraint};非法返回 None"""
    spec = spec.split(";", 1)[0].strip()  # 去掉环境标记
    if not spec or spec.startswith(("-", "--", "#")):
        return None
    m = _PEP508_RE.match(spec)
    if not m:
        return None
    name, _extras, op, ver = m.groups()
    if op == "==":
        return {"name": name, "version": ver, "constraint": ""}
    constraint = f"{op}{ver}" if op else ""
    return {"name": name, "version": "", "constraint": constraint}


def parse_requirements(text: str) -> list[dict]:
    """解析 requirements*.txt(逐行 PEP 508;选项行 -r/--index-url 等跳过)"""
    deps: list[dict] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        dep = _split_pep508(line)
        if dep:
            deps.append(dep)
    return deps


def parse_pyproject(text: str) -> list[dict]:
    """解析 pyproject.toml 的 [project].dependencies

    优先 tomllib 结构化解析;不可用/解析失败时正则兜底(只提取 dependencies 数组里的串)。
    """
    dep_strs: list[str] = []
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
            project = data.get("project") or {}
            raw_deps = project.get("dependencies") or []
            dep_strs = [d for d in raw_deps if isinstance(d, str)]
        except Exception as e:
            logger.debug(f"pyproject.toml tomllib 解析失败,降级正则: {e}")
    if not dep_strs:
        # 兜底:dependencies = [ "a==1.0", "b>=2" ] 数组里的引号串
        m = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL)
        if m:
            dep_strs = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    deps = []
    for s in dep_strs:
        dep = _split_pep508(s)
        if dep:
            deps.append(dep)
    return deps


def parse_package_json(text: str) -> list[dict]:
    """解析 package.json 的 dependencies + devDependencies

    npm 的 version 字段本身就是约束串(^1.2.3/~2.0/精确值),统一放 constraint;
    无操作符前缀的视为精确版本放 version。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    deps: list[dict] = []
    for key in ("dependencies", "devDependencies"):
        section = data.get(key) or {}
        if not isinstance(section, dict):
            continue
        for name, spec in section.items():
            if not isinstance(spec, str):
                continue
            spec = spec.strip()
            if spec and spec[0] not in "^~><=*.|":
                deps.append({"name": name, "version": spec, "constraint": ""})
            else:
                deps.append({"name": name, "version": "", "constraint": spec})
    return deps


def parse_package_lock(text: str) -> list[dict]:
    """解析 package-lock.json(优先 v3 的 packages,兜底 v1 的 dependencies)"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    deps: list[dict] = []
    packages = data.get("packages")
    if isinstance(packages, dict):
        # v2/v3:键为 node_modules/<name>(嵌套依赖键含多段 node_modules,只取顶层依赖)
        for key, info in packages.items():
            if not key.startswith("node_modules/"):
                continue
            tail = key[len("node_modules/"):]
            if "node_modules/" in tail:
                continue  # 嵌套依赖,跳过避免重复
            version = (info or {}).get("version", "")
            deps.append({"name": tail, "version": version, "constraint": ""})
            if len(deps) >= _LOCK_MAX_ENTRIES:
                break
        return deps
    # v1 兜底
    legacy = data.get("dependencies")
    if isinstance(legacy, dict):
        for name, info in list(legacy.items())[:_LOCK_MAX_ENTRIES]:
            version = (info or {}).get("version", "")
            deps.append({"name": name, "version": version, "constraint": ""})
    return deps


def parse_go_mod(text: str) -> list[dict]:
    """解析 go.mod 的 require(块内多行 + 单行 require 两种形态)"""
    deps: list[dict] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        candidate = ""
        if in_block:
            candidate = line
        elif line.startswith("require "):
            candidate = line[len("require "):].strip()
        if not candidate:
            continue
        parts = candidate.split()
        if len(parts) >= 2 and parts[1].startswith("v"):
            deps.append({"name": parts[0], "version": parts[1], "constraint": ""})
    return deps


def parse_pom_xml(text: str) -> list[dict]:
    """解析 pom.xml 的 <dependency> 块(正则提取,不引入 XML 库)

    version 含 ${...} 属性占位符时无法确定具体版本,放 constraint 提示。
    """
    deps: list[dict] = []
    for block in re.findall(r"<dependency>(.*?)</dependency>", text, re.DOTALL):
        gid = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", block)
        aid = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", block)
        ver = re.search(r"<version>\s*([^<]+?)\s*</version>", block)
        if not aid:
            continue
        name = f"{gid.group(1)}:{aid.group(1)}" if gid else aid.group(1)
        version = ver.group(1) if ver else ""
        if "${" in version:
            deps.append({"name": name, "version": "", "constraint": version})
        else:
            deps.append({"name": name, "version": version, "constraint": ""})
    return deps


_MANIFEST_PARSERS = {
    "pyproject.toml": (_ECOSYSTEM_PYPI, parse_pyproject),
    "package.json": (_ECOSYSTEM_NPM, parse_package_json),
    "package-lock.json": (_ECOSYSTEM_NPM, parse_package_lock),
    "go.mod": (_ECOSYSTEM_GO, parse_go_mod),
    "pom.xml": (_ECOSYSTEM_MAVEN, parse_pom_xml),
}


# ============================================================
# 文件发现与读取(local / sandbox 双模式)
# ============================================================


def _discover_manifests(ctx: dict, repo_path: str) -> list[str]:
    """列出仓库根的清单文件(相对路径列表)

    local:直接 os 列目录;sandbox:ls -1 一次拉根目录清单。
    requirements*.txt 按前缀通配,其余按固定文件名匹配。
    """
    names: list[str] = []
    if ctx["mode"] == "local":
        root = Path(repo_path)
        if not root.is_dir():
            return []
        try:
            names = [p.name for p in root.iterdir() if p.is_file()]
        except OSError:
            return []
    else:
        session: SandboxSession = ctx["session"]
        try:
            out = session.run_command(f"ls -1 {shlex.quote(repo_path)}")
            names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        except Exception as e:
            logger.warning(f"[list_dependencies] ls 失败: {e}")
            return []

    found: list[str] = []
    for n in names:
        lower = n.lower()
        if lower.startswith("requirements") and lower.endswith(".txt"):
            found.append(n)
        elif n in _MANIFEST_PARSERS:
            found.append(n)
    return sorted(found)


def _read_manifest(ctx: dict, repo_path: str, rel_path: str) -> str | None:
    """读取清单文件内容;失败返回 None(不阻断其他清单解析)"""
    abs_path = f"{repo_path.rstrip('/')}/{rel_path}"
    try:
        if ctx["mode"] == "local":
            return Path(abs_path).read_text(encoding="utf-8", errors="replace")
        session: SandboxSession = ctx["session"]
        return session.read_file(abs_path)
    except Exception as e:
        logger.warning(f"[list_dependencies] 读取 {rel_path} 失败: {e}")
        return None


# ============================================================
# 工具入口:list_dependencies
# ============================================================


def list_dependencies(repo_path: str, task_id: str = "") -> dict:
    """扫描仓库清单文件,返回结构化依赖清单(供 query_cve 批量查漏洞)

    支持清单:requirements*.txt / pyproject.toml / package.json /
    package-lock.json / go.mod / pom.xml(仅仓库根目录)。

    返回:{
        "manifests": [{"file": "requirements.txt", "ecosystem": "PyPI", "count": 12}],
        "dependencies": [{"ecosystem": "PyPI", "name": "flask", "version": "2.0.1",
                          "constraint": "", "source": "requirements.txt"}],
        "total": int,
        "truncated": bool,   # 依赖数超过上限被截断
        "hint": str          # 使用指引(version 为空时如何处理)
    }
    """
    ctx = _get_or_create_session(task_id)

    manifest_files = _discover_manifests(ctx, repo_path)
    manifests: list[dict] = []
    dependencies: list[dict[str, Any]] = []
    truncated = False

    for rel in manifest_files:
        if rel.lower().startswith("requirements"):
            ecosystem, parser = _ECOSYSTEM_PYPI, parse_requirements
        else:
            ecosystem, parser = _MANIFEST_PARSERS[rel]
        text = _read_manifest(ctx, repo_path, rel)
        if text is None:
            continue
        try:
            parsed = parser(text)
        except Exception as e:
            logger.warning(f"[list_dependencies] 解析 {rel} 失败: {e}")
            continue
        manifests.append({"file": rel, "ecosystem": ecosystem, "count": len(parsed)})
        for dep in parsed:
            if len(dependencies) >= _MAX_DEPS:
                truncated = True
                break
            dependencies.append({**dep, "ecosystem": ecosystem, "source": rel})
        if truncated:
            break

    return {
        "manifests": manifests,
        "dependencies": dependencies,
        "total": len(dependencies),
        "truncated": truncated,
        "hint": (
            "version 非空的依赖可直接用 query_cve(package_name, version, ecosystem) 查漏洞;"
            "version 为空的是范围约束(constraint),需先从 lockfile 或运行环境解析精确版本。"
            "package-lock.json/go.mod 的版本即锁定版本,优先按它们查。"
        ),
    }
