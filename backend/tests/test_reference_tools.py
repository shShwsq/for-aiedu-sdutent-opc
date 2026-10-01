"""reference_tools(check_reference)单元测试

全部通过 monkeypatch mock 网络层(getaddrinfo / _request_once / _fetch),
无真实网络依赖。覆盖:
- SSRF 硬防护:非法 scheme / 私网 / 回环 / 链路本地(云元数据)/
  IPv4-mapped IPv6 / 数字主机名绕过 / 重定向到私网被拦
- DNS rebinding 防护:校验通过后 IP 直连(Host/SNI 用原域名)
- 可达性语义:200 → exists、404 → broken、超时 → unreachable(不代表引用不实)
- SPA 短正文 → content_extractable=False
- 域名分级:github.com/advisories → TIER1、github.com 其他 → TIER2、未知 → TIER3
"""
import socket

import pytest

from app.tools import reference_tools as rt


# ============================================================
# 测试辅助
# ============================================================


class FakeResponse:
    """http.client.HTTPResponse 的最小替身"""

    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self._headers = headers or {}
        self._body = body
        self.closed = False

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, n=-1):
        return self._body[:n] if n and n > 0 else self._body

    def close(self):
        self.closed = True


def mock_getaddrinfo(ips):
    """构造返回指定 IP 列表的 getaddrinfo 替身(AF_INET 优先)"""
    def fake(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in ips
        ]
    return fake


# ============================================================
# SSRF:IP 段判定
# ============================================================


class TestIsForbiddenIp:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1",
        "169.254.169.254",  # 云元数据
        "0.0.0.0", "100.64.0.1",  # 保留/运营商 NAT 段(is_private 覆盖)
        "fd00::1", "fe80::1", "::1",
    ])
    def test_forbidden(self, ip):
        import ipaddress
        assert rt._is_forbidden_ip(ipaddress.ip_address(ip)) is True

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1"])
    def test_public_allowed(self, ip):
        import ipaddress
        assert rt._is_forbidden_ip(ipaddress.ip_address(ip)) is False

    def test_ipv4_mapped_ipv6_forbidden(self):
        import ipaddress
        # ::ffff:10.0.0.1(IPv4-mapped)按内嵌 IPv4 判定
        assert rt._is_forbidden_ip(ipaddress.ip_address("::ffff:10.0.0.1")) is True
        assert rt._is_forbidden_ip(ipaddress.ip_address("::ffff:8.8.8.8")) is False


class TestResolveAndValidateHost:
    def test_private_ip_blocked(self, monkeypatch):
        monkeypatch.setattr(rt.socket, "getaddrinfo", mock_getaddrinfo(["10.0.0.5"]))
        with pytest.raises(rt._SSRFBlockedError):
            rt._resolve_and_validate_host("evil.example.com")

    def test_mixed_ips_any_private_blocked(self, monkeypatch):
        # 公网 IP 排前面也拦(防"首个公网"绕过)
        monkeypatch.setattr(
            rt.socket, "getaddrinfo",
            mock_getaddrinfo(["93.184.216.34", "127.0.0.1"]),
        )
        with pytest.raises(rt._SSRFBlockedError):
            rt._resolve_and_validate_host("evil.example.com")

    def test_public_ips_pass(self, monkeypatch):
        monkeypatch.setattr(
            rt.socket, "getaddrinfo", mock_getaddrinfo(["93.184.216.34"])
        )
        assert rt._resolve_and_validate_host("example.com") == ["93.184.216.34"]

    def test_dns_failure_blocked(self, monkeypatch):
        def fake(host, port, *a, **kw):
            raise socket.gaierror("name unknown")
        monkeypatch.setattr(rt.socket, "getaddrinfo", fake)
        with pytest.raises(rt._SSRFBlockedError):
            rt._resolve_and_validate_host("nonexistent.invalid")


# ============================================================
# SSRF:check_reference 入口拦截
# ============================================================


