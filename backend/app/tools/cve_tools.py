"""CVE 查询工具(阶段 3)

使用 OSV(Open Source Vulnerabilities)API,免费、无需 key。
- POST https://api.osv.dev/v1/query
- body: {"package": {"name": "flask", "ecosystem": "PyPI"}, "version": "2.0.0"}
- 返回该版本已知漏洞列表

参考:https://google.github.io/osv.dev/api/
"""
import json
import logging
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


OSV_QUERY_URL = "https://api.osv.dev/v1/query"

# OSV 支持的 ecosystem 映射
# https://ossf.github.io/osv-schema/#affectedpackage-field
SUPPORTED_ECOSYSTEMS = {
    "python": "PyPI",
    "pypi": "PyPI",
    "npm": "npm",
    "javascript": "npm",
    "node": "npm",
    "go": "Go",
    "golang": "Go",
    "java": "Maven",
    "maven": "Maven",
    "php": "Packagist",
    "composer": "Packagist",
    "ruby": "RubyGems",
    "rubygems": "RubyGems",
    "rust": "crates.io",
    "cargo": "crates.io",
    "csharp": "NuGet",
    "nuget": "NuGet",
    ".net": "NuGet",
}


def query_cve(package_name: str, version: str, ecosystem: str = "python", task_id: str = "") -> dict:
    """查询指定包+版本的已知漏洞

    参数:
        package_name: 包名,如 "flask"、"requests"(大小写要和仓库一致)
        version: 版本号,如 "2.0.0"
        ecosystem: 包管理系统,默认 python(PyPI)。其他:npm/go/java/php/ruby/rust/csharp

    返回:{
        "package": str,
        "version": str,
        "ecosystem": str,
        "vulnerabilities": [
            {"id": "CVE-2023-xxx", "severity": "HIGH", "summary": "...", "fixed_in": "2.0.1"},
            ...
        ],
        "count": int
    }
    """
    eco = SUPPORTED_ECOSYSTEMS.get(ecosystem.lower())
    if not eco:
        return {
            "package": package_name,
            "version": version,
            "ecosystem": ecosystem,
            "vulnerabilities": [],
            "count": 0,
            "error": f"不支持的 ecosystem: {ecosystem},支持的有: {', '.join(set(SUPPORTED_ECOSYSTEMS.values()))}",
        }

    # 构造请求
    payload = json.dumps({
        "package": {"name": package_name, "ecosystem": eco},
        "version": version,
    }).encode("utf-8")

    req = urllib.request.Request(
        OSV_QUERY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")[:200]
        logger.error(f"[task={task_id}] OSV 查询失败 HTTP {e.code}: {err_body}")
        return {
            "package": package_name,
            "version": version,
            "ecosystem": eco,
            "vulnerabilities": [],
            "count": 0,
            "error": f"OSV API HTTP {e.code}",
        }
    except Exception as e:
        logger.error(f"[task={task_id}] OSV 查询异常: {e}")
        return {
            "package": package_name,
            "version": version,
            "ecosystem": eco,
            "vulnerabilities": [],
            "count": 0,
            "error": str(e)[:200],
        }

    # 解析漏洞列表
    vulns_raw = data.get("vulns", [])
    vulnerabilities: list[dict[str, Any]] = []
    for v in vulns_raw:
        vid = v.get("id", "UNKNOWN")
        summary = v.get("summary", "")[:200]
        # 提取严重程度
        severity = _extract_severity(v)
        # 提取修复版本(取第一个 affected.range.events.fixed)
        fixed_in = _extract_fixed_version(v, package_name, eco)
        # 提取 CWE
        cwe = _extract_cwe(v)
        vulnerabilities.append({
            "id": vid,
            "severity": severity,
            "summary": summary,
            "fixed_in": fixed_in,
            "cwe": cwe,
        })

    return {
        "package": package_name,
        "version": version,
        "ecosystem": eco,
        "vulnerabilities": vulnerabilities,
        "count": len(vulnerabilities),
    }


# ============================================================
# 辅助:从 OSV 漏洞对象提取关键字段
# ============================================================


def _extract_severity(vuln: dict) -> str:
    """提取严重程度,返回大写字符串

    OSV 用 database_specific.severity 或 severity 数组
    """
    # 优先取 severity 数组(标准字段)
    for s in vuln.get("severity", []):
        # CVSS v3/v4 的 score 可以映射到 HIGH/MEDIUM/LOW
        score_str = s.get("score", "")
        if score_str.startswith("CVSS"):
            try:
                # CVSS 字符串末尾有 base score,如 "CVSS:3.1/AV:N/.../E:U/CR:H/IR:H/AR:H/4.7"
                # 简单按 base score 估等级
                base_score = float(score_str.split("/")[-1])
                if base_score >= 9.0:
                    return "CRITICAL"
                elif base_score >= 7.0:
                    return "HIGH"
                elif base_score >= 4.0:
                    return "MEDIUM"
                else:
                    return "LOW"
            except (ValueError, IndexError):
                pass
    # 回退到 database_specific.severity(字符串,如 "HIGH"、"MODERATE")
    ds = vuln.get("database_specific", {})
    sev = ds.get("severity", "")
    if sev:
        sev_upper = sev.upper()
        if sev_upper in ("CRITICAL", "HIGH", "MEDIUM", "MODERATE", "LOW"):
            return "HIGH" if sev_upper == "MODERATE" else sev_upper
    return "UNKNOWN"


def _extract_fixed_version(vuln: dict, package_name: str, ecosystem: str) -> str:
    """提取修复版本

    OSV 的 affected[].ranges[].events.fixed
    """
    for affected in vuln.get("affected", []):
        pkg = affected.get("package", {})
        if pkg.get("name") != package_name or pkg.get("ecosystem") != ecosystem:
            continue
        for rng in affected.get("ranges", []):
            for event in rng.get("events", []):
                if "fixed" in event:
                    return event["fixed"]
    return ""


def _extract_cwe(vuln: dict) -> str:
    """提取 CWE 编号(若有)"""
    for affected in vuln.get("affected", []):
        # 有些 ecosystem_specific 里有 cwe 字段
        eco_spec = affected.get("ecosystem_specific", {})
        if "cwe" in eco_spec:
            return str(eco_spec["cwe"])
    # 一些漏洞的 aliases 里有 CVE id,可作参考
    aliases = vuln.get("aliases", [])
    for a in aliases:
        if a.startswith("CVE-"):
            return a
    return ""
