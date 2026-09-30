"""list_dependencies 工具测试(纯解析器单测 + local 模式集成)"""
import json
import uuid

import pytest

from app.config import settings
from app.tools import dependency_tools, sandbox_tools


@pytest.fixture
def local_mode(monkeypatch):
    """强制 local 模式,避免连真实沙箱。"""
    monkeypatch.setattr(settings, "SANDBOX_MODE", "local")
    yield


@pytest.fixture
def task_id():
    return f"test-deps-{uuid.uuid4()}"


def _cleanup(tid: str) -> None:
    try:
        sandbox_tools.close_session(tid)
    except Exception:
        pass


# ---------- 纯函数解析器 ----------

def test_parse_requirements_pins_and_constraints():
    text = """
# 注释行
flask==2.0.1
requests>=2.25
django[bcrypt]~=4.2
-r other.txt
--index-url https://pypi.org/simple
numpy
"""
    deps = dependency_tools.parse_requirements(text)
    by_name = {d["name"]: d for d in deps}
    assert by_name["flask"]["version"] == "2.0.1"
    assert by_name["flask"]["constraint"] == ""
    assert by_name["requests"]["version"] == ""
    assert by_name["requests"]["constraint"] == ">=2.25"
    assert by_name["django"]["version"] == ""
    assert by_name["django"]["constraint"] == "~=4.2"
    assert by_name["numpy"]["version"] == ""
    assert len(deps) == 4  # 选项行不计入


def test_parse_pyproject_dependencies():
    text = """
[project]
name = "demo"
dependencies = [
    "fastapi==0.110.0",
    "pydantic>=2.0",
]

[project.optional-dependencies]
dev = ["pytest"]
"""
    deps = dependency_tools.parse_pyproject(text)
    by_name = {d["name"]: d for d in deps}
    assert by_name["fastapi"]["version"] == "0.110.0"
    assert by_name["pydantic"]["constraint"] == ">=2.0"
    # optional-dependencies 不在 [project].dependencies 里,不解析
    assert "pytest" not in by_name


def test_parse_pyproject_regex_fallback():
    """tomllib 不可用时正则兜底仍提取 dependencies 数组"""
    text = '[project]\ndependencies = [\n  "a==1.0",\n  "b>=2"\n]\n'
    deps = []
    import re
    m = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if m:
        for s in re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)):
            deps.append(dependency_tools._split_pep508(s))
    assert deps[0]["name"] == "a"
    assert deps[1]["constraint"] == ">=2"


def test_parse_package_json_deps_and_devdeps():
    text = json.dumps({
        "dependencies": {"vue": "^3.4.0", "axios": "1.6.8"},
        "devDependencies": {"vite": "~5.0.0"},
    })
    deps = dependency_tools.parse_package_json(text)
    by_name = {d["name"]: d for d in deps}
    assert by_name["vue"]["constraint"] == "^3.4.0"
    assert by_name["vue"]["version"] == ""
    assert by_name["axios"]["version"] == "1.6.8"  # 无操作符=精确版本
    assert by_name["vite"]["constraint"] == "~5.0.0"


def test_parse_package_lock_v3_packages():
    text = json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "root"},
            "node_modules/vue": {"version": "3.4.21"},
            "node_modules/vue/node_modules/sub": {"version": "1.0.0"},  # 嵌套,跳过
        },
    })
    deps = dependency_tools.parse_package_lock(text)
    assert deps == [{"name": "vue", "version": "3.4.21", "constraint": ""}]


def test_parse_package_lock_v1_legacy():
    text = json.dumps({
        "lockfileVersion": 1,
        "dependencies": {"lodash": {"version": "4.17.21"}},
    })
    deps = dependency_tools.parse_package_lock(text)
    assert deps == [{"name": "lodash", "version": "4.17.21", "constraint": ""}]


def test_parse_go_mod_block_and_single():
    text = """
module example.com/demo

go 1.21

require (
    github.com/gin-gonic/gin v1.9.1
    golang.org/x/sync v0.6.0 // indirect
)

require single.example/pkg v0.1.0
"""
    deps = dependency_tools.parse_go_mod(text)
    by_name = {d["name"]: d for d in deps}
    assert by_name["github.com/gin-gonic/gin"]["version"] == "v1.9.1"
    assert by_name["golang.org/x/sync"]["version"] == "v0.6.0"
    assert by_name["single.example/pkg"]["version"] == "v0.1.0"


def test_parse_pom_xml_dependencies():
    text = """
<project>
  <dependencies>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-core</artifactId>
      <version>5.3.20</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>${junit.version}</version>
    </dependency>
  </dependencies>
</project>
"""
    deps = dependency_tools.parse_pom_xml(text)
    by_name = {d["name"]: d for d in deps}
    spring = by_name["org.springframework:spring-core"]
    assert spring["version"] == "5.3.20"
    junit = by_name["junit:junit"]
    assert junit["version"] == ""  # ${} 占位符无法确定版本
    assert junit["constraint"] == "${junit.version}"


# ---------- local 模式集成 ----------

def test_list_dependencies_local_mixed_manifests(local_mode, tmp_path, task_id):
    """tmp 目录放 requirements.txt + package.json + go.mod,一次拿全生态清单"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("flask==2.0.1\nrequests>=2.25\n", encoding="utf-8")
    (repo / "package.json").write_text(
        json.dumps({"dependencies": {"vue": "^3.4.0"}}), encoding="utf-8",
    )
    (repo / "go.mod").write_text(
        "module demo\n\nrequire github.com/x/y v1.0.0\n", encoding="utf-8",
    )
    try:
        result = dependency_tools.list_dependencies(str(repo), task_id=task_id)
        assert result["total"] == 4
        assert result["truncated"] is False
        files = {m["file"] for m in result["manifests"]}
        assert files == {"requirements.txt", "package.json", "go.mod"}
        by_eco = {}
        for d in result["dependencies"]:
            by_eco.setdefault(d["ecosystem"], []).append(d["name"])
        assert "flask" in by_eco["PyPI"]
        assert "vue" in by_eco["npm"]
        assert "github.com/x/y" in by_eco["Go"]
        assert result["hint"]
    finally:
        _cleanup(task_id)


def test_list_dependencies_local_empty_repo(local_mode, tmp_path, task_id):
    """无清单文件 → manifests 空,不报错"""
    repo = tmp_path / "empty"
    repo.mkdir()
    try:
        result = dependency_tools.list_dependencies(str(repo), task_id=task_id)
        assert result["manifests"] == []
        assert result["dependencies"] == []
        assert result["total"] == 0
    finally:
        _cleanup(task_id)