class TestCheckReferenceSSRF:
    def test_file_scheme_rejected(self):
        r = rt.check_reference("file:///etc/passwd")
        assert r["exists"] is False and r["reachable"] is False
        assert "安全策略拦截" in r["error"]

    def test_ftp_scheme_rejected(self):
        r = rt.check_reference("ftp://example.com/x")
        assert "安全策略拦截" in r["error"]

    def test_loopback_ip_rejected(self):
        # IP 字面量的 getaddrinfo 不查 DNS,本机直接解析
        r = rt.check_reference("http://127.0.0.1:8080/admin")
        assert "安全策略拦截" in r["error"]

    def test_private_ip_rejected(self):
        r = rt.check_reference("http://10.0.0.1/")
        assert "安全策略拦截" in r["error"]

    def test_metadata_ip_rejected(self):
        r = rt.check_reference("http://169.254.169.254/latest/meta-data/")
        assert "安全策略拦截" in r["error"]

    def test_numeric_hostname_rejected(self, monkeypatch):
        # http://2130706433/ = 127.0.0.1 的十进制绕过
        monkeypatch.setattr(
            rt.socket, "getaddrinfo", mock_getaddrinfo(["127.0.0.1"])
        )
        r = rt.check_reference("http://2130706433/")
        assert "安全策略拦截" in r["error"]

    def test_dns_rebinding_domain_to_loopback(self, monkeypatch):
        # 域名"看似正常"但解析到回环(rebinding),必须拦
        monkeypatch.setattr(
            rt.socket, "getaddrinfo", mock_getaddrinfo(["127.0.0.1"])
        )
        r = rt.check_reference("https://normal-looking.example.com/steal")
        assert "安全策略拦截" in r["error"]

    def test_redirect_to_private_blocked(self, monkeypatch):
        # 第一跳 302 → 回环地址,第二跳真实执行被 SSRF 拦截
        real = rt._request_once

        def fake(scheme, host, port, path, method, timeout):
            if not getattr(fake, "_called", False):
                fake._called = True
                return FakeResponse(302, {"Location": "http://127.0.0.1/inner"})
            return real(scheme, host, port, path, method, timeout)

        monkeypatch.setattr(rt, "_request_once", fake)
        r = rt.check_reference("https://attacker.example.com/go")
        assert "安全策略拦截" in r["error"]
        assert r["reachable"] is False


# ============================================================
# DNS rebinding 防护:IP 直连
# ============================================================


class TestIPDirectConnection:
    def test_request_connects_to_ip_with_domain_host_header(self, monkeypatch):
        """校验通过后连接目标必须是解析出的 IP,Host 头用原域名"""
        monkeypatch.setattr(
            rt.socket, "getaddrinfo", mock_getaddrinfo(["93.184.216.34"])
        )

        recorded = {}

        class FakeConn:
            def __init__(self, ip, port, timeout, origin_host):
                recorded["ip"] = ip
                recorded["port"] = port
                recorded["origin_host"] = origin_host
                self.headers = []

            def putrequest(self, method, url, skip_host=False,
                           skip_accept_encoding=False):
                recorded["method"] = method
                recorded["path"] = url
                recorded["skip_host"] = skip_host

            def putheader(self, name, value):
                self.headers.append((name, value))

            def endheaders(self):
                pass

            def getresponse(self):
                return FakeResponse(200, body=b"<html><body>x</body></html>")

            def close(self):
                pass

        monkeypatch.setattr(rt, "_IPHTTPConnection", FakeConn)
        resp = rt._request_once(
            "http", "example.com", 80, "/a?b=1", "GET", 5.0
        )
        assert resp.status == 200
        assert recorded["ip"] == "93.184.216.34"  # 直连 IP,不走域名再解析
        assert recorded["origin_host"] == "example.com"
        assert recorded["path"] == "/a?b=1"


# ============================================================
# 可达性与正文语义
# ============================================================


class TestCheckReferenceSemantics:
    def test_200_ok(self, monkeypatch):
        html = (
            b"<html><head><title>CVE-2024-1234 - Detail</title></head>"
            b"<body>" + b"vulnerability description. " * 30 + b"</body></html>"
        )
        monkeypatch.setattr(
            rt, "_fetch", lambda *a, **kw: (200, "https://nvd.nist.gov/vuln/detail/CVE-2024-1234", html)
        )
        r = rt.check_reference(
            "https://nvd.nist.gov/vuln/detail/CVE-2024-1234",
            claim="该 CVE 为 SQL 注入",
        )
        assert r["exists"] is True and r["reachable"] is True
        assert r["status_code"] == 200
        assert r["title"] == "CVE-2024-1234 - Detail"
        assert "vulnerability description" in r["snippet"]
        assert r["content_extractable"] is True
        assert r["claim"] == "该 CVE 为 SQL 注入"

    def test_404_broken(self, monkeypatch):
        monkeypatch.setattr(
            rt, "_fetch", lambda *a, **kw: (404, "https://example.com/gone", b"")
        )
        r = rt.check_reference("https://example.com/gone")
        assert r["exists"] is False and r["reachable"] is True
        assert "链接不存在" in r["error"]

    def test_410_broken(self, monkeypatch):
        monkeypatch.setattr(
            rt, "_fetch", lambda *a, **kw: (410, "https://example.com/gone", b"")
        )
        r = rt.check_reference("https://example.com/gone")
        assert r["exists"] is False

    def test_timeout_unreachable_not_faking_broken(self, monkeypatch):
        def raise_timeout(*a, **kw):
            raise socket.timeout("timed out")
        monkeypatch.setattr(rt, "_fetch", raise_timeout)
        r = rt.check_reference("https://example.com/slow")
        assert r["reachable"] is False and r["exists"] is False
        # 关键语义:不可达 ≠ 引用不实
        assert "不代表引用不实" in r["error"]

    def test_dns_failure_unreachable(self, monkeypatch):
        def raise_gaierror(*a, **kw):
            raise socket.gaierror("name resolution failed")
        monkeypatch.setattr(rt, "_fetch", raise_gaierror)
        r = rt.check_reference("https://nonexistent.invalid/x")
        assert r["reachable"] is False
        assert "不代表引用不实" in r["error"]

    def test_spa_short_body_not_extractable(self, monkeypatch):
        # SPA 页面:HTML 几乎无正文(客户端渲染)
        monkeypatch.setattr(
            rt, "_fetch",
            lambda *a, **kw: (200, "https://nvd.nist.gov/vuln/detail/CVE-1", b"<html><body></body></html>"),
        )
        r = rt.check_reference("https://nvd.nist.gov/vuln/detail/CVE-1")
        assert r["exists"] is True
        assert r["content_extractable"] is False  # claim 真伪不应下结论

    def test_snippet_strips_script_and_style(self, monkeypatch):
        html = (
            b"<html><head><style>.a{color:red}</style>"
            b"<script>alert('ignore me')</script></head>"
            b"<body><p>" + b"real content. " * 40 + b"</p></body></html>"
        )
        monkeypatch.setattr(
            rt, "_fetch", lambda *a, **kw: (200, "https://example.com/a", html)
        )
        r = rt.check_reference("https://example.com/a")
        assert "alert" not in r["snippet"]
        assert "color:red" not in r["snippet"]
        assert "real content" in r["snippet"]


