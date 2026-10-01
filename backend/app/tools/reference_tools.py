"""引用复核工具(agent2 用):抓取外部 URL 核实 agent1 引用的依据

在后端进程抓取(与 cve_tools 同侧):生产沙箱默认禁外网,只有后端
进程具备出网能力,故不放进沙箱执行器。

安全边界(SSRF 硬防护):
- 仅允许 http/https,拒绝 file:// / ftp:// 等其他 scheme
- 每一跳(含重定向,最多 REFERENCE_MAX_REDIRECTS 跳)解析主机全部 IP,
  逐个校验:拒绝私网/回环/链路本地/保留段/组播/未指定地址,以及
  IPv4-mapped IPv6(如 ::ffff:10.0.0.1);DNS 解析失败即拒
- 防 DNS rebinding(TOCTOU):校验通过后**直连 IP**(http.client 层),
  Host 头与 HTTPS SNI/证书校验仍用原域名,攻击者无法通过"校验后
  切换 DNS 记录"把请求引向内网
- 响应体限量读取(REFERENCE_MAX_BODY_CHARS),防大响应拖垮内存

结果语义(供 agent2 理解,prompt 中同步约定):
- exists=True:链接可达(2xx/3xx);404/410 → exists=False(status=broken)
- reachable=False:网络层不可达(超时/拒连/DNS 失败)。**只代表"复核
  无法完成"(可能是本机网络限制),不代表引用不实**
- content_extractable=False:页面正文过短(典型 SPA 客户端渲染,如
  NVD/cve.org),此时 claim 真伪**不下结论**,只采信可达性结果
- authority:域名分级参考信号(TIER1 权威/TIER2 可信/TIER3 未知),
  非权威认证,最终判断留给 agent2
"""
import http.client
import ipaddress
import logging
import re
import socket
import ssl
from typing import Any
from urllib.parse import urljoin, urlsplit

from app.config import settings

logger = logging.getLogger(__name__)

# 正文抽取:纯文本判定"可抽取"的最小长度;snippet 截断长度
_MIN_EXTRACTABLE_TEXT_CHARS = 100
_SNIPPET_CHARS = 1500

# 域名分级值
_TIER1 = "authoritative"
_TIER2 = "credible"
_TIER3 = "unknown"

# 出错结果的统一模板字段
_ERROR_TEMPLATE = {
    "exists": False,
    "status_code": None,
    "final_url": "",
    "reachable": False,
    "content_extractable": False,
    "authority": _TIER3,
    "authority_reason": "",
    "title": "",
    "snippet": "",
}


class _SSRFBlockedError(Exception):
    """URL 命中 SSRF 防护规则(私网/非法 scheme/DNS 解析失败)"""


# ============================================================
# SSRF 校验
# ============================================================


# CGNAT 共享地址空间(100.64.0.0/10):ipaddress 不归入 is_private,
# 但属于运营商内网段,必须显式拦截
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_forbidden_ip(ip: ipaddress._BaseAddress) -> bool:
    """IP 是否命中禁止段(私网/回环/链路本地/保留/组播/未指定等)"""
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4-mapped IPv6(::ffff:10.0.0.1)按内嵌 IPv4 判定
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return _is_forbidden_ip(mapped)
        # IPv6 unique-local(fc00::/7)is_private 已覆盖
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 含云元数据 169.254.169.254
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NETWORK)
    )


def _resolve_and_validate_host(host: str) -> list[str]:
    """解析主机全部 IP 并逐个 SSRF 校验,返回公网 IP 列表

    任一 IP 命中禁止段或解析失败 → 抛 _SSRFBlockedError。
    校验全部 IP(而非首个)防攻击者把公网 IP 排在私网 IP 前面绕过。
    """
    if not host:
        raise _SSRFBlockedError("URL 缺少主机名")
    # 禁数字/十六进制主机名绕过(如 http://2130706433/ = 127.0.0.1):
    # getaddrinfo 会把数字形式解析成 IP,统一走 ipaddress 判定
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise _SSRFBlockedError(f"DNS 解析失败: {host}") from e

    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        # zone id(fe80::1%eth0)剥离
        ip_str = ip_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise _SSRFBlockedError(f"无法解析的地址: {ip_str}") from e
        if _is_forbidden_ip(ip):
            raise _SSRFBlockedError(f"禁止访问的非公网地址: {ip_str}")
        ips.append(ip_str)
    if not ips:
        raise _SSRFBlockedError(f"主机无可用地址: {host}")
    return ips


# ============================================================
# IP 直连连接(http.client 层,防 DNS rebinding)
# ============================================================


class _IPHTTPConnection(http.client.HTTPConnection):
    """对已校验 IP 直连,Host 头用原域名"""

    def __init__(self, ip: str, port: int, timeout: float, origin_host: str):
        super().__init__(ip, port, timeout=timeout)
        self._origin_host = origin_host

    def putrequest(self, method, url, skip_host=False, skip_accept_encoding=False):
        super().putrequest(
            method, url, skip_host=True,
            skip_accept_encoding=skip_accept_encoding,
        )
        self.putheader("Host", self._origin_host)


