"""
Claude Agent SDK Client - Standard Library Implementation
A reimplementation of the Claude Agent SDK that calls the Claude Code CLI
using only Python standard libraries (no external dependencies).

Reference: https://github.com/anthropics/claude-agent-sdk-python
"""

import asyncio
import json
import os
import shutil
import sys
import logging
from typing import Optional, Dict, Any, AsyncIterator, List, Callable, Union
from enum import Enum

# logger by package name
LOG = logging.getLogger(__package__)

class MessageType(Enum):
    """Types of messages that can be received from Claude"""
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    STOP = "stop"
    THINKING = "thinking"


class Message:
    """Represents a message from Claude CLI"""

    def __init__(self, msg_type: str, content: Any = None, msg_id: Optional[str] = None, **kwargs):
        self.type = msg_type
        self.content = content
        self.id = msg_id
        self.raw_data = kwargs

    def __repr__(self):
        return f"Message(type={self.type}, id={self.id}, content={self.content})"


class TextBlock:
    """Represents a text content block"""

    def __init__(self, text: str):
        self.text = text
        self.type = "text"

    def __repr__(self):
        return f"TextBlock(text={self.text[:50]}...)"


class PermissionResultAllow:
    """Result indicating permission is granted"""
    def __init__(self, updated_input: Optional[Dict[str, Any]] = None):
        self.updated_input = updated_input


class PermissionResultDeny:
    """Result indicating permission is denied"""
    def __init__(self, message: str = "Permission denied"):
        self.message = message


class ToolPermissionContext:
    """Context for tool permission requests"""
    def __init__(self, suggestions: Optional[List[Dict[str, Any]]] = None):
        self.suggestions = suggestions or []


class AssistantMessage:
    """Represents an assistant message with content blocks"""

    def __init__(self, content: List[Union[TextBlock, Any]], msg_id: Optional[str] = None):
        self.content = content
        self.id = msg_id
        self.role = "assistant"
        self.type = "assistant"  # Add type attribute for consistency

    def __repr__(self):
        return f"AssistantMessage(id={self.id}, blocks={len(self.content)})"


class ClaudeAgentOptions:
    """Configuration options for Claude Agent"""

    def __init__(
        self,
        cwd: Optional[str] = None,
        cli_path: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_turns: Optional[int] = None,
        allowed_tools: Optional[List[str]] = None,
        permission_mode: str = "default",
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        can_use_tool: Optional[Callable] = None,
        plan_mode: bool = False
    ):
        self.cwd = cwd or os.getcwd()
        self.cli_path = cli_path
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools or []
        self.permission_mode = permission_mode  # 'default', 'acceptEdits', 'bypassPermissions'
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.auth_token = auth_token
        self.can_use_tool = can_use_tool
        self.plan_mode = plan_mode


