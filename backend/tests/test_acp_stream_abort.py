"""ACP 流异常终止兜底(CLI 崩溃/连接中断)单元测试

覆盖 acp_bridge → acp_base 两端的 stream_error 契约:
- bridge /rpc 循环:CLI stdout EOF 等异常关流前推 event: stream_error
  (含原因),并 close_connection 快速失败
- ACPClient._rpc:流结束但未收到 id 匹配的最终响应 → 抛 ACPStreamAborted
  (优先用 bridge 推送的原因;未收到时用通用描述)
- ACPClient._rpc:收到最终响应 → 正常返回(不误伤);无 id 通知 → 返回 {}
- ACPClient.prompt:流异常终止 → cancel + 截断标记 + 空结果(不 fail 任务,
  由调用方用已累积输出收尾并标注"输出不完整")
- recorder:异常终止元信息落盘 JSONL(事后按文件定位崩溃原因)

背景:qoder CLI(Node)崩溃(如 OOM)时 bridge 关闭 SSE 连接,此前 _rpc
静默返回 {} 把崩溃当作正常收尾,残缺输出直接流入 agent2 审查。
"""
import io
import json
import queue as queue_mod
import threading
from unittest.mock import MagicMock

import pytest

from app.agents import acp_bridge
from app.agents.acp_base import ACPClient, ACPStreamAborted, _ACPRecorder


# ============================================================
# 辅助:伪造 httpx streaming response(_rpc 的 with stream 消费)
# ============================================================


class _FakeStreamResponse:
    """模拟 httpx streaming response:status_code + iter_lines"""

    def __init__(self, lines: list[str], status_code: int = 200):
        self.status_code = status_code
        self._lines = lines

    def iter_lines(self):
        yield from self._lines


class _StreamCM:
    """with client._client.stream(...) 的上下文管理器替身"""

    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc):
        return False


def _install_stream(monkeypatch, client: ACPClient, lines: list[str]) -> None:
    resp = _FakeStreamResponse(lines)
    monkeypatch.setattr(client._client, "stream", lambda *a, **kw: _StreamCM(resp))


# ============================================================
# ACPClient._rpc:异常终止检测
# ============================================================


