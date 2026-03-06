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
import re
from typing import Optional, Dict, Any, AsyncIterator, List, Callable, Union

LOG = logging.getLogger(__package__)

from .base_agent import MessageType, Message, TextBlock, AssistantMessage, \
    PermissionResultAllow, PermissionResultDeny, ToolPermissionContext, AgentOptions, BaseAgent


def _find_codex_cli() -> Optional[str]:
    """Search common default install locations for the codex CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(appdata, "npm", "codex.cmd"),
            os.path.join(appdata, "npm", "codex"),
            os.path.join(local_appdata, "Programs", "codex", "codex.exe"),
            os.path.join(local_appdata, "Programs", "codex", "codex.cmd"),
        ]
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "codex"),
            os.path.join(home, ".npm-global", "bin", "codex"),
            os.path.join(home, ".yarn", "bin", "codex"),
            "/usr/local/bin/codex",
            "/opt/homebrew/bin/codex",           # macOS (Intel/Apple Silicon)
            "/home/linuxbrew/.linuxbrew/bin/codex",  # Linux Homebrew
        ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            LOG.info(f"Found codex CLI at default location: {path}")
            return path
    return None



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
        self.cli_path = self.options.cli_path or shutil.which("codex") or _find_codex_cli()
        self._is_connected = False
        self.available_models: List[Dict[str, Any]] = []

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
        # Pending approval responses from the UI, keyed by approval_id
        self._pending_approvals: Dict[str, asyncio.Future] = {}
        # Cache item data from item/started, keyed by itemId
        self._item_cache: Dict[str, Dict[str, Any]] = {}
        # Plan mode: mutable at runtime (separate from options.plan_mode snapshot)
        self.plan_mode: bool = self.options.plan_mode
        # Accumulates item/plan/delta content in plan mode
        self._plan_text: str = ""

        if not self.cli_path:
            raise FileNotFoundError(
                "Codex CLI not found in PATH. Please install it or set 'codex_command' in settings."
            )
        LOG.info(f"Codex CLI path: {self.cli_path}")

    async def _fetch_models(self) -> None:
        """Fetch available models via the model/list RPC method."""
        try:
            result = await self._rpc_request("model/list", {})
            if not result or not isinstance(result, dict):
                return
            models = [
                {
                    "displayName": m.get("displayName") or m.get("id", ""),
                    "description": m.get("description", ""),
                    "value": m.get("id", ""),
                }
                for m in result.get("data", [])
                if m.get("id") and not m.get("hidden", False)
            ]
            if models:
                self.available_models = models
                LOG.info(f"Fetched {len(models)} models via model/list RPC")
                await self._message_queue.put(Message(
                    "models_update",
                    content={"models": models},
                ))
        except Exception as e:
            LOG.warning(f"Failed to fetch models via model/list: {e}")

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

        # Inject user-defined extra environment variables (including ANTHROPIC_API_KEY etc.)
        if self.options.extra_env:
            env.update(self.options.extra_env)

        cmd = [self.cli_path, "app-server"]

        LOG.info(f"Starting Codex app-server: {' '.join(cmd)}")
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.options.cwd,
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

        # Fetch available models via model/list RPC (no API key required)
        asyncio.create_task(self._fetch_models())

        # Create a thread
        thread_params = {"cwd": self.options.cwd}
        if self.options.model:
            thread_params["model"] = self.options.model

        # Map approve_mode to Codex approvalPolicy:
        #   "untrusted"  — always ask for approval (commands + file changes)
        #   "on-failure" — ask only when a command fails
        #   "on-request" — ask only when the agent itself requests it
        #   "never"      — never ask
        approve_mode = self.options.approve_mode
        if approve_mode == "accept-all":
            thread_params["approvalPolicy"] = "never"
        else:
            # default / allow-edit → all operations require approval
            thread_params["approvalPolicy"] = "untrusted"

        if self.options.sandbox_mode:
            sandbox_map = {
                "read-only": {"type": "readOnly"},
                "workspace-write": {
                    "type": "workspaceWrite",
                    "writableRoots": [],
                    "networkAccess": False,
                },
                "danger-full-access": {"type": "dangerFullAccess"},
            }
            thread_params["sandbox"] = sandbox_map.get(self.options.sandbox_mode, {
                "type": "workspaceWrite",
                "writableRoots": [],
                "networkAccess": False,
            })

        result = await self._rpc_request("thread/start", thread_params)
        if result and isinstance(result, dict):
            thread = result.get("thread", {})
            self.thread_id = thread.get("id")
            LOG.info(f"Codex thread started: {self.thread_id}")

        if prompt:
            await self.send_message(prompt)

    def set_model(self, model: str) -> None:
        """Dynamically switch the model; takes effect on the next turn."""
        self.options.model = model
        LOG.info(f"Codex model switched to: {model}")

    async def send_message(self, content: str, parent_tool_use_id: Optional[str] = None) -> None:
        """Send a user message to Codex by starting a new turn on the thread."""
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        if not self.thread_id:
            raise RuntimeError("No active thread. Call connect() first.")

        self._plan_text = ""
        params: Dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": content}],
        }
        if self.options.model:
            params["model"] = self.options.model
        if self.plan_mode:
            params["collaborationMode"] = {
                "mode": "plan",
                "settings": {
                    "model": self.options.model,
                    "developer_instructions": None,
                },
            }

        await self._rpc_request("turn/start", params)

    async def _respond_to_server_request(self, request_id: Any, result: Dict[str, Any]) -> None:
        """Send a JSON-RPC response to a server-initiated request."""
        await self._write_json({"id": request_id, "result": result})

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

        if method == "turn/started":
            self._active_turn_id = params.get("turnId")

        elif method == "turn/completed":
            self._active_turn_id = None
            if self.plan_mode and self._plan_text:
                await self._message_queue.put(
                    Message(MessageType.PLAN_DELTA.value, content=self._plan_text)
                )
                self._plan_text = ""
            await self._message_queue.put(Message(MessageType.STOP.value))

        elif method == "item/plan/delta":
            delta = params.get("delta", "")
            if delta:
                self._plan_text += delta

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
            # Run in a separate task to avoid blocking the message reader
            asyncio.create_task(self._handle_command_approval(data["id"], params))

        elif method == "item/fileChange/requestApproval":
            asyncio.create_task(self._handle_file_approval(data["id"], params))

        elif method == "codex/event/stream_error":
            msg = params.get("msg", {})
            err_text = msg.get("message", "") if isinstance(msg, dict) else str(params)
            LOG.warning(f"Codex stream error: {err_text}")

        elif method == "codex/event/error" or method == "error":
            msg = params.get("msg", {})
            err_text = msg.get("message", "") if isinstance(msg, dict) else str(params)
            await self._message_queue.put(
                Message(MessageType.ERROR.value, content=err_text)
            )

    async def _handle_item_started(self, params: Dict[str, Any]) -> None:
        item = params.get("item", {})
        item_type = item.get("type")

        # Cache item data so requestApproval handlers can look it up by itemId
        item_id = item.get("id")
        if item_id:
            self._item_cache[item_id] = item

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
            changes = item.get("changes", [])
            filenames = []
            for c in changes:
                path = c.get("path")
                if path:
                    filenames.append(os.path.basename(path))

            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "file_change",
                    "changes": changes,
                    "filenames": filenames,
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

    async def send_approval_response(self, approval_id: str, response_data: Dict[str, Any]) -> None:
        """
        Called by the UI to respond to an approval request.
        Routes to the pending future so the approval handler can complete.
        """
        future = self._pending_approvals.get(approval_id)
        if future and not future.done():
            future.set_result(response_data)

    async def _handle_command_approval(self, request_id: Any, params: Dict[str, Any]) -> None:
        """Handle a command execution approval request."""
        approval_id = str(request_id)
        command = params.get("command", "")
        cwd = params.get("cwd", "")

        LOG.info(f"Command approval request [rpc_id={request_id}]: {command} in {cwd}")

        input_data = {"command": command}
        approved = await self._emit_approval_request(approval_id, "command_execution", input_data)
        decision = "accept" if approved else "decline"
        await self._respond_to_server_request(request_id, {"decision": decision})

    async def _handle_file_approval(self, request_id: Any, params: Dict[str, Any]) -> None:
        """Handle a file change approval request."""
        approval_id = str(request_id)

        LOG.info(f"File approval request [rpc_id={request_id}]")

        # requestApproval params only contain threadId/turnId/itemId, not the changes.
        # Look up the item data cached from the preceding item/started event.
        item_id = params.get("itemId")
        item_data = self._item_cache.get(item_id, {}) if item_id else {}

        # Pre-process diff data for the UI using the cached item's changes
        processed_diff = self._generate_file_change_diff(item_data)

        input_data = {**params}
        if processed_diff:
            input_data["processed_diff"] = processed_diff

        approved = await self._emit_approval_request(approval_id, "file_change", input_data)
        decision = "accept" if approved else "decline"
        await self._respond_to_server_request(request_id, {"decision": decision})

    def _generate_file_change_diff(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Processes Codex file change parameters and generates combined diff data.
        Returns a dict with { 'old_text', 'new_text', 'display_name' } or None.
        """
        changes = params.get("changes", [])
        if not changes and "fileChanges" in params:
            # Convert dict to list for uniform processing
            file_changes = params.get("fileChanges", {})
            if isinstance(file_changes, dict):
                for path, change in file_changes.items():
                    if isinstance(change, dict):
                        change_copy = change.copy()
                        change_copy["path"] = path
                        changes.append(change_copy)

        if not changes:
            return None

        old_full_text = ""
        new_full_text = ""

        for change in changes:
            path = change.get("path", "")
            # Codex uses {"kind": {"type": "update"}} while other formats use top-level "type"
            kind = change.get("kind", {})
            change_type = (kind.get("type") if isinstance(kind, dict) else None) or change.get("type", "update")
            # Codex uses "diff" field name; also support "patch" and "unified_diff"
            patch = change.get("diff") or change.get("patch") or change.get("unified_diff")
            content = change.get("content", "")

            if not path:
                continue

            # Read old content
            old_text = ""
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        old_text = f.read()
                except Exception:
                    pass

            new_text = old_text
            # Logic for deriving new_text
            if change_type in ("add", "create"):
                new_text = content or ""
            elif change_type == "delete":
                new_text = ""
            else: # update/edit
                if patch:
                    new_text = self._apply_patch(old_text, patch)
                elif content: # Full content overwrite
                    new_text = content

            # Combine into full diff if multiple files
            if len(changes) > 1:
                header = f"File: {path}\n" + "="*40 + "\n"
                old_full_text += header + old_text + "\n\n"
                new_full_text += header + new_text + "\n\n"
            else:
                old_full_text = old_text
                new_full_text = new_text

        display_name = os.path.basename(changes[0].get("path", "file"))
        if len(changes) > 1:
            display_name = f"{len(changes)} files"

        return {
            "old_text": old_full_text,
            "new_text": new_full_text,
            "display_name": display_name,
            "count": len(changes),
            "files": [os.path.basename(c.get('path', '')) for c in changes if c.get('path')]
        }

    def _apply_patch(self, old_text: str, patch: str) -> str:
        """
        Apply a unified diff patch to old_text.
        Returns the modified text or original text if patch fails.
        """
        if not patch:
            return old_text

        lines = old_text.splitlines(keepends=True)
        patch_lines = patch.splitlines(keepends=True)

        # Simple hunk parsing: @@ -start,count +start,count @@
        hunk_re = re.compile(r'^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@')

        applied_lines = []
        current_old_line = 0

        i = 0
        # Skip header lines if they exist (e.g. --- / +++)
        while i < len(patch_lines) and not patch_lines[i].startswith('@@'):
            i += 1

        if i == len(patch_lines):
            return old_text

        while i < len(patch_lines):
            line = patch_lines[i]
            match = hunk_re.match(line)
            if match:
                try:
                    old_start = int(match.group(1)) - 1 # 0-indexed
                    while current_old_line < old_start and current_old_line < len(lines):
                        applied_lines.append(lines[current_old_line])
                        current_old_line += 1

                    i += 1
                    while i < len(patch_lines) and not patch_lines[i].startswith('@@'):
                        p_line = patch_lines[i]
                        if p_line.startswith(' '):
                            if current_old_line < len(lines):
                                applied_lines.append(lines[current_old_line])
                                current_old_line += 1
                        elif p_line.startswith('+'):
                            applied_lines.append(p_line[1:])
                        elif p_line.startswith('-'):
                            current_old_line += 1
                        i += 1
                    continue
                except Exception as e:
                    LOG.error(f"Error applying diff hunk: {e}")
                    return old_text
            i += 1

        while current_old_line < len(lines):
            applied_lines.append(lines[current_old_line])
            current_old_line += 1

        return "".join(applied_lines)

    async def _emit_approval_request(self, approval_id: str, tool_name: str, input_data: Dict[str, Any]) -> bool:
        """
        Emit a control_request message to the message queue and wait for
        the UI to respond via send_approval_response().
        Returns True if approved, False if denied.
        """
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_approvals[approval_id] = future

        # Emit control_request in the same format as Claude agent
        control_msg = Message(
            "control_request",
            content={
                "request_id": approval_id,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": tool_name,
                    "input": input_data,
                },
            },
        )
        await self._message_queue.put(control_msg)

        try:
            response_data = await future
        except asyncio.CancelledError:
            response_data = {"behavior": "deny"}
        finally:
            self._pending_approvals.pop(approval_id, None)

        return response_data.get("behavior") == "allow"

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

        # Cancel pending approval futures
        for future in self._pending_approvals.values():
            if not future.done():
                future.cancel()
        self._pending_approvals.clear()

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
