"""run_lint / run_coverage 测试(纯解析器单测 + local 模式降级行为)"""
import json
import uuid

import pytest

from app.config import settings
from app.tools import quality_tools, sandbox_tools


@pytest.fixture
def local_mode(monkeypatch):
    """强制 local 模式,避免连真实沙箱。"""
    monkeypatch.setattr(settings, "SANDBOX_MODE", "local")
    yield


@pytest.fixture
def no_tools(monkeypatch):
    """宿主机无 ruff/pytest/npx:验证降级 note 而非报错"""
    monkeypatch.setattr(quality_tools.shutil, "which", lambda *a, **k: None)
    yield


@pytest.fixture
def task_id():
    return f"test-quality-{uuid.uuid4()}"


def _cleanup(tid: str) -> None:
    try:
        sandbox_tools.close_session(tid)
    except Exception:
        pass


# ---------- parse_ruff_json ----------

def test_parse_ruff_json_basic():
    output = json.dumps([
        {
            "code": "E501",
            "message": "line too long",
            "filename": "/repo/src/a.py",
            "location": {"row": 10, "column": 81},
        },
        {
            "code": "F401",
            "message": "imported but unused",
            "filename": "/repo/src/b.py",
            "location": {"row": 1, "column": 1},
        },
    ])
    result = quality_tools.parse_ruff_json(output, "/repo")
    assert result["total"] == 2
    assert result["truncated"] is False
    assert result["issues"][0] == {
        "file": "src/a.py", "line": 10, "col": 81,
        "code": "E501", "message": "line too long",
    }


def test_parse_ruff_json_invalid():
    result = quality_tools.parse_ruff_json("not json", "/repo")
    assert result["error"]
    assert result["issues"] == []


def test_parse_ruff_json_truncates_over_limit():
    output = json.dumps([
        {"code": "E501", "message": "x", "filename": "f.py",
         "location": {"row": i, "column": 1}}
        for i in range(quality_tools._MAX_ISSUES + 5)
    ])
    result = quality_tools.parse_ruff_json(output, "/repo")
    assert len(result["issues"]) == quality_tools._MAX_ISSUES
    assert result["truncated"] is True
    assert result["total"] == quality_tools._MAX_ISSUES + 5


# ---------- parse_coverage_json ----------

def test_parse_coverage_json_totals_and_ranking():
    data = {
        "totals": {"percent_covered": 62.5, "covered_lines": 50, "missing_lines": 30},
        "files": {
            "src/a.py": {"summary": {"percent_covered": 20.0},
                         "missing_lines": [10, 11, 12, 13]},
            "src/b.py": {"summary": {"percent_covered": 90.0}, "missing_lines": [5]},
            "src/c.py": {"summary": {"percent_covered": 100.0}, "missing_lines": []},
        },
    }
    result = quality_tools.parse_coverage_json(json.dumps(data))
    assert result["totals"]["coverage_percent"] == 62.5
    assert result["total_files"] == 3
    # 按未覆盖行数降序,全覆盖文件不进清单
    assert result["uncovered_files"][0]["file"] == "src/a.py"
    assert result["uncovered_files"][0]["uncovered_lines"] == 4
    assert result["uncovered_files"][1]["file"] == "src/b.py"
    assert len(result["uncovered_files"]) == 2


def test_parse_coverage_json_invalid():
    result = quality_tools.parse_coverage_json("{bad json")
    assert result["error"]


# ---------- _parse_istanbul_json(vitest/jest 格式) ----------

def test_parse_istanbul_json_statement_counts():
    data = {
        "src/a.ts": {
            "statementMap": {"0": {}, "1": {}, "2": {}},
            "s": {"0": 3, "1": 0, "2": 0},
        },
        "src/b.ts": {
            "statementMap": {"0": {}},
            "s": {"0": 1},
        },
    }
    result = quality_tools._parse_istanbul_json(json.dumps(data))
    assert result["totals"]["covered_lines"] == 2
    assert result["totals"]["missing_lines"] == 2
    assert result["totals"]["coverage_percent"] == 50.0
    assert result["uncovered_files"] == [
        {"file": "src/a.ts", "uncovered_lines": 2, "coverage_percent": 33.33},
    ]


# ---------- local 模式降级行为 ----------

def test_run_lint_local_missing_ruff_returns_note(local_mode, no_tools, tmp_path, task_id):
    """local 模式宿主机无 ruff → 返回 note 指引(不抛异常)"""
    repo = tmp_path / "pyrepo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("flask==2.0.1\n", encoding="utf-8")
    try:
        result = quality_tools.run_lint(str(repo), task_id=task_id)
        assert "note" in result
        assert "ruff" in result["note"]
    finally:
        _cleanup(task_id)


def test_run_lint_auto_detect_unknown_project_note(local_mode, tmp_path, task_id):
    """无清单文件的目录 → 探测不到语言,返回 note"""
    repo = tmp_path / "bare"
    repo.mkdir()
    try:
        result = quality_tools.run_lint(str(repo), task_id=task_id)
        assert "note" in result
    finally:
        _cleanup(task_id)


def test_run_lint_js_without_eslint_config_note(local_mode, tmp_path, task_id):
    """JS 项目无 eslint 配置 → note 引导用 run_semgrep"""
    repo = tmp_path / "jsrepo"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps({"dependencies": {"vue": "^3.0.0"}}), encoding="utf-8",
    )
    try:
        result = quality_tools.run_lint(str(repo), language="javascript", task_id=task_id)
        assert "note" in result
        assert "run_semgrep" in result["note"]
    finally:
        _cleanup(task_id)


def test_run_coverage_local_missing_pytest_returns_note(local_mode, no_tools, tmp_path, task_id):
    """local 模式宿主机无 pytest → 返回 note 指引"""
    repo = tmp_path / "pyrepo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("flask==2.0.1\n", encoding="utf-8")
    try:
        result = quality_tools.run_coverage(str(repo), task_id=task_id)
        assert "note" in result
        assert "pytest" in result["note"]
    finally:
        _cleanup(task_id)


def test_run_coverage_js_without_vitest_note(local_mode, tmp_path, task_id):
    """package.json 无 vitest → note"""
    repo = tmp_path / "jsrepo"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps({"devDependencies": {"jest": "^29.0.0"}}), encoding="utf-8",
    )
    try:
        result = quality_tools.run_coverage(str(repo), task_id=task_id)
        assert "note" in result
    finally:
        _cleanup(task_id)