def test_rpc_raises_when_stream_ends_without_final(monkeypatch):
    """流结束但未收到 id 匹配的最终响应(CLI 崩溃后 bridge 关流)→ 抛 ACPStreamAborted"""
    client = ACPClient("http://fake")
    lines = [
        'data: {"jsonrpc":"2.0","method":"session/update","params":{}}',
        "",
    ]
    _install_stream(monkeypatch, client, lines)
    with pytest.raises(ACPStreamAborted):
        client._rpc({"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 3})


def test_rpc_aborted_is_runtime_error():
    """继承 RuntimeError:new_session 等调用方的 bridge 日志增强路径仍可捕获"""
    assert issubclass(ACPStreamAborted, RuntimeError)


def test_rpc_uses_stream_error_reason(monkeypatch):
    """bridge 关流前推的 event: stream_error 原因进入异常消息(诊断可见)"""
    client = ACPClient("http://fake")
    lines = [
        'data: {"jsonrpc":"2.0","method":"session/update","params":{}}',
        "",
        "event: stream_error",
        'data: {"reason": "cli_exit", "message": "CLI 进程在执行中退出(returncode=3)"}',
        "",
    ]
    _install_stream(monkeypatch, client, lines)
    with pytest.raises(ACPStreamAborted) as ei:
        client._rpc({"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 3})
    assert "returncode=3" in str(ei.value)


def test_rpc_generic_message_without_stream_error(monkeypatch):
    """未收到 stream_error(网络中断/旧版 bridge)→ 用通用描述,不掩盖异常"""
    client = ACPClient("http://fake")
    _install_stream(monkeypatch, client, [""])
    with pytest.raises(ACPStreamAborted) as ei:
        client._rpc({"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 1})
    assert "最终响应" in str(ei.value)


def test_rpc_returns_final_response_normally(monkeypatch):
    """收到 id 匹配的最终响应 → 正常返回 result,通知照常分发(不误伤)"""
    client = ACPClient("http://fake")
    lines = [
        'data: {"jsonrpc":"2.0","method":"session/update","params":{"update":{}}}',
        "",
        'data: {"jsonrpc":"2.0","result":{"stopReason":"end_turn"},"id":3}',
        "",
    ]
    _install_stream(monkeypatch, client, lines)
    events = []
    result = client._rpc(
        {"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 3},
        on_event=events.append,
    )
    assert result == {"stopReason": "end_turn"}
    assert len(events) == 1  # 通知正常分发


def test_rpc_notification_without_id_returns_empty(monkeypatch):
    """无 id 的请求(防御分支,当前代码不发送):流结束不报错,返回 {}"""
    client = ACPClient("http://fake")
    _install_stream(monkeypatch, client, [""])
    result = client._rpc({"jsonrpc": "2.0", "method": "notify", "params": {}})
    assert result == {}


def test_rpc_records_stream_abort_in_recorder(monkeypatch, tmp_path):
    """异常终止时 recorder 落盘 meta 行,JSONL 日志可事后定位崩溃原因"""
    recorder = _ACPRecorder("task-stream-abort", 1, log_dir=tmp_path)
    client = ACPClient("http://fake", recorder=recorder)
    lines = [
        "event: stream_error",
        'data: {"reason": "stdout_eof", "message": "CLI 输出流关闭(EOF),疑似进程崩溃"}',
        "",
    ]
    _install_stream(monkeypatch, client, lines)
    with pytest.raises(ACPStreamAborted):
        client._rpc({"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 1})
    recorder.close()

    jsonl_files = list(tmp_path.glob("*.jsonl"))
    assert len(jsonl_files) == 1
    content = jsonl_files[0].read_text(encoding="utf-8")
    assert "event: stream_error" in content  # 原始事件行(record_raw 优先落盘)
    assert "stream aborted" in content  # _rpc 的 meta 标记行


# ============================================================
# ACPClient.prompt:流异常终止善后(与 idle 超时同款兜底)
# ============================================================


def test_prompt_stream_abort_salvages(monkeypatch):
    """prompt 捕获 ACPStreamAborted:cancel + 截断标记 + 空结果(不 fail 任务)"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(
        client, "_rpc",
        MagicMock(side_effect=ACPStreamAborted("CLI 进程在执行中退出(returncode=3)")),
    )
    cancelled = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))

    result = client.prompt("sess-1", [{"type": "text", "text": "hi"}])

    assert result == {}
    assert cancelled == ["sess-1"]
    assert client.last_prompt_truncated is not None
    assert "returncode=3" in client.last_prompt_truncated


def test_prompt_resets_truncated_flag_on_stream_abort(monkeypatch):
    """流异常终止置标记后,下一次 prompt 正常完成时重置(状态不跨轮串扰)"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(
        client, "_rpc", MagicMock(side_effect=ACPStreamAborted("CLI 崩溃")),
    )
    monkeypatch.setattr(client, "cancel", lambda sid: None)
    client.prompt("sess-1", [{"type": "text", "text": "hi"}])
    assert client.last_prompt_truncated is not None

    monkeypatch.setattr(client, "_rpc", MagicMock(return_value={"stopReason": "end"}))
    result = client.prompt("sess-1", [{"type": "text", "text": "hi"}])
    assert result == {"stopReason": "end"}
    assert client.last_prompt_truncated is None


# ============================================================
# bridge 端:异常关流前推 stream_error 事件
# ============================================================


def _mk_handler() -> tuple:
    """构造不经过 socket 的 BridgeHandler(object.__new__ 绕过 __init__),
    挂 BytesIO 作 wfile,直接验证 SSE 输出"""
    handler = object.__new__(acp_bridge.BridgeHandler)
    sink = io.BytesIO()
    handler.wfile = sink
    return handler, sink


def test_bridge_push_stream_error_writes_sse_event():
    """_push_stream_error 输出标准 SSE 事件(event: + data: JSON,双换行结尾)"""
    handler, sink = _mk_handler()
    handler._push_stream_error("cli_exit", "CLI 进程在执行中退出(returncode=3)")
    out = sink.getvalue().decode("utf-8")
    assert out.startswith("event: stream_error\n")
    assert '"reason": "cli_exit"' in out
    assert "returncode=3" in out
    assert out.endswith("\n\n")


def test_bridge_push_stream_error_client_gone(capsys):
    """客户端已断开(BrokenPipe/连接重置)时静默跳过,不抛异常"""
    handler, _ = _mk_handler()

    class _BrokenPipe(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError("client gone")

        def flush(self):
            pass

    handler.wfile = _BrokenPipe()
    handler._push_stream_error("stdout_eof", "CLI 输出流关闭(EOF),疑似进程崩溃")
    # 不抛异常即通过


def test_bridge_rpc_pushes_stream_error_on_eof(monkeypatch):
    """/rpc 集成:CLI stdout EOF(进程崩溃)→ 通知照常转发,
    流尾部推 event: stream_error 并 close_connection 快速失败"""
    q: queue_mod.Queue = queue_mod.Queue()
    q.put((
        "line",
        '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk"}}}',
    ))
    q.put(("end", None))  # CLI stdout EOF 哨兵

    fake_cli = MagicMock()
    fake_cli.alive = True
    fake_cli._stdout_q = q
    fake_cli._rpc_lock = threading.Lock()
    monkeypatch.setattr(acp_bridge, "_cli", fake_cli)

    handler = object.__new__(acp_bridge.BridgeHandler)
    handler.path = "/rpc"
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.requestline = "POST /rpc HTTP/1.1"  # log_request 默认实现需要
    body = json.dumps({"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 3})
    handler.headers = {"Content-Length": str(len(body.encode("utf-8")))}
    handler.rfile = io.BytesIO(body.encode("utf-8"))
    sink = io.BytesIO()
    handler.wfile = sink

    handler.do_POST()

    out = sink.getvalue().decode("utf-8")
    assert "agent_message_chunk" in out  # 崩溃前的通知正常转发
    assert "event: stream_error" in out
    assert '"reason": "stdout_eof"' in out
    assert handler.close_connection is True  # 无最终响应 → 关连接快速失败


def test_bridge_rpc_final_response_no_stream_error(monkeypatch):
    """/rpc 集成:收到最终响应正常关流,不推 stream_error、不关连接"""
    q: queue_mod.Queue = queue_mod.Queue()
    q.put(("line", '{"jsonrpc":"2.0","result":{"stopReason":"end_turn"},"id":3}'))

    fake_cli = MagicMock()
    fake_cli.alive = True
    fake_cli._stdout_q = q
    fake_cli._rpc_lock = threading.Lock()
    monkeypatch.setattr(acp_bridge, "_cli", fake_cli)

    handler = object.__new__(acp_bridge.BridgeHandler)
    handler.path = "/rpc"
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.requestline = "POST /rpc HTTP/1.1"  # log_request 默认实现需要
    body = json.dumps({"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 3})
    handler.headers = {"Content-Length": str(len(body.encode("utf-8")))}
    handler.rfile = io.BytesIO(body.encode("utf-8"))
    sink = io.BytesIO()
    handler.wfile = sink

    handler.do_POST()

    out = sink.getvalue().decode("utf-8")
    assert "stopReason" in out
    assert "event: stream_error" not in out
    assert not getattr(handler, "close_connection", False)
