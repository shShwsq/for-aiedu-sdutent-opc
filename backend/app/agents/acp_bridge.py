#!/usr/bin/env python3
"""ACP Bridge:HTTP <-> stdio 桥接服务(运行在沙箱内)

将后端的 HTTP 请求桥接到任意支持 ACP(Agent Client Protocol)over stdio 的
外部 CLI agent(如 Qoder CLI),CLI 通过命令行参数启动,凭证经环境变量注入。

工作原理:
1. 启动时 spawn `<bin> <args...>` 子进程(如 `qodercli --acp --yolo`),
   持有 stdin/stdout pipe。凭证(PAT 等)由父进程(bridge)环境变量继承,
   无需命令行明文传递。
2. 监听 HTTP 端口(默认 8088)
3. POST /rpc:接收 JSON-RPC 请求,写入 CLI stdin,读取 stdout 响应
   - 响应通过 SSE(stream)返回:每行 stdout 作为一个 SSE event
   - 通知(notification,无 id)作为中间事件,最终响应(有 id)作为终止事件
4. GET /health:健康检查(CLI 进程存活返回 200)

ACP 协议(JSON-RPC 2.0 over stdio):
- 请求:{"jsonrpc":"2.0","method":"initialize","params":{...},"id":1}
- 响应:{"jsonrpc":"2.0","result":{...},"id":1}
- 通知:{"jsonrpc":"2.0","method":"progress","params":{...}}  (无 id)

使用方式(沙箱内):
    python acp_bridge.py --port 8088 --bin qodercli --args '["--acp","--yolo"]'

依赖:仅 Python 标准库(http.server, subprocess, json, threading)
"""
import argparse
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ============================================================
# ACP CLI 进程管理(通用,不绑定具体 CLI)
# ============================================================


class ACPCLIProcess:
    """管理 ACP CLI 子进程的 stdin/stdout 通信

    通过 bin + args 启动子进程,子进程继承父进程环境变量
    (凭证经 envs 注入到 bridge,再继承给 CLI,避免命令行明文)。
    """

    def __init__(self, bin_name: str, args: list[str] | None = None):
        self.bin_name = bin_name
        self.args = args or []
        self.proc: subprocess.Popen | None = None
        # _rpc_lock:保护整个 send+collect 串行(ACP 协议是串行的)
        # _stdin_lock:保护 stdin 写入(短临界区,用于 request_permission 响应回写)
        # 拆分原因:POST /rpc 在 wait permission 响应期间持有 _rpc_lock,
        # 但 POST /permission_response 需要唤醒它,不能死锁;
        # 响应回写通过 _stdin_lock 与 /rpc 的请求写入互斥。
        self._rpc_lock = threading.Lock()
        self._stdin_lock = threading.Lock()

    def start(self) -> None:
        """启动 ACP CLI 子进程"""
        cmd = [self.bin_name, *self.args]
        print(f"[bridge] 启动 ACP CLI: {' '.join(cmd)}", flush=True)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # 行缓冲
        )
        # 启动 stderr 监控线程(把 CLI 的 stderr 转发到 bridge 的 stderr)
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stderr(self) -> None:
        """把 CLI 的 stderr 输出到 bridge 的 stderr(调试用)"""
        if not self.proc or not self.proc.stderr:
            return
        for line in self.proc.stderr:
            print(f"[cli stderr] {line.rstrip()}", file=sys.stderr, flush=True)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send_and_collect(self, request: dict) -> list[str]:
        """发送 JSON-RPC 请求,收集所有响应行(通知 + 最终响应)

        返回行列表,每行是一个 JSON 字符串。最后一行是匹配 id 的最终响应。
        通知(无 id)在最终响应之前返回。

        线程安全:用 _rpc_lock 保护整个 send+collect 串行(ACP 协议串行)。
        """
        if not self.alive:
            raise RuntimeError("ACP CLI 进程未运行或已退出")

        request_id = request.get("id")
        request_line = json.dumps(request, ensure_ascii=False)
        collected: list[str] = []

        with self._rpc_lock:
            # 写入请求(加换行符,ACP 用 newline-delimited JSON)
            self._write_stdin(request_line)

            # 逐行读取响应,直到收到匹配 id 的最终响应
            assert self.proc is not None
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                collected.append(line)
                # 检查是否是最终响应(有 id 且匹配)
                try:
                    msg = json.loads(line)
                    if request_id is not None and msg.get("id") == request_id:
                        break
                except json.JSONDecodeError:
                    continue  # 非 JSON 行(如日志),跳过

        return collected

    def _write_stdin(self, line: str) -> None:
        """写一行到 CLI stdin(线程安全,用 _stdin_lock 保护)。

        供 send_and_collect 和 request_permission 响应回写共用。
        """
        assert self.proc is not None
        assert self.proc.stdin is not None
        with self._stdin_lock:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def stop(self) -> None:
        """停止 CLI 子进程"""
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
            self.proc = None


