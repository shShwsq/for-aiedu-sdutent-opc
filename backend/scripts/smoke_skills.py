"""skills 上传/鉴权/隔离 端到端冒烟脚本(连本地 8000 端口后端)

流程:
1. 匿名 GET /skills → 仅内置 3 个,owned=false
2. 注册+登录两个测试用户(alice/bob)
3. alice 上传 zip(标准结构) → 成功,owned=true
4. alice 重复上传(force=false) → 409
5. alice 重复上传(force=true) → replaced=true
6. bob GET /skills → 看不到 alice 的私有 skill
7. bob 尝试删除 alice 的 skill → 403
8. alice 尝试删除内置 skill → 403
9. alice 删除自己的 skill → 204
10. 匿名上传 → 401
"""
import io
import sys
import uuid
import zipfile
from pathlib import Path

import requests

# 脚本在 backend/scripts/ 下,把 backend 根加入 sys.path 以便 import app.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.email_service import create_email_verification_token
from app.models.user import User
import app.models.user_git_binding  # noqa: F401  注册关系模型,避免 mapper 解析失败

BASE = "http://127.0.0.1:8000"
FAILURES = []


def check(name: str, cond: bool, extra: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {extra}")
    if not cond:
        FAILURES.append(name)


def register_and_login(email: str) -> str:
    # 注册(重复注册会 409,直接登录兜底)
    r = requests.post(f"{BASE}/auth/register", json={
        "email": email, "password": "TestPass123!",
    })
    if r.status_code not in (200, 201, 409):
        raise RuntimeError(f"register failed: {r.status_code} {r.text}")

    # 测试环境直接生成验证 token 完成邮箱验证(绕过真实邮件)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user and not user.is_email_verified:
            token = create_email_verification_token(db, user.id)
            db.commit()
            r = requests.post(f"{BASE}/auth/verify-email", json={"token": token})
            if r.status_code not in (200, 400):
                raise RuntimeError(f"verify failed: {r.status_code} {r.text}")
    finally:
        db.close()

    r = requests.post(f"{BASE}/auth/login", json={
        "email": email, "password": "TestPass123!",
    })
    if r.status_code != 200:
        raise RuntimeError(f"login failed: {r.status_code} {r.text}")
    return r.json()["access_token"]


def make_zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


SKILL_MD = """---
name: smoke_test_skill
description: 冒烟测试技能
---

# 指令
执行 xxx 检查。
"""


def main():
    # 1. 匿名列表
    r = requests.get(f"{BASE}/skills")
    check("匿名可见内置 skill", r.status_code == 200, f"({r.status_code})")
    names = [s["name"] for s in r.json()]
    check("内置 3 个 skill", len(names) == 3 and "check_sql_injection" in names, str(names))
    check("匿名 owned=false", all(not s["owned"] for s in r.json()))

    # 2. 登录
    suffix = uuid.uuid4().hex[:8]
    alice_token = register_and_login(f"alice_{suffix}@example.com")
    bob_token = register_and_login(f"bob_{suffix}@example.com")
    alice_h = {"Authorization": f"Bearer {alice_token}"}
    bob_h = {"Authorization": f"Bearer {bob_token}"}

    # 3. 上传(标准结构)
    zip_bytes = make_zip({
        "smoke_test_skill/SKILL.md": SKILL_MD,
        "smoke_test_skill/rules.txt": "rule1\n",
    })
    r = requests.post(
        f"{BASE}/skills/upload",
        headers=alice_h,
        files={"file": ("skill.zip", zip_bytes, "application/zip")},
    )
    check("alice 上传成功", r.status_code == 200, f"({r.status_code}) {r.text[:200]}")
    body = r.json()
    check("上传 returned replaced=false", body.get("replaced") is False)
    check("上传 owned=true", body["skill"]["owned"] is True)

    # 4. 重复上传 → 409
    r = requests.post(
        f"{BASE}/skills/upload",
        headers=alice_h,
        files={"file": ("skill.zip", zip_bytes, "application/zip")},
    )
    check("重复上传 409", r.status_code == 409, f"({r.status_code}) {r.text[:120]}")

    # 5. force 覆盖 → replaced=true
    r = requests.post(
        f"{BASE}/skills/upload",
        headers=alice_h,
        data={"force": "true"},
        files={"file": ("skill.zip", zip_bytes, "application/zip")},
    )
    check("force 覆盖成功", r.status_code == 200 and r.json()["replaced"] is True,
          f"({r.status_code}) {r.text[:200]}")

    # 6. bob 看不到 alice 的私有 skill
    r = requests.get(f"{BASE}/skills", headers=bob_h)
    bob_names = [s["name"] for s in r.json()]
    check("bob 看不到 alice 的 skill", "smoke_test_skill" not in bob_names, str(bob_names))

    # 7. bob 删除 alice 的 skill → 403
    scenario = body["skill"]["scenario_id"]
    r = requests.delete(f"{BASE}/skills/{scenario}/smoke_test_skill", headers=bob_h)
    check("bob 删除他人 skill 403", r.status_code == 403, f"({r.status_code})")

    # 8. alice 删除内置 skill → 403
    r = requests.delete(f"{BASE}/skills/code_security_audit/check_ssrf", headers=alice_h)
    check("删除内置 skill 403", r.status_code == 403, f"({r.status_code})")

    # 9. alice 删除自己的 skill → 204
    r = requests.delete(f"{BASE}/skills/{scenario}/smoke_test_skill", headers=alice_h)
    check("alice 删除自己的 skill 204", r.status_code == 204, f"({r.status_code})")

    # 10. 匿名上传 → 401
    r = requests.post(
        f"{BASE}/skills/upload",
        files={"file": ("skill.zip", zip_bytes, "application/zip")},
    )
    check("匿名上传 401", r.status_code == 401, f"({r.status_code})")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        raise SystemExit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