class ClaudeCodeAgent:
    """
    Client for bidirectional, interactive conversations with Claude Code.

    This client provides full control over the conversation flow with support
    for streaming, interrupts, and dynamic message sending.

    Key features:
    - Bidirectional: Send and receive messages at any time
    - Stateful: Maintains conversation context across messages
    - Interactive: Send follow-ups based on responses
    - Control flow: Support for interrupts and session management
    """

    def __init__(self, options: Optional[ClaudeAgentOptions] = None):
        """Initialize Claude SDK client"""
        self.options = options or ClaudeAgentOptions()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.is_connected = False
        self._read_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._session_id: Optional[str] = None
        self._permission_callback = self.options.can_use_tool

        # Find claude executable
        self.cli_path = self.options.cli_path or shutil.which("claude")
        if not self.cli_path:
            raise FileNotFoundError(
                "Claude CLI not found. Please install it first:\n"
                "curl -fsSL https://claude.ai/install.sh | bash"
            )

    async def connect(self, prompt: Optional[str] = None) -> None:
        """
        Connect to Claude with an optional initial prompt.

        Args:
            prompt: Optional initial prompt to send after connection
        """
        if self.is_connected:
            raise RuntimeError("Client is already connected")

        # Build command arguments for streaming JSON mode
        # --print: Non-interactive mode
        # --output-format=stream-json: Stream JSON responses
        # --input-format=stream-json: Accept JSON input stream
        # --replay-user-messages: Echo user messages for acknowledgment
        # --verbose: Required when using stream-json output format
        # --permission-prompt-tool: Enable permission prompts for tool usage
        cmd = [
            self.cli_path,
            "--print",
            "--output-format=stream-json",
            "--input-format=stream-json",
            "--replay-user-messages",
            "--verbose",
            "--permission-prompt-tool=stdio",
        ]

        # Add plan mode if enabled (overrides permission mode if strictly enforced)
        if self.options.plan_mode:
            cmd.extend(["--permission-mode", "plan"])

        # Add system prompt if specified
        if self.options.system_prompt:
            cmd.extend(["--system-prompt", self.options.system_prompt])

        # Add model if specified
        if self.options.model:
            cmd.extend(["--model", self.options.model])

        # Set up environment
        env = os.environ.copy()
        env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"

        if self.options.api_key is not None:
            env["ANTHROPIC_API_KEY"] = self.options.api_key

        if self.options.base_url is not None:
            env["ANTHROPIC_BASE_URL"] = self.options.base_url

        if self.options.auth_token is not None:
            env["ANTHROPIC_AUTH_TOKEN"] = self.options.auth_token

        # Start subprocess
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.options.cwd
        )

        self.is_connected = True

        # Start background task to read messages
        self._read_task = asyncio.create_task(self._read_messages())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # Send initialization control_request
        await self._send_initialize_request()

        # Send initial prompt if provided
        if prompt:
            await self.send_message(prompt)



    async def send_message(self, content: str, parent_tool_use_id: Optional[str] = None) -> None:
        """
        Send a user message to Claude.

        Args:
            content: The message content to send
            parent_tool_use_id: Optional parent tool use ID for tool results
        """
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        # Format message according to Claude CLI stream-json format
        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": content
            }
        }

        if parent_tool_use_id:
            message["parent_tool_use_id"] = parent_tool_use_id

        if self._session_id:
            message["session_id"] = self._session_id

        await self._write_json(message)

    async def _write_json(self, data: Dict[str, Any]) -> None:
        """Write a JSON message to the subprocess stdin"""
        if not self.process or not self.process.stdin:
            raise RuntimeError("Process not available")

        json_line = json.dumps(data) + "\n"
        self.process.stdin.write(json_line.encode("utf-8"))
        await self.process.stdin.drain()

    async def _send_initialize_request(self) -> None:
        """Send initialization control_request to Claude CLI"""
        import uuid
        request_id = f"req_init_{uuid.uuid4().hex[:8]}"

        init_request = {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "initialize",
                "hooks": None
            }
        }

        await self._write_json(init_request)
        LOG.debug(f"Sent initialization control_request: {request_id}")

    async def _handle_permission_request(self, data: Dict[str, Any]) -> None:
        """Handle permission request from Claude CLI"""
        request_id = data.get("request_id")
        request = data.get("request", {})
        tool_name = request.get("tool_name")
        input_data = request.get("input", {})
        suggestions = request.get("permission_suggestions", [])

        # Create context
        context = ToolPermissionContext(suggestions=suggestions)

        try:
            # Call the permission callback
            result = await self._permission_callback(tool_name, input_data, context)

            response_data = {}
            # Send response based on result
            if isinstance(result, PermissionResultAllow):
                response_data = {
                    "behavior": "allow",
                    "updatedInput": (result.updated_input if result.updated_input is not None
                        else input_data),
                }
            elif isinstance(result, PermissionResultDeny):
                response_data = {
                    "behavior": "deny",
                    "message": result.message
                }

            await self._send_control_response(
                request_id=request_id,
                response_data=response_data
            )
        except Exception as e:
            LOG.error(f"Error in permission callback: {e}")
            # Default to deny on error
            await self._send_control_error(
                request_id=request_id,
                error=f"Permission callback error: {str(e)}"
            )

    async def _send_control_response(
        self,
        request_id: str,
        response_data: Dict[str, Any]
    ) -> None:
        """Send a success control response to Claude CLI"""
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response_data
            }
        }
        await self._write_json(response)

    async def _send_control_error(
        self,
        request_id: str,
        error: str
    ) -> None:
        """Send an error control response to Claude CLI"""
        response = {
            "type": "control_response",
            "response": {
                "subtype": "error",
                "request_id": request_id,
                "error": error
            }
        }
        await self._write_json(response)


    async def _read_messages(self) -> None:
        """Background task to continuously read messages from Claude CLI"""
        if not self.process or not self.process.stdout:
            return
        chunk_limit = 65536
        buffer = b""

        try:
            while self.is_connected:
                # Read chunks instead of lines to avoid buffer limits
                try:
                    chunk = await self.process.stdout.read(chunk_limit)
                except Exception:
                    break

                if not chunk:
                    break

                buffer += chunk

                # Process lines from buffer
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                        LOG.debug(f"claude msg: {data}")
                        message = self._parse_message(data)
                        await self._message_queue.put(message)
                    except json.JSONDecodeError:
                        LOG.error(f"claude non-json msg: {line[:200]}...")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.error(f"Reading messages error: {e}")
            error_msg = Message("error", content=str(e))
            await self._message_queue.put(error_msg)

    async def _read_stderr(self) -> None:
        """Background task to read stderr from Claude CLI"""
        if not self.process or not self.process.stderr:
            return

        try:
            while self.is_connected:
                line = await self.process.stderr.readline()
                if not line:
                    break

                line_str = line.decode("utf-8").strip()
                if line_str:
                    # Optionally log stderr to a file or ignore
                    LOG.error(f"claude stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            pass  # Silently ignore stderr errors

    def _parse_message(self, data: Dict[str, Any]) -> Message:
        """Parse raw JSON data into a Message object"""
        msg_type = data.get("type", "unknown")
        msg_id = data.get("id") or data.get("uuid")

        # Handle system init message - extract session_id
        if msg_type == "system" and data.get("subtype") == "init":
            self._session_id = data.get("session_id")
            return Message(msg_type, content=data, msg_id=msg_id)

        # Handle control_request message for permission callbacks
        if msg_type == "control_request":
            request_id = data.get("request_id")
            request = data.get("request", {})
            subtype = request.get("subtype")

            if subtype == "can_use_tool" and self._permission_callback:
                # Schedule permission callback handling
                asyncio.create_task(self._handle_permission_request(data))

            return Message(msg_type, content=data, msg_id=msg_id)

        # Handle assistant message from Claude CLI stream-json format
        if msg_type == "assistant":
            # Extract the nested message object
            message_data = data.get("message", {})
            content_blocks = message_data.get("content", [])

            # Parse content blocks
            blocks = []
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        blocks.append(TextBlock(block.get("text", "")))
                    else:
                        blocks.append(block)

            assistant_msg = AssistantMessage(
                content=blocks,
                msg_id=message_data.get("id")
            )
            return assistant_msg

        # Handle result message (final status)
        if msg_type == "result":
            return Message(msg_type, content=data.get("result"), msg_id=msg_id)

        # Handle control_response message
        if msg_type == "control_response":
            return Message(msg_type, content=data, msg_id=msg_id)

        # Handle other message types
        content = data.get("content") or data.get("message")
        return Message(msg_type, content, msg_id, **data)

    async def receive_messages(self) -> AsyncIterator[Message]:
        """
        Receive messages from Claude as an async iterator.

        Yields:
            Message objects from Claude
        """
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        while self.is_connected:
            try:
                # Wait for next message with timeout
                message = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=0.1
                )
                yield message

                # Stop if we receive a stop message
                if message.type == "stop":
                    break
            except asyncio.TimeoutError:
                # Check if process is still alive
                if self.process and self.process.returncode is not None:
                    break
                continue

    async def disconnect(self) -> None:
        """Disconnect from Claude and cleanup resources"""
        if not self.is_connected:
            return

        self.is_connected = False

        # Cancel read tasks
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if hasattr(self, '_stderr_task') and self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

        # Terminate process
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def __aenter__(self):
        """Async context manager entry"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.disconnect()


async def query(
    prompt: str,
    options: Optional[ClaudeAgentOptions] = None
) -> AsyncIterator[Message]:
    """
    Query Claude Code for one-shot or unidirectional streaming interactions.

    This function is ideal for simple, stateless queries where you don't need
    bidirectional communication or conversation management.

    Args:
        prompt: The prompt to send to Claude
        options: Optional configuration (defaults to ClaudeAgentOptions() if None)

    Yields:
        Messages from the conversation

    Example:
        async for message in query(prompt="What is 2 + 2?"):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text)
    """
    if options is None:
        options = ClaudeAgentOptions()

    client = ClaudeCodeAgent(options=options)

    try:
        await client.connect(prompt=prompt)

        async for message in client.receive_messages():
            yield message

            # Stop if we receive a stop/result message
            msg_type = getattr(message, 'type', None)
            if msg_type in ("stop", "result"):
                break
    finally:
        await client.disconnect()



if __name__ == "__main__":
    # Windows compatibility
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async def simple_query_example():
        """Example using the simple query() function"""
        print("Simple Query Example")
        print("=" * 50)
        print()

        message_count = 0
        async for message in query(prompt="Tell me about you in one sentence"):
            message_count += 1

            if isinstance(message, AssistantMessage):
                print("\nClaude: ", end="")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="")
                print()

        if message_count == 0:
            print("[WARNING] No messages received from Claude CLI!")

    # Or run the simple query example:
    asyncio.run(simple_query_example())
