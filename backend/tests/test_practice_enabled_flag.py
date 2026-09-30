"""练习功能总开关 PRACTICE_ENABLED 测试

覆盖:
- 默认开启:/health 报告 features.practice_enabled=true,/practice/* 已注册(未登录 401)
- PRACTICE_ENABLED=false 重启:/practice/* 不注册(404),/health 报告 false
- 重新开启后路由恢复(模拟部署时改 env 重启)

开关在 app.main 导入期决定路由注册,故用 env + reload(config/health/main)重建 app 模拟重启;
无其他测试依赖 app.main,reload 不产生跨用例副作用(fixture 结束时恢复默认态)。
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def restore_main(monkeypatch):
    """用例结束后把开关恢复为默认(true)并重建 app,避免污染后续用例"""
    yield
    monkeypatch.delenv("PRACTICE_ENABLED", raising=False)
    _reload_app()


def _reload_app():
    """按依赖顺序 reload:config → health(读 settings)→ main(注册路由)"""
    import app.config
    import app.routers.health
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.routers.health)
    importlib.reload(app.main)
    return app.main.app


def test_default_enabled(monkeypatch, restore_main):
    monkeypatch.delenv("PRACTICE_ENABLED", raising=False)
    app = _reload_app()
    client = TestClient(app)

    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["features"]["practice_enabled"] is True
    # 路由已注册:未登录 401(而非 404)
    assert client.get("/practice/stats").status_code == 401


def test_disabled_env_hides_routes(monkeypatch, restore_main):
    monkeypatch.setenv("PRACTICE_ENABLED", "false")
    app = _reload_app()
    client = TestClient(app)

    body = client.get("/health").json()
    assert body["features"]["practice_enabled"] is False
    # 路由不注册:一律 404
    assert client.get("/practice/stats").status_code == 404
    assert client.get("/practice/summary").status_code == 404
    assert client.post("/practice/generate", json={}).status_code == 404
    # 其余功能不受影响
    assert body["status"] == "ok"


def test_reenable_restores_routes(monkeypatch, restore_main):
    monkeypatch.setenv("PRACTICE_ENABLED", "false")
    _reload_app()
    monkeypatch.setenv("PRACTICE_ENABLED", "true")
    app = _reload_app()
    client = TestClient(app)

    assert client.get("/health").json()["features"]["practice_enabled"] is True
    assert client.get("/practice/stats").status_code == 401
