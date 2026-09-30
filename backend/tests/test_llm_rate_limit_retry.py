"""429 限流退避重试单元测试

针对 app/llm/client.py 的 create_with_rate_limit_retry / _parse_retry_after。
用假的 create_fn + 真实 httpx.Response 构造 openai.RateLimitError,
time.sleep 被 mock 为记录列表,测试不真实等待。
"""
import httpx
import pytest
from openai import RateLimitError

from app.config import settings
from app.llm import client as llm_client
from app.llm.client import _parse_retry_after, create_with_rate_limit_retry


def _make_429(retry_after: str | None = None) -> RateLimitError:
    """构造响应头可控的 RateLimitError"""
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    resp = httpx.Response(
        429,
        headers=headers,
        request=httpx.Request("POST", "https://example.test/v1/chat/completions"),
    )
    return RateLimitError("rate limited", response=resp, body=None)


def _patch(monkeypatch, max_retries: int) -> list[float]:
    """设置重试次数,mock time.sleep 为记录列表并返回"""
    monkeypatch.setattr(settings, "LLM_RATE_LIMIT_MAX_RETRIES", max_retries)
    sleeps: list[float] = []
    monkeypatch.setattr(llm_client.time, "sleep", sleeps.append)
    return sleeps


def test_success_no_retry(monkeypatch):
    """首次成功:不重试、不 sleep"""
    sleeps = _patch(monkeypatch, 3)
    calls = []

    def create_fn():
        calls.append(1)
        return "stream"

    assert create_with_rate_limit_retry(create_fn, label="t") == "stream"
    assert len(calls) == 1
    assert sleeps == []


def test_retry_then_success(monkeypatch):
    """前两次 429、第三次成功:共 3 次调用、2 次退避"""
    sleeps = _patch(monkeypatch, 3)
    calls = []

    def create_fn():
        calls.append(1)
        if len(calls) < 3:
            raise _make_429()
        return "stream"

    assert create_with_rate_limit_retry(create_fn, label="t") == "stream"
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_exhausted_raises(monkeypatch):
    """重试耗尽:原样重抛 RateLimitError,共 1+max_retries 次调用"""
    sleeps = _patch(monkeypatch, 2)
    calls = []

    def create_fn():
        calls.append(1)
        raise _make_429()

    with pytest.raises(RateLimitError):
        create_with_rate_limit_retry(create_fn, label="t")
    assert len(calls) == 3  # 首次 + 2 次重试
    assert len(sleeps) == 2


def test_zero_retries(monkeypatch):
    """max_retries=0:只调一次,直接重抛"""
    sleeps = _patch(monkeypatch, 0)
    calls = []

    def create_fn():
        calls.append(1)
        raise _make_429()

    with pytest.raises(RateLimitError):
        create_with_rate_limit_retry(create_fn, label="t")
    assert len(calls) == 1
    assert sleeps == []


def test_retry_after_header_respected(monkeypatch):
    """厂商返回 Retry-After 时优先采用(视为等待下限,不加抖动)"""
    sleeps = _patch(monkeypatch, 1)
    calls = []

    def create_fn():
        calls.append(1)
        if len(calls) == 1:
            raise _make_429(retry_after="7")
        return "stream"

    assert create_with_rate_limit_retry(create_fn, label="t") == "stream"
    assert sleeps == [7.0]


def test_backoff_without_retry_after(monkeypatch):
    """无 Retry-After:指数退避 base*2^attempt,带 ±25% 抖动"""
    sleeps = _patch(monkeypatch, 2)

    def create_fn():
        if len(sleeps) < 2:
            raise _make_429()
        return "stream"

    assert create_with_rate_limit_retry(create_fn, label="t") == "stream"
    # attempt 0 → 1s ±25%,attempt 1 → 2s ±25%
    assert 0.75 <= sleeps[0] <= 1.25
    assert 1.5 <= sleeps[1] <= 2.5


def test_parse_retry_after_valid():
    assert _parse_retry_after(_make_429("5")) == 5.0


def test_parse_retry_after_invalid_or_missing():
    assert _parse_retry_after(_make_429("abc")) is None
    assert _parse_retry_after(_make_429()) is None


def test_parse_retry_after_clamped():
    # 超大值封顶,负数钳到 0
    assert _parse_retry_after(_make_429("9999")) == llm_client._RETRY_AFTER_MAX
    assert _parse_retry_after(_make_429("-3")) == 0.0
