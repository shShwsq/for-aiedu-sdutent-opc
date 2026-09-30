#!/usr/bin/env python3
"""Codex ACP Bridge:将 ACP JSON-RPC 翻译为 codex exec --json 调用(运行在沙箱内)

Codex CLI 不原生支持 ACP 协议,但提供:
- `codex exec --json`:非交互模式,输出 JSONL 流式事件
- `codex exec resume <thread_id>`:恢复之前的会话(多轮对话)

本 bridge 实现 ACP 兼容层:
1. 监听 HTTP 端口(与 acp_bridge.py 相同的接口)
2. POST /rpc:接收 ACP JSON-RPC 请求
   - initialize → 返回 ACP 能力声明
   - authenticate → 返回成功(凭证经 config.toml + 环境变量注入)
   - session/new → 返回会话 ID
   - session/prompt → 运行 codex exec --json,翻译 JSONL 事件为 ACP 通知
   - session/cancel → 终止当前 codex exec 进程
3. GET /health:健康检查

Codex JSONL 事件 → ACP 通知映射:
- item.started/updated (agent_message) → agent_message_chunk(增量 delta)
- item.started/updated (reasoning)     → thought_chunk(增量 delta)
- item.started (command_execution)     → tool_call
- item.completed (command_execution)   → tool_call_update(completed)
- item.started (file_change)           → tool_call
- item.completed (file_change)         → tool_call_update(completed)
- item.started/updated (todo_list)     → plan
- item.started/updated (mcp_tool_call) → tool_call + tool_call_update
- item.started/updated (web_search)    → tool_call + tool_call_update
- turn.completed                       → ACP 最终响应
- turn.failed / error                  → ACP 错误响应

使用方式(沙箱内):
    python codex_bridge.py --port 8088 --bin codex --args '["--dangerously-bypass-approvals-and-sandbox"]'

依赖:仅 Python 标准库(http.server, subprocess, json, threading, select)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ============================================================
# Codex exec 进程管理 + JSONL → ACP 事件翻译
# ============================================================


class CodexExecSession:
    """管理 codex exec 子进程,翻译 JSONL 事件为 ACP 通知

    每次 session/prompt 启动一个新的 codex exec 进程:
    - 首次:codex exec --json <extra_args> -
    - 后续:codex exec resume <thread_id> --json <extra_args> -

    prompt 经 stdin 传入(避免命令行长度限制)。
    JSONL 事件经 stdout 逐行读取,翻译为 ACP 通知后通过 callback 推送。
    """

    def __init__(self, bin_name: str, extra_args: list[str] | None = None):
        self.bin_name = bin_name
        self.extra_args = extra_args or []
        self.thread_id: str | None = None  # 从 thread.started 事件提取
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        # 累积 codex exec 的 stderr:进程异常退出时拼进 ACP error message,
        # 让真实错误原因(auth/配额/参数/配置等)传回前端,而非只有"退出码 1"
        self._stderr_buf: list[str] = []
        self._stderr_thread = None

    def run_prompt(
        self,
        prompt_text: str,
        on_event,  # callback: (acp_notification: dict) -> None
        on_final,  # callback: (acp_response: dict) -> None
        request_id,
    ) -> None:
        """执行一次 codex exec,翻译事件,完成后调 on_final

        on_event: 每翻译出一个 ACP 通知就调用(中间事件)
        on_final: 翻译完成或出错时调用一次(最终响应,含匹配的 id)
        """
        # 清空上次的 stderr(每次 prompt 独立,避免累积)
        self._stderr_buf.clear()

        # 构建命令
        cmd = [self.bin_name, "exec"]
        if self.thread_id:
            cmd.extend(["resume", self.thread_id])
        cmd.extend(["--json"])
        cmd.extend(self.extra_args)
        cmd.append("-")  # 从 stdin 读取 prompt

        print(f"[codex_bridge] 启动: {' '.join(cmd)}", file=sys.stderr, flush=True)

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        # 写入 prompt 到 stdin 并关闭
        assert self._proc.stdin is not None
        self._proc.stdin.write(prompt_text)
        self._proc.stdin.close()

        # stderr 监控线程(累积到 _stderr_buf + 转发到 bridge stderr)
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stderr_thread.start()

        # 逐行读取 JSONL 事件
        item_texts: dict[str, str] = {}  # item_id → 上次的 text(用于增量 delta)
        session_id = "codex-session"  # ACP 通知需要的 sessionId

        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[codex_bridge] 非 JSON 行,跳过: {line[:200]}", file=sys.stderr, flush=True)
                    continue

                # 翻译事件
                done = self._translate_event(
                    event, item_texts, session_id, on_event, on_final, request_id
                )
                if done:
                    return

        except Exception as e:
            print(f"[codex_bridge] 读取事件异常: {e}", file=sys.stderr, flush=True)
            on_final({
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": f"codex exec 读取异常: {e}"},
                "id": request_id,
            })
            return

        # 进程结束但未收到 turn.completed(可能正常结束或异常退出)
        exit_code = self._proc.wait() if self._proc else -1
        # 等 stderr 线程 drain 完(codex 的真实错误原因在 stderr 里,
        # 不等就拼进 error message 可能截断)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2.0)
        if exit_code != 0:
            stderr_text = "".join(self._stderr_buf).strip()
            msg = f"codex exec 退出码 {exit_code}"
            if stderr_text:
                # 限制长度(避免超长 traceback 撑爆 JSON-RPC message)
                msg += f"\n[codex stderr]\n{stderr_text[-2000:]}"
            on_final({
                "jsonrpc": "2.0",
                "error": {
                    "code": -32603,
                    "message": msg,
                },
                "id": request_id,
            })
        else:
            # 正常退出但未发 turn.completed,发一个空的最终响应
            on_final({
                "jsonrpc": "2.0",
                "result": {"stop": {"reason": "end_turn"}},
                "id": request_id,
            })

    def _translate_event(
        self,
        event: dict,
        item_texts: dict[str, str],
        session_id: str,
        on_event,
        on_final,
        request_id,
    ) -> bool:
        """翻译单个 Codex JSONL 事件为 ACP 通知/响应

        返回 True 表示翻译完成(已发最终响应),调用方应停止读取。
        """
        event_type = event.get("type", "")

        # thread.started:提取 thread_id 用于后续 resume
        if event_type == "thread.started":
            self.thread_id = event.get("thread_id", "")
            print(f"[codex_bridge] thread_id={self.thread_id}", file=sys.stderr, flush=True)
            return False

        # turn.started:忽略
        if event_type == "turn.started":
            return False

        # turn.completed:发最终响应
        if event_type == "turn.completed":
            on_final({
                "jsonrpc": "2.0",
                "result": {"stop": {"reason": "end_turn"}},
                "id": request_id,
            })
            return True

        # turn.failed:发错误响应
        if event_type == "turn.failed":
            error = event.get("error", {})
            msg = error.get("message", "codex turn failed") if isinstance(error, dict) else str(error)
            on_final({
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": msg},
                "id": request_id,
            })
            return True

        # error:发错误响应
        if event_type == "error":
            msg = event.get("message", "codex error")
            on_final({
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": msg},
                "id": request_id,
            })
            return True

        # item.started / item.updated / item.completed:翻译 item 内容
        if event_type in ("item.started", "item.updated", "item.completed"):
            item = event.get("item", {})
            self._translate_item(
                event_type, item, item_texts, session_id, on_event
            )
            return False

        # 未知事件类型
        print(f"[codex_bridge] 未知事件类型: {event_type}", file=sys.stderr, flush=True)
        return False

    def _translate_item(
        self,
        event_type: str,
        item: dict,
        item_texts: dict[str, str],
        session_id: str,
        on_event,
    ) -> None:
        """翻译 Codex ThreadItem 为 ACP 通知"""
        item_id = item.get("id", "")
        item_type = item.get("type", "")

        # agent_message:翻译为 agent_message_chunk(增量)
        if item_type == "agent_message":
            text = item.get("text", "")
            prev = item_texts.get(item_id, "")
            delta = text[len(prev):] if text.startswith(prev) else text
            item_texts[item_id] = text
            if delta:
                on_event(self._make_acp_notification(
                    session_id, "agent_message_chunk",
                    content={"type": "text", "text": delta},
                ))

        # reasoning:翻译为 thought_chunk(增量)
        elif item_type == "reasoning":
            text = item.get("text", "")
            prev = item_texts.get(item_id, "")
            delta = text[len(prev):] if text.startswith(prev) else text
            item_texts[item_id] = text
            if delta:
                on_event(self._make_acp_notification(
                    session_id, "thought_chunk",
                    content={"type": "text", "text": delta},
                ))

        # command_execution:翻译为 tool_call + tool_call_update
        elif item_type == "command_execution":
            command = item.get("command", "")
            status = item.get("status", "")
            output = item.get("aggregated_output", "")
            exit_code = item.get("exit_code")

            if event_type == "item.started":
                on_event(self._make_acp_notification(
                    session_id, "tool_call",
                    toolCallId=item_id,
                    title=f"执行: {command[:80]}" if command else "执行命令",
                    kind="execute",
                    rawInput={"command": command},
                ))
            elif event_type == "item.completed":
                result_text = output
                if exit_code is not None and exit_code != 0:
                    result_text = f"[exit code: {exit_code}]\n{output}"
                on_event(self._make_acp_notification(
                    session_id, "tool_call_update",
                    toolCallId=item_id,
                    status="completed",
                    rawOutput=result_text or "(无输出)",
                ))

        # file_change:翻译为 tool_call + tool_call_update
        elif item_type == "file_change":
            changes = item.get("changes", [])
            status = item.get("status", "")

            if event_type == "item.started":
                paths = [c.get("path", "?") for c in changes]
                on_event(self._make_acp_notification(
                    session_id, "tool_call",
                    toolCallId=item_id,
                    title=f"修改文件: {', '.join(paths[:3])}",
                    kind="file",
                    rawInput={"changes": changes},
                ))
            elif event_type == "item.completed":
                on_event(self._make_acp_notification(
                    session_id, "tool_call_update",
                    toolCallId=item_id,
                    status="completed",
                    rawOutput=f"文件变更完成({len(changes)} 个文件)" if status == "completed" else "文件变更失败",
                ))

        # mcp_tool_call:翻译为 tool_call + tool_call_update
        elif item_type == "mcp_tool_call":
            tool_name = item.get("tool", "mcp_tool")
            server = item.get("server", "")
            status = item.get("status", "")
            arguments = item.get("arguments", {})
            result = item.get("result")
            error = item.get("error")

            if event_type == "item.started":
                on_event(self._make_acp_notification(
                    session_id, "tool_call",
                    toolCallId=item_id,
                    title=f"MCP: {tool_name}",
                    kind="other",
                    rawInput={"server": server, "tool": tool_name, "arguments": arguments},
                ))
            elif event_type == "item.completed":
                if error:
                    output = error.get("message", "MCP tool error")
                elif result:
                    output = json.dumps(result.get("content", []), ensure_ascii=False)
                else:
                    output = "(无输出)"
                on_event(self._make_acp_notification(
                    session_id, "tool_call_update",
                    toolCallId=item_id,
                    status="completed",
                    rawOutput=output,
                ))

        # web_search:翻译为 tool_call + tool_call_update
        elif item_type == "web_search":
            # WebSearchAction 在 item 里可能没有显式 query 字段,用 title 代替
            if event_type == "item.started":
                on_event(self._make_acp_notification(
                    session_id, "tool_call",
                    toolCallId=item_id,
                    title="Web 搜索",
                    kind="other",
                    rawInput={},
                ))
            elif event_type == "item.completed":
                on_event(self._make_acp_notification(
                    session_id, "tool_call_update",
                    toolCallId=item_id,
                    status="completed",
                    rawOutput="搜索完成",
                ))

        # todo_list:翻译为 plan
        elif item_type == "todo_list":
            # Codex 的 todo_list 可能含 items 数组
            items = item.get("items", [])
            if items:
                entries = []
                for i, todo in enumerate(items, 1):
                    if isinstance(todo, dict):
                        entries.append({
                            "content": todo.get("content", todo.get("text", "")),
                            "status": todo.get("status", "pending"),
                        })
                if entries:
                    on_event(self._make_acp_notification(
                        session_id, "plan",
                        entries=entries,
                    ))

        # error item(codex exec 把 Warning/ConfigWarning/Deprecation/ModelRerouted
        # 等非致命提示都包装成 ErrorItem,见 exec event_processor_with_jsonl_output.rs
        # collect_warning —— status 始终 Running)。致命错误走 turn.failed / 顶层
        # error 事件(已由 on_final 终止处理)。翻译为 thought_chunk 通知而非 error
        # 通知,否则 test_credential_streaming 收到 error 通知会立即判失败终止(如
        # "Model metadata for ... not found" 实为 warning,codex 会用 fallback
        # metadata 继续运行)。thought_chunk 让用户仍能看到提示文本但不终止测试。
        elif item_type == "error":
            msg = item.get("message", "未知错误")
            on_event(self._make_acp_notification(
                session_id, "thought_chunk",
                content={"type": "text", "text": msg},
            ))

        # collab_tool_call:忽略(不常用)
        # 其他未知类型:忽略

    @staticmethod
    def _make_acp_notification(
        session_id: str,
        update_type: str,
        content=None,
        toolCallId: str = "",
        title: str = "",
        kind: str = "",
        rawInput=None,
        status: str = "",
        rawOutput: str = "",
        entries=None,
    ) -> dict:
        """构造 ACP session/update 通知"""
        update = {"sessionUpdate": update_type}
        if content is not None:
            update["content"] = content
        if toolCallId:
            update["toolCallId"] = toolCallId
        if title:
            update["title"] = title
        if kind:
            update["kind"] = kind
        if rawInput is not None:
            update["rawInput"] = rawInput
        if status:
            update["status"] = status
        if rawOutput:
            update["rawOutput"] = rawOutput
        if entries is not None:
            update["entries"] = entries
        return {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }

    def _pump_stderr(self) -> None:
        """把 codex exec 的 stderr 累积到 _stderr_buf + 转发到 bridge stderr

        累积的目的是:codex exec 异常退出时,run_prompt 把 stderr 拼进 ACP error
        message,让真实错误原因(auth/配额/参数/配置等)传回前端。
        """
        if not self._proc or not self._proc.stderr:
            return
        for line in self._proc.stderr:
            self._stderr_buf.append(line)
            print(f"[codex stderr] {line.rstrip()}", file=sys.stderr, flush=True)

    def cancel(self) -> None:
        """终止当前 codex exec 进程"""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

    @property
    def alive(self) -> bool:
        """bridge 是否就绪(始终 True,因为 codex exec 按需启动)"""
        return True


# ============================================================
# HTTP 请求处理(与 acp_bridge.py 接口一致)
# ============================================================

# 全局 Codex 会话实例
_codex: CodexExecSession | None = None
# 当前正在处理的 request_id(用于 cancel)
_current_request_id = None


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器(与 acp_bridge.py 兼容)"""

    def log_message(self, format, *args):
        try:
            msg = format % args if args else format
        except Exception:
            msg = f"{format} {args}"
        print(f"[codex_bridge] {msg}", file=sys.stderr, flush=True)

    def do_GET(self):
        """GET /health:健康检查"""
        if self.path == "/health":
            # codex_bridge 按需启动 codex exec,bridge 本身始终就绪
            body = json.dumps({"status": "ok"})
            self._send_json(200, body)
        else:
            self._send_json(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        """POST /rpc:接收 ACP JSON-RPC 请求,返回 SSE

        对于 initialize/authenticate/session/new:快速返回 JSON 响应。
        对于 session/prompt:启动 codex exec,流式返回 ACP 通知 + 最终响应。
        """
        if self.path != "/rpc":
            self._send_json(404, json.dumps({"error": "not found"}))
            return

        if _codex is None:
            self._send_json(503, json.dumps({"error": "Codex session not initialized"}))
            return

        # 读取请求体
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            request = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, json.dumps({"error": f"invalid JSON: {e}"}))
            return

        request_id = request.get("id")
        method = request.get("method", "")

        print(f"[codex_bridge] >>> method={method}, id={request_id}", file=sys.stderr, flush=True)

        # 快速方法:直接返回 JSON 响应(非流式)
        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "result": {
                    "protocolVersion": 1,
                    "capabilities": {},
                    "serverInfo": {"name": "codex", "version": "0.1.0"},
                },
                "id": request_id,
            }
            self._send_sse(response)
            return

        if method == "authenticate":
            # 凭证经 config.toml + 环境变量注入,无需显式认证
            response = {"jsonrpc": "2.0", "result": {}, "id": request_id}
            self._send_sse(response)
            return

        if method == "session/new":
            session_id = f"codex-{uuid.uuid4().hex[:8]}"
            response = {"jsonrpc": "2.0", "result": {"sessionId": session_id}, "id": request_id}
            self._send_sse(response)
            return

        if method == "session/cancel":
            _codex.cancel()
            response = {"jsonrpc": "2.0", "result": {}, "id": request_id}
            self._send_sse(response)
            return

        # session/set_config_option:忽略(Codex 经 config.toml 配置)
        if method == "session/set_config_option":
            response = {"jsonrpc": "2.0", "result": {}, "id": request_id}
            self._send_sse(response)
            return

        # session/prompt:流式返回 SSE
        if method == "session/prompt":
            self._handle_prompt(request, request_id)
            return

        # 未知方法
        response = {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": f"method not found: {method}"},
            "id": request_id,
        }
        self._send_sse(response)

    def _handle_prompt(self, request: dict, request_id):
        """处理 session/prompt:启动 codex exec,流式返回 ACP 事件"""
        params = request.get("params", {})
        prompt_parts = params.get("prompt", [])

        # 从 ACP prompt 数组提取文本
        prompt_text = ""
        for part in prompt_parts:
            if isinstance(part, dict):
                prompt_text += part.get("text", "")
            elif isinstance(part, str):
                prompt_text += part

        if not prompt_text:
            prompt_text = "(空 prompt)"

        # 发送 SSE 响应头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # 定义回调
        def on_event(notification: dict):
            """推送 ACP 通知到 SSE"""
            data = json.dumps(notification, ensure_ascii=False)
            try:
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                pass

        def on_final(response: dict):
            """推送最终响应到 SSE(匹配 id,结束流)"""
            data = json.dumps(response, ensure_ascii=False)
            try:
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                pass

        # 运行 codex exec(同步,阻塞直到完成)
        try:
            _codex.run_prompt(prompt_text, on_event, on_final, request_id)
        except Exception as e:
            error_response = {
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": f"codex exec 失败: {e}"},
                "id": request_id,
            }
            data = json.dumps(error_response, ensure_ascii=False)
            try:
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                pass

    def _send_json(self, status_code: int, body: str) -> None:
        """发送 JSON 响应(用于 GET /health 等非 /rpc 端点)"""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _send_sse(self, response: dict) -> None:
        """以 SSE 流格式发送单条 ACP 响应(POST /rpc 专用)

        ACPClient._rpc 对所有 POST /rpc 响应按 SSE 解析(只认 'data:' 开头的行),
        因此快速方法(initialize/authenticate/session/new/cancel/set_config_option)
        也必须用 SSE 格式,不能用 _send_json 返回普通 JSON——否则客户端解析不到
        任何 data 行,final_result 为 None,session/new 等会因 sessionId 为空失败。
        """
        data = json.dumps(response, ensure_ascii=False)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
        except BrokenPipeError:
            pass