class _IPHTTPSConnection(http.client.HTTPSConnection):
    """对已校验 IP 直连,SNI/证书校验与 Host 头用原域名

    标准 HTTPSConnection 用 self.host(此处为 IP)做 SNI 与证书校验,
    直连 IP 会导致证书不匹配,故覆盖 connect():socket 连 IP,
    TLS 握手的 server_hostname 用原域名,SNI 与证书校验均正确。
    """

    def __init__(
        self, ip: str, port: int, timeout: float,
        origin_host: str, context: ssl.SSLContext,
    ):
        super().__init__(ip, port, timeout=timeout, context=context)
        self._origin_host = origin_host

    def connect(self):
        sock = socket.create_connection(
            (self.host, self.port or 443), self.timeout
        )
        self.sock = self._context.wrap_socket(
            sock, server_hostname=self._origin_host
        )

    def putrequest(self, method, url, skip_host=False, skip_accept_encoding=False):
        super().putrequest(
            method, url, skip_host=True,
            skip_accept_encoding=skip_accept_encoding,
        )
        self.putheader("Host", self._origin_host)


def _parse_url(url: str) -> tuple[str, str, int, str]:
    """解析 URL,校验 scheme,返回 (scheme, host, port, path?query)"""
    try:
        parts = urlsplit(url.strip())
    except ValueError as e:
        raise _SSRFBlockedError(f"URL 解析失败: {e}") from e
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise _SSRFBlockedError(f"仅允许 http/https,拒绝: {scheme or '(空)'}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise _SSRFBlockedError("URL 缺少主机名")
    try:
        port = parts.port
    except ValueError as e:
        raise _SSRFBlockedError(f"端口非法: {e}") from e
    if port is None:
        port = 443 if scheme == "https" else 80
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return scheme, host, port, path


def _request_once(
    scheme: str, host: str, port: int, path: str,
    method: str, timeout: float,
) -> http.client.HTTPResponse:
    """对单个 URL 发起一次请求(IP 直连,不跟随重定向)"""
    ips = _resolve_and_validate_host(host)
    ip = ips[0]
    if scheme == "https":
        conn: http.client.HTTPConnection = _IPHTTPSConnection(
            ip, port, timeout, host, ssl.create_default_context()
        )
    else:
        conn = _IPHTTPConnection(ip, port, timeout, host)
    try:
        conn.putrequest(method, path)
        conn.putheader("User-Agent", "SecondLook-ReferenceCheck/1.0")
        conn.putheader("Accept", "text/html,application/xhtml+xml,*/*;q=0.8")
        conn.putheader("Accept-Language", "en,zh;q=0.8")
        conn.endheaders()
        return conn.getresponse()  # caller 负责 conn.close()
    except Exception:
        conn.close()
        raise


_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _fetch(
    url: str, timeout: float, max_redirects: int,
) -> tuple[int, str, bytes]:
    """逐跳抓取(每跳 SSRF 校验),返回 (status, final_url, body_bytes)

    直接 GET(复核本就需要正文判断 claim,HEAD 拿不到 body;
    GET 也天然规避部分站点对 HEAD 返回 405 的问题)。
    body 限量读取,HEAD 语义不需要。
    """
    current = url
    for _hop in range(max_redirects + 1):
        scheme, host, port, path = _parse_url(current)
        resp = None
        try:
            resp = _request_once(scheme, host, port, path, "GET", timeout)
            if resp.status in _REDIRECT_STATUSES:
                location = resp.getheader("Location") or ""
                if not location:
                    raise _SSRFBlockedError(
                        f"重定向({resp.status})缺少 Location 头"
                    )
                current = urljoin(current, location)
                if not current.startswith(("http://", "https://")):
                    # protocol-relative(//host/...)urljoin 已处理,这里兜底
                    raise _SSRFBlockedError(f"非法重定向目标: {location[:200]}")
                continue  # finally 统一关闭当前连接
            body = resp.read(settings.REFERENCE_MAX_BODY_CHARS + 1)
            return resp.status, current, body
        finally:
            if resp is not None:
                resp.close()
    raise _SSRFBlockedError(f"重定向超过 {max_redirects} 跳上限")


# ============================================================
# 正文抽取
# ============================================================

_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)


def _extract_title_and_text(body: bytes) -> tuple[str, str]:
    """从 HTML 抽取 <title> 与正文纯文本(前 _SNIPPET_CHARS 字)"""
    try:
        html = body.decode("utf-8", errors="ignore")
    except Exception:
        return "", ""
    # 去 script/style 后再抽文本,避免把脚本内容当正文
    html_no_ss = _SCRIPT_STYLE_RE.sub(" ", html)
    m = _TITLE_RE.search(html)
    title = _TAG_RE.sub("", m.group(1)).strip() if m else ""
    text = _TAG_RE.sub(" ", html_no_ss)
    text = re.sub(r"\s+", " ", text).strip()
    return title[:300], text[:_SNIPPET_CHARS]


