"""
Codex Agent SDK Client - App Server Implementation
A client for interacting with Codex via the "codex app-server" JSON-RPC protocol.

The app-server provides a persistent bidirectional stdio connection with support
for interactive approval, multi-turn conversations, and streaming events.
"""

import asyncio
import json
import os
import shutil
import sys
import logging
from typing import Optional, Dict, Any, AsyncIterator, List, Callable, Union

LOG = logging.getLogger(__package__)

from .base_agent import MessageType, Message, TextBlock, AssistantMessage, \
    PermissionResultAllow, PermissionResultDeny, ToolPermissionContext, AgentOptions, BaseAgent


class CodexAgent(BaseAgent):
    """
    Client for interacting with Codex via "codex app-server" (JSON-RPC over stdio).

    Unlike the old "codex exec --json" approach which spawned a new process per
    turn and had no interactive approval support, the app-server maintains a
    single long-lived process with bidirectional communication.

    Key features:
    - Persistent process: single codex app-server subprocess for the session
    - Bidirectional: send and receive JSON-RPC messages at any time
    - Approval support: handle command/file-change approval requests
    - Multi-turn: reuse the same thread across multiple messages
    """

    def __init__(self, options: Optional[AgentOptions] = None):
        super().__init__(options)
        self.thread_id: Optional[str] = None
        self.cli_path = self.options.cli_path or shutil.which("codex")
        self._is_connected = False

        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._process: Optional[asyncio.subprocess.Process] = None
        self._read_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._rpc_id: int = 0
        # Pending RPC responses keyed by request id
        self._pending_responses: Dict[int, asyncio.Future] = {}
        self._permission_callback: Optional[Callable] = self.options.can_use_tool
        # Track active turn so we know when it completes
        self._active_turn_id: Optional[str] = None

        if not self.cli_path:
            raise FileNotFoundError(
                "Codex CLI not found in PATH. Please install it or set 'codex_command' in settings."
            )
        LOG.info(f"Codex CLI path: {self.cli_path}")

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def connect(self, prompt: Optional[str] = None) -> None:
        """
        Start the codex app-server subprocess, perform the JSON-RPC
        initialize handshake, and create a thread.
        """
        if self._is_connected:
            raise RuntimeError("Client is already connected")

        env = os.environ.copy()
        if self.options.api_key is not None:
            env["CODEX_API_KEY"] = self.options.api_key
        if self.options.base_url is not None:
            env["OPENAI_BASE_URL"] = self.options.base_url

        cmd = [self.cli_path, "app-server"]

        LOG.info(f"Starting Codex app-server: {' '.join(cmd)}")
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        self._is_connected = True
        self._read_task = asyncio.create_task(self._read_messages())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # JSON-RPC initialize handshake
        await self._rpc_request("initialize", {
            "clientInfo": {"name": "codyform", "title": "Codyform", "version": "1.0"},
            "capabilities": {"experimentalApi": True}
        })
        await self._rpc_notify("initialized")

        # Create a thread
        config = {"cwd": self.options.cwd}
        if self.options.model:
            config["model"] = self.options.model
        if self.options.sandbox_mode:
            # Map simple string modes to sandbox object
            sandbox_map = {
                "read-only": {"type": "readOnly"},
                "workspace-write": {
                    "type": "workspaceWrite",
                    "writableRoots": [],
                    "networkAccess": False,
                },
                "danger-full-access": {"type": "dangerFullAccess"},
            }
            config["sandbox"] = sandbox_map.get(self.options.sandbox_mode, {
                "type": "workspaceWrite",
                "writableRoots": [],
                "networkAccess": False,
            })

        result = await self._rpc_request("thread/start", {"config": config})
        if result and isinstance(result, dict):
            thread = result.get("thread", {})
            self.thread_id = thread.get("id")
            LOG.info(f"Codex thread started: {self.thread_id}")

        if prompt:
            await self.send_message(prompt)

    async def send_message(self, content: str, parent_tool_use_id: Optional[str] = None) -> None:
        """Send a user message to Codex by starting a new turn on the thread."""
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        if not self.thread_id:
            raise RuntimeError("No active thread. Call connect() first.")

        await self._rpc_request("turn/start", {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": content}],
        })

    async def respond_approval(self, approval_id: str, approved: bool) -> None:
        """Respond to a command execution approval request."""
        await self._rpc_request("approval/respondCommandExecution", {
            "approvalId": approval_id,
            "approved": approved,
        })

    async def respond_file_approval(self, approval_id: str, approved: bool) -> None:
        """Respond to a file change approval request."""
        await self._rpc_request("approval/respondFileChange", {
            "approvalId": approval_id,
            "approved": approved,
        })

    async def _write_json(self, data: Dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError("Process not available")
        raw = json.dumps(data) + "\n"
        self._process.stdin.write(raw.encode("utf-8"))
        await self._process.stdin.drain()

    async def _rpc_request(self, method: str, params: Dict[str, Any], timeout: float = 30.0) -> Any:
        """Send a JSON-RPC request and wait for its response."""
        rid = self._next_id()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_responses[rid] = future

        await self._write_json({"method": method, "id": rid, "params": params})
        LOG.debug(f"rpc_request [{rid}] {method}")

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_responses.pop(rid, None)
            LOG.error(f"RPC timeout for {method} (id={rid})")
            return None

    async def _rpc_notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        msg = {"method": method}
        if params:
            msg["params"] = params
        await self._write_json(msg)

    async def _read_messages(self) -> None:
        """Background task to read JSONL from app-server stdout."""
        if not self._process or not self._process.stdout:
            return

        buffer = b""
        chunk_limit = 65536

        try:
            while self._is_connected:
                try:
                    chunk = await self._process.stdout.read(chunk_limit)
                except Exception:
                    break
                if not chunk:
                    break

                buffer += chunk
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        LOG.debug(f"codex receive: {line}")
                        data = json.loads(line)
                        await self._dispatch(data)
                    except json.JSONDecodeError:
                        LOG.debug(f"codex non-json: {line[:200]}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.error(f"read_messages error: {e}")
            await self._message_queue.put(Message(MessageType.ERROR.value, content=str(e)))

    async def _read_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return
        try:
            while self._is_connected:
                line = await self._process.stderr.readline()
                if not line:
                    break
                LOG.debug(f"codex stderr: {line.decode('utf-8', errors='replace').strip()}")
        except (asyncio.CancelledError, Exception):
            pass

    # ── Message dispatch ────────────────────────────────────────────────

    async def _dispatch(self, data: Dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message to the right handler."""

        # RPC response (has "id" but no "method")
        if "id" in data and "method" not in data:
            rid = data["id"]
            future = self._pending_responses.pop(rid, None)
            if future and not future.done():
                if "error" in data:
                    LOG.error(f"RPC error [{rid}]: {data['error']}")
                    future.set_result(None)
                else:
                    future.set_result(data.get("result"))
            return

        method = data.get("method", "")
        params = data.get("params", {})

        LOG.debug(f"codex event: {method}")

        if method == "turn/started":
            self._active_turn_id = params.get("turnId")

        elif method == "turn/completed":
            self._active_turn_id = None
            await self._message_queue.put(Message(MessageType.STOP.value))

        elif method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if delta:
                await self._message_queue.put(
                    Message(MessageType.TEXT.value, content=delta,
                            msg_id=params.get("itemId"))
                )

        elif method == "item/completed":
            await self._handle_item_completed(params)

        elif method == "item/started":
            await self._handle_item_started(params)

        elif method == "item/commandExecution/requestApproval":
            await self._handle_command_approval(params)

        elif method == "item/fileChange/requestApproval":
            await self._handle_file_approval(params)

        elif method == "codex/event/error" or method == "error":
            msg = params.get("msg", {})
            err_text = msg.get("message", "") if isinstance(msg, dict) else str(params)
            await self._message_queue.put(
                Message(MessageType.ERROR.value, content=err_text)
            )

    async def _handle_item_started(self, params: Dict[str, Any]) -> None:
        item = params.get("item", {})
        item_type = item.get("type")

        if item_type == "commandExecution":
            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "command_execution",
                    "command": item.get("command"),
                    "status": "in_progress",
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

    async def _handle_item_completed(self, params: Dict[str, Any]) -> None:
        item = params.get("item", {})
        item_type = item.get("type")

        if item_type == "agentMessage":
            text = item.get("text", "")
            if text:
                blocks = [TextBlock(text)]
                msg = AssistantMessage(content=blocks, msg_id=item.get("id"))
                await self._message_queue.put(msg)

        elif item_type == "commandExecution":
            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "command_execution",
                    "command": item.get("command"),
                    "output": item.get("aggregatedOutput"),
                    "exit_code": item.get("exitCode"),
                    "status": item.get("status"),
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

        elif item_type == "fileChange":
            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "file_change",
                    "changes": item.get("changes", []),
                    "status": item.get("status"),
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

        elif item_type == "mcpToolCall":
            server = item.get("server", "")
            tool = item.get("tool", "")
            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": f"{server}/{tool}",
                    "input": item.get("arguments", {}),
                    "result": item.get("result", {}),
                    "error": item.get("error", {}),
                    "status": item.get("status"),
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

        elif item_type == "reasoning":
            text = ""
            summary = item.get("summary", [])
            if summary:
                text = summary[0] if isinstance(summary, list) else str(summary)
            if text:
                await self._message_queue.put(
                    Message(MessageType.THINKING.value, content=text, msg_id=item.get("id"))
                )

    async def _handle_command_approval(self, params: Dict[str, Any]) -> None:
        """Handle a command execution approval request."""
        approval_id = params.get("approvalId")
        command = params.get("command", "")
        cwd = params.get("cwd", "")

        LOG.info(f"Approval request [{approval_id}]: {command} in {cwd}")

        if self._permission_callback:
            context = ToolPermissionContext()
            try:
                result = await self._permission_callback(
                    "command_execution",
                    {"command": command, "cwd": cwd, "approvalId": approval_id},
                    context,
                )
                approved = isinstance(result, PermissionResultAllow)
                await self.respond_approval(approval_id, approved)
            except Exception as e:
                LOG.error(f"Permission callback error: {e}")
                await self.respond_approval(approval_id, False)
        else:
            # No callback: auto-approve (matching old exec behavior)
            await self.respond_approval(approval_id, True)

    async def _handle_file_approval(self, params: Dict[str, Any]) -> None:
        """Handle a file change approval request."""
        approval_id = params.get("approvalId")

        LOG.info(f"File approval request [{approval_id}]")

        if self._permission_callback:
            context = ToolPermissionContext()
            try:
                result = await self._permission_callback(
                    "file_change",
                    {"approvalId": approval_id, **params},
                    context,
                )
                approved = isinstance(result, PermissionResultAllow)
                await self.respond_file_approval(approval_id, approved)
            except Exception as e:
                LOG.error(f"Permission callback error: {e}")
                await self.respond_file_approval(approval_id, False)
        else:
            await self.respond_file_approval(approval_id, True)

    async def receive_messages(self) -> AsyncIterator[Message]:
        """
        Yields messages from the agent.
        Keeps running until disconnect, matching ClaudeAgent behavior.
        """
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        while self._is_connected:
            try:
                message = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=0.1,
                )
                yield message
            except asyncio.TimeoutError:
                if self._process and self._process.returncode is not None:
                    break
                continue

    async def disconnect(self) -> None:
        """Disconnect and cleanup resources."""
        self._is_connected = False

        # Cancel background tasks
        for task in (self._read_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Cancel pending RPC futures
        for future in self._pending_responses.values():
            if not future.done():
                future.cancel()
        self._pending_responses.clear()

        # Terminate process
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()


async def query(
    prompt: str,
    options: Optional[AgentOptions] = None,
) -> AsyncIterator[Message]:
    """Query Codex for one-shot interactions."""
    if options is None:
        options = AgentOptions()

    client = CodexAgent(options=options)

    try:
        await client.connect(prompt=prompt)

        async for message in client.receive_messages():
            yield message

            msg_type = getattr(message, "type", None)
            if msg_type == MessageType.STOP.value:
                break
    finally:
        await client.disconnect()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async def chat(client, prompt):
        print(f"\n[User] {prompt}")
        await client.send_message(prompt)
        async for msg in client.receive_messages():
            msg_type = getattr(msg, "type", None)
            if msg_type == "assistant":
                for b in msg.content:
                    print(f"  Codex: {b.text if hasattr(b, 'text') else b}")
            elif msg_type == MessageType.TEXT.value:
                print(msg.content, end="", flush=True)
            elif msg_type == MessageType.STOP.value:
                print()
                break

    async def test():
        async with CodexAgent() as client:
            await chat(client, "Tell me a random joke.")
            await chat(client, "Make it shorter.")

    asyncio.run(test())