# ============================================================
# 主入口
# ============================================================


def main():
    parser = argparse.ArgumentParser(description="Codex ACP Bridge: HTTP <-> codex exec")
    parser.add_argument(
        "--port", type=int, default=8088,
        help="HTTP 监听端口(默认 8088)",
    )
    parser.add_argument(
        "--bin", type=str, default="codex",
        help="Codex CLI 可执行文件名/路径(默认 codex)",
    )
    parser.add_argument(
        "--args", type=str, default='["--dangerously-bypass-approvals-and-sandbox"]',
        help='codex exec 额外参数(JSON 数组)',
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0",
        help="监听地址(默认 0.0.0.0)",
    )
    args = parser.parse_args()

    # 解析 args JSON
    try:
        extra_args = json.loads(args.args)
        if not isinstance(extra_args, list):
            raise ValueError("args 必须是 JSON 数组")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[codex_bridge] --args 解析失败: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    global _codex
    _codex = CodexExecSession(bin_name=args.bin, extra_args=extra_args)

    # 检查 codex 是否可用
    # 用 shutil.which 查找,而非 subprocess.run(["command","-v",bin]):
    # `command` 是 shell builtin,非独立可执行文件,subprocess.run 默认 shell=False
    # 会在 PATH 中找名为 "command" 的可执行文件,精简容器镜像(如 OpenSandbox)无此文件,
    # 抛 FileNotFoundError 导致 bridge 进程直接退出、HTTP 服务从未启动。
    if not shutil.which(args.bin):
        print(f"[codex_bridge] 警告: {args.bin} 未在 PATH 中找到", file=sys.stderr, flush=True)

    # 启动 HTTP 服务器
    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    print(f"[codex_bridge] HTTP 服务监听 {args.host}:{args.port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[codex_bridge] 正在关闭...", file=sys.stderr, flush=True)
        if _codex:
            _codex.cancel()
        server.server_close()


if __name__ == "__main__":
    main()