# ============================================================
# 重定向路由
# ============================================================


class TestRedirects:
    def test_follows_redirect_to_final(self, monkeypatch):
        hops = [
            FakeResponse(302, {"Location": "https://other.example.com/landed"}),
            FakeResponse(200, body=b"<html><body>" + b"ok. " * 40 + b"</body></html>"),
        ]
        monkeypatch.setattr(rt, "_request_once", lambda *a, **kw: hops.pop(0))
        r = rt.check_reference("https://start.example.com/redirect")
        assert r["status_code"] == 200
        assert r["final_url"] == "https://other.example.com/landed"
        assert r["exists"] is True

    def test_too_many_redirects(self, monkeypatch):
        def always_redirect(*a, **kw):
            return FakeResponse(302, {"Location": "https://loop.example.com/next"})
        monkeypatch.setattr(rt, "_request_once", always_redirect)
        r = rt.check_reference("https://loop.example.com/start")
        assert r["exists"] is False
        assert "安全策略拦截" in r["error"]  # 超跳数上限按拦截处理

    def test_relative_location_resolved(self, monkeypatch):
        hops = [
            FakeResponse(301, {"Location": "/new-path"}),
            FakeResponse(200, body=b"<html><body>" + b"x. " * 80 + b"</body></html>"),
        ]
        seen = []

        def fake(scheme, host, port, path, method, timeout):
            seen.append((host, path))
            return hops.pop(0)

        monkeypatch.setattr(rt, "_request_once", fake)
        r = rt.check_reference("https://example.com/old")
        assert r["final_url"] == "https://example.com/new-path"
        assert seen == [("example.com", "/old"), ("example.com", "/new-path")]


# ============================================================
# 域名分级(authority)
# ============================================================


class TestAuthorityClassification:
    def test_github_advisories_tier1(self):
        a, reason = rt._classify_authority(
            "https://github.com/advisories/GHSA-xxxx-yyyy"
        )
        assert a == "authoritative"
        assert "github.com/advisories" in reason

    def test_github_other_paths_tier2(self):
        a, _ = rt._classify_authority("https://github.com/someone/some-repo")
        assert a == "credible"

    def test_nvd_tier1(self):
        a, _ = rt._classify_authority(
            "https://nvd.nist.gov/vuln/detail/CVE-2024-1234"
        )
        assert a == "authoritative"

    def test_subdomain_tier1(self):
        a, _ = rt._classify_authority("https://nvd.nist.gov/anything")
        assert a == "authoritative"

    def test_unknown_domain_tier3(self):
        a, _ = rt._classify_authority("https://random-blog.example.net/post/1")
        assert a == "unknown"

    def test_wikipedia_tier2(self):
        a, _ = rt._classify_authority("https://en.wikipedia.org/wiki/SQL_injection")
        assert a == "credible"

    def test_authority_in_result(self, monkeypatch):
        monkeypatch.setattr(
            rt, "_fetch",
            lambda *a, **kw: (
                200, "https://owasp.org/www-community/attacks/SQL_Injection",
                b"<html><body>" + b"owasp content. " * 40 + b"</body></html>",
            ),
        )
        r = rt.check_reference("https://owasp.org/www-community/attacks/SQL_Injection")
        assert r["authority"] == "authoritative"
        assert r["authority_reason"]