# ============================================================
# HTTP 请求处理
# ============================================================

# 全局 CLI 进程实例(所有请求共享)
_cli: ACPCLIProcess | None = None

# Pending permission 请求管理(CLI 发来的 request_permission 请求,等待用户确认)
# 结构:{request_id: {"event": threading.Event, "result": dict | None}}
# 流程:POST /rpc 读到 request_permission → 存入 _pending_permissions →
#       推 SSE 事件给后端 → 阻塞 event.wait() →
#       POST /permission_response 设 result + event.set() →
#       POST /rpc 唤醒,把 result 写回 CLI stdin
_pending_permissions: dict[str, dict] = {}
_pending_lock = threading.Lock()


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def log_message(self, format, *args):
        # 覆盖默认日志,安全格式化(避免参数数量不匹配导致异常)
        try:
            msg = format % args if args else format
        except Exception:
            msg = f"{format} {args}"
        print(f"[bridge] {msg}", file=sys.stderr, flush=True)

    def do_GET(self):
        """GET /health:健康检查"""
        if self.path == "/health":
            alive = _cli is not None and _cli.alive
            status_code = 200 if alive else 503
            body = json.dumps({"status": "ok" if alive else "cli_not_running"})
            self._send_json(status_code, body)
        else:
            self._send_json(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        """POST /rpc:发送 JSON-RPC 请求,流式返回 SSE(每行 stdout 即时推送)
        POST /permission_response:提交用户对 request_permission 的确认结果

        /rpc 与批量收集模式不同,这里逐行读取 CLI stdout 并立即推送 SSE,
        让后端能实时收到 thinking/text/tool_call 增量,实现真正流式体验。

        线程安全:整个读取过程持有 _cli._rpc_lock,避免并发请求交叉。
        (ACP 是串行协议,同一时刻只处理一个请求,锁不影响吞吐)

        request_permission 处理:
        CLI 检测到危险命令时会发 JSON-RPC 请求(method=request_permission, 有 id),
        bridge 在 SSE 流里推 event: permission_request 事件给后端,后端问用户,
        用户确认后调 POST /permission_response,bridge 把结果写回 CLI stdin。
        """
        if self.path == "/permission_response":
            self._handle_permission_response()
            return

        if self.path != "/rpc":
            self._send_json(404, json.dumps({"error": "not found"}))
            return

        if _cli is None or not _cli.alive:
            self._send_json(503, json.dumps({"error": "CLI process not running"}))
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
        request_method = request.get("method", "")
        request_line = json.dumps(request, ensure_ascii=False)

        print(f"[bridge] >>> 发送到 CLI: method={request_method}, id={request_id}", file=sys.stderr, flush=True)

        # 发送请求 + 流式读取响应(持 _rpc_lock,串行)
        try:
            with _cli._rpc_lock:
                if not _cli.alive:
                    raise RuntimeError("ACP CLI 进程已退出")

                # 写请求到 stdin(用 _write_stdin,内部持 _stdin_lock)
                _cli._write_stdin(request_line)

                # 先发 SSE 响应头
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                # 逐行读取 stdout,立即推送 SSE
                # 使用 readline + 超时检测,避免 CLI 无输出时永久挂起
                assert _cli.proc is not None
                assert _cli.proc.stdout is not None
                import select as _select
                stdout_fd = _cli.proc.stdout.fileno()
                # 不设硬性 deadline:CLI 可能在等异步子 agent(数十秒~数分钟无输出)。
                # 只在 CLI 进程退出或 stdout EOF 时结束,确保不丢失后续响应。
                # select 用 5s 超时,便于周期性检查进程存活 + 推送 idle 心跳。
                line_count = 0
                idle_secs = 0.0
                last_idle_log = 0.0

                while True:
                    # 用 select 检查 stdout 是否有数据(5s 超时,便于周期性检查进程存活)
                    ready, _, _ = _select.select([stdout_fd], [], [], 5.0)
                    if not ready:
                        # 暂无数据,检查 CLI 进程是否还活着
                        if not _cli.alive:
                            print(f"[bridge] CLI 进程在等待响应时退出(method={request_method})", file=sys.stderr, flush=True)
                            break
                        # 推送 idle 心跳到 SSE(带 event: idle 标记,便于 recorder 记录)
                        # 每 5s 一次,让后端知道 bridge 还活着、CLI 还在跑
                        idle_secs += 5.0
                        # 每 30s 打一次日志(避免刷屏)
                        if idle_secs - last_idle_log >= 30.0:
                            print(f"[bridge] 等待 CLI 响应中(method={request_method}, idle={int(idle_secs)}s)", file=sys.stderr, flush=True)
                            last_idle_log = idle_secs
                        # 推送 SSE 注释行(: 开头是 SSE 注释,客户端会忽略,
                        # 但我们的 recorder 会记录原始行,便于事后分析 CLI 卡在哪)
                        try:
                            self.wfile.write(f": idle {int(idle_secs)}s\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except BrokenPipeError:
                            break  # 客户端断开连接
                        continue

                    # 有数据,重置 idle 计数
                    idle_secs = 0.0
                    last_idle_log = 0.0

                    raw_line = _cli.proc.stdout.readline()
                    if not raw_line:
                        # EOF,CLI 进程关闭了 stdout
                        print(f"[bridge] CLI stdout EOF(method={request_method})", file=sys.stderr, flush=True)
                        break

                    line = raw_line.strip()
                    if not line:
                        continue

                    line_count += 1
                    print(f"[bridge] <<< CLI stdout [{line_count}]: {line[:500]}", file=sys.stderr, flush=True)

                    # 检查是否是 CLI 发来的 request_permission 请求(JSON-RPC 请求,有 id)
                    # ACP 协议:CLI 检测危险命令 → 发 request_permission → bridge 转发后端 →
                    # 后端问用户 → POST /permission_response 提交结果 → bridge 写回 CLI stdin
                    try:
                        msg = json.loads(line)
                        if (
                            isinstance(msg, dict)
                            and msg.get("method") == "request_permission"
                            and msg.get("id") is not None
                        ):
                            self._handle_request_permission(msg)
                            continue  # 已处理,继续读 stdout(等待 CLI 后续响应)
                    except json.JSONDecodeError:
                        pass

                    sse_data = f"data: {line}\n\n"
                    try:
                        self.wfile.write(sse_data.encode("utf-8"))
                        self.wfile.flush()
                    except BrokenPipeError:
                        break  # 客户端断开连接

                    # 收到匹配 id 的最终响应 → 结束流
                    try:
                        msg = json.loads(line)
                        if request_id is not None and msg.get("id") == request_id:
                            print(f"[bridge] 收到匹配 id={request_id} 的最终响应,结束流", file=sys.stderr, flush=True)
                            break
                    except json.JSONDecodeError:
                        continue

                if line_count == 0:
                    print(f"[bridge] 警告:CLI 未输出任何响应行(method={request_method})", file=sys.stderr, flush=True)
        except Exception as e:
            # 响应头未发时返回 JSON 错误;已发则只能日志记录
            try:
                self._send_json(500, json.dumps({"error": str(e)}))
            except Exception:
                print(f"[bridge] 流式响应异常: {e}", file=sys.stderr, flush=True)

    def _handle_request_permission(self, msg: dict) -> None:
        """处理 CLI 发来的 request_permission JSON-RPC 请求。

        ACP 协议:CLI 检测到危险命令 → 发 request_permission 请求(有 id)→
        bridge 通过 SSE 流推 event: permission_request 给后端 →
        阻塞等待 POST /permission_response 提交结果 →
        把结果作为 JSON-RPC 响应写回 CLI stdin。

        msg 结构:{
            "jsonrpc": "2.0",
            "method": "request_permission",
            "id": <CLI 分配的 id>,
            "params": {
                "session_id": "...",
                "tool_call": {...},  # ToolCallUpdate,含命令/diff 等
                "options": [{"option_id": "allow_once", "kind": "allow_once", "name": "..."}, ...]
            }
        }
        """
        perm_id = str(msg.get("id"))
        params = msg.get("params", {})
        tool_call = params.get("tool_call", {})
        options = params.get("options", [])

        # 提取命令文本(tool_call.content[0].text 或 raw_input.command)
        command = ""
        description = ""
        raw_input = tool_call.get("raw_input", {})
        if isinstance(raw_input, dict):
            command = str(raw_input.get("command", ""))
            description = str(raw_input.get("description", "")) or command
        # 从 content 提取展示文本(备用)
        if not command:
            content_list = tool_call.get("content", [])
            if isinstance(content_list, list) and content_list:
                first_content = content_list[0]
                if isinstance(first_content, dict):
                    text_block = first_content.get("text", "")
                    if isinstance(text_block, str):
                        command = text_block

        # 构造 permission_request 事件载荷(后端用它推 command_confirm SSE 给前端)
        perm_payload = {
            "id": perm_id,
            "tool_call_id": tool_call.get("id", ""),
            "title": tool_call.get("title", ""),
            "kind": tool_call.get("kind", ""),
            "command": command,
            "description": description,
            "options": options,
        }

        # 推 SSE 事件给后端(event: permission_request,后端 ACPClient 识别此事件)
        sse_event = f"event: permission_request\ndata: {json.dumps(perm_payload, ensure_ascii=False)}\n\n"
        try:
            self.wfile.write(sse_event.encode("utf-8"))
            self.wfile.flush()
            print(f"[bridge] 推送 permission_request 事件(perm_id={perm_id}, command={command[:100]})", file=sys.stderr, flush=True)
        except BrokenPipeError:
            print(f"[bridge] 推送 permission_request 失败:客户端断开(perm_id={perm_id})", file=sys.stderr, flush=True)
            return

        # 注册 pending permission,阻塞等待 POST /permission_response 唤醒
        event = threading.Event()
        with _pending_lock:
            _pending_permissions[perm_id] = {"event": event, "result": None}

        # 阻塞等待(无超时,用户可能需要很久才确认;CLI 进程退出时会被 select 检测到)
        # 期间定期检查 CLI 是否还活着,避免 CLI 死了还在等
        while True:
            if event.wait(timeout=5.0):
                break
            if not _cli or not _cli.alive:
                print(f"[bridge] 等待 permission 响应时 CLI 退出(perm_id={perm_id})", file=sys.stderr, flush=True)
                break

        # 取出结果
        with _pending_lock:
            entry = _pending_permissions.pop(perm_id, None)
        result = entry["result"] if entry else None

        # 构造 JSON-RPC 响应写回 CLI stdin
        if result is None:
            # 超时/CLI 退出,默认拒绝
            outcome = {"outcome": "rejected"}
            print(f"[bridge] permission 无结果,默认拒绝(perm_id={perm_id})", file=sys.stderr, flush=True)
        else:
            outcome = result.get("outcome", {"outcome": "rejected"})

        response = {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"outcome": outcome},
        }
        response_line = json.dumps(response, ensure_ascii=False)
        _cli._write_stdin(response_line)
        print(f"[bridge] permission 响应已写回 CLI(perm_id={perm_id}, outcome={outcome})", file=sys.stderr, flush=True)

    def _handle_permission_response(self) -> None:
        """POST /permission_response:后端提交用户对 request_permission 的确认结果。

        请求体:{
            "id": "<perm_id>",  # 对应 request_permission 的 id
            "outcome": {"outcome": "selected", "option_id": "allow_once"}  # 或 {"outcome": "rejected"}
        }
        """
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, json.dumps({"error": f"invalid JSON: {e}"}))
            return

        perm_id = str(data.get("id", ""))
        outcome = data.get("outcome", {"outcome": "rejected"})

        with _pending_lock:
            entry = _pending_permissions.get(perm_id)
            if entry is None:
                self._send_json(404, json.dumps({"error": f"permission id not found: {perm_id}"}))
                return
            entry["result"] = {"outcome": outcome}
            entry["event"].set()

        print(f"[bridge] 收到 permission 响应(perm_id={perm_id}, outcome={outcome})", file=sys.stderr, flush=True)
        self._send_json(200, json.dumps({"status": "ok"}))

    def _send_json(self, status_code: int, body: str) -> None:
        """发送 JSON 响应"""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