# ============================================================
# 权威性分级
# ============================================================


def _parse_domain_list(raw: str) -> list[str]:
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _match_entry(entry: str, host: str, path: str) -> bool:
    """单个清单条目匹配:纯域名后缀匹配,或"域名/路径前缀"精确匹配"""
    if "/" in entry:
        domain, prefix = entry.split("/", 1)
        prefix = "/" + prefix
        return host == domain and path.startswith(prefix)
    return host == entry or host.endswith("." + entry)


def _classify_authority(final_url: str) -> tuple[str, str]:
    """按域名清单分级,返回 (authority, authority_reason)。

    定位为参考信号:github.com/advisories 等 TIER1 条目用路径前缀限定;
    TIER1 未命中才查 TIER2,其余 TIER3。
    """
    try:
        scheme, host, _port, path = _parse_url(final_url)
    except _SSRFBlockedError:
        return _TIER3, ""
    for entry in _parse_domain_list(settings.REFERENCE_TIER1_DOMAINS):
        if _match_entry(entry, host, path):
            return _TIER1, f"命中权威源清单: {entry}"
    for entry in _parse_domain_list(settings.REFERENCE_TIER2_DOMAINS):
        if _match_entry(entry, host, path):
            return _TIER2, f"命中可信源清单: {entry}"
    return _TIER3, "未命中域名分级清单,来源可靠性未知"


# ============================================================
# 对外入口
# ============================================================


def check_reference(
    url: str, claim: str = "", task_id: str = "",
) -> dict[str, Any]:
    """复核 agent1 引用的外部依据 URL

    参数:
        url: agent1 结论中引用的链接(CVE / 安全公告 / 官方文档等)
        claim: agent1 据其主张的关键点(可选,记录供回溯与前端展示)
        task_id: 任务 id(日志追踪用)

    返回:
        {
          "exists": bool,             # 链接是否存在(2xx/3xx;404/410 → False)
          "status_code": int | None,  # 最终 HTTP 状态码
          "final_url": str,           # 重定向后的最终 URL
          "reachable": bool,          # 网络层是否可达(False 仅代表复核无法完成)
          "content_extractable": bool, # 正文是否可抽取(SPA 页面常为 False)
          "authority": str,           # authoritative / credible / unknown(参考信号)
          "authority_reason": str,
          "title": str, "snippet": str,  # 页面标题与正文摘录(供判断 claim)
          "claim": str,               # 回传入参便于落库追溯
          "error": str,               # 错误说明(成功为空)
        }
    """
    result: dict[str, Any] = dict(_ERROR_TEMPLATE)
    result["claim"] = (claim or "")[:500]
    result["url"] = (url or "")[:2000]

    try:
        status, final_url, body = _fetch(
            url,
            timeout=float(settings.REFERENCE_CHECK_TIMEOUT),
            max_redirects=int(settings.REFERENCE_MAX_REDIRECTS),
        )
    except _SSRFBlockedError as e:
        # SSRF 拦截/非法 URL:对 agent2 明确"不可抓取",不算 unreachable
        # (避免误导为"目标不可达");同时绝不暴露内网探测结果
        logger.info(f"[task={task_id}] check_reference 拦截: {url} → {e}")
        result.update(
            exists=False, reachable=False, error=f"请求被安全策略拦截: {e}"
        )
        return result
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        # 超时/拒连/DNS 失败等:可达性失败,**不代表引用不实**
        logger.info(f"[task={task_id}] check_reference 不可达: {url} → {e}")
        result.update(
            exists=False, reachable=False,
            error=f"网络不可达(超时/拒连/DNS 失败,不代表引用不实): {str(e)[:200]}",
        )
        return result
    except Exception as e:
        logger.exception(f"[task={task_id}] check_reference 未知异常: {url}")
        result.update(exists=False, reachable=False, error=f"抓取异常: {str(e)[:200]}")
        return result

    title, snippet = _extract_title_and_text(body)
    authority, authority_reason = _classify_authority(final_url)
    broken = status in (404, 410)
    result.update(
        exists=not broken,
        status_code=status,
        final_url=final_url[:2000],
        reachable=True,
        content_extractable=len(snippet) >= _MIN_EXTRACTABLE_TEXT_CHARS,
        authority=authority,
        authority_reason=authority_reason,
        title=title,
        snippet=snippet,
        error="链接不存在(404/410)" if broken else "",
    )
    logger.info(
        f"[task={task_id}] check_reference: {url} → {status} "
        f"authority={authority} extractable={result['content_extractable']}"
    )
    return result