# ============================================================
# 主入口
# ============================================================


def main():
    parser = argparse.ArgumentParser(description="ACP Bridge: HTTP <-> stdio")
    parser.add_argument(
        "--port", type=int, default=8088,
        help="HTTP 监听端口(默认 8088)",
    )
    parser.add_argument(
        "--bin", type=str, default="qodercli",
        help="ACP CLI 可执行文件名/路径(默认 qodercli,从 PATH 查找或绝对路径)",
    )
    parser.add_argument(
        "--args", type=str, default='["--acp", "--permission-mode", "bypass_permissions"]',
        help='CLI 启动参数(JSON 数组,默认 \'["--acp", "--permission-mode", "bypass_permissions"]\')',
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0",
        help="监听地址(默认 0.0.0.0,允许外部访问)",
    )
    args = parser.parse_args()

    # 解析 args JSON
    try:
        cli_args = json.loads(args.args)
        if not isinstance(cli_args, list):
            raise ValueError("args 必须是 JSON 数组")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[bridge] --args 解析失败: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    global _cli

    # 启动 CLI 子进程(继承当前环境变量,凭证经 envs 注入到 bridge 进程)
    _cli = ACPCLIProcess(bin_name=args.bin, args=cli_args)
    _cli.start()

    # 等待 CLI 就绪(短暂等待进程稳定)
    time.sleep(1)
    if not _cli.alive:
        print("[bridge] ACP CLI 启动失败,退出", file=sys.stderr, flush=True)
        sys.exit(1)

    # 启动 HTTP 服务器
    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    print(f"[bridge] HTTP 服务监听 {args.host}:{args.port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[bridge] 正在关闭...", file=sys.stderr, flush=True)
        if _cli:
            _cli.stop()
        server.server_close()


if __name__ == "__main__":
    main()
