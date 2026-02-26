"""
Codex Agent SDK Client - Standard Library Implementation
A client for interacting with Codex Agent using raw standard libraries.
"""

import asyncio
import json
import os
import shutil
import sys
import logging
from typing import Optional, Dict, Any, AsyncIterator, List

LOG = logging.getLogger(__package__)

from .base_agent import MessageType, Message, TextBlock, AssistantMessage, AgentOptions, BaseAgent


class CodexAgent(BaseAgent):
    """
    Client for interacting with Codex CLI ("codex exec --json").

    The Codex CLI execution model is per-turn:
    - We spawn a new subprocess for each message.
    - We pass "codex exec" (or "codex resume <thread_id>")
    - The input is written to stdin and stdin is closed.
    - The CLI streams JSON events to stdout.
    - We parse the ThreadEvent streams into unified standard Message objects.
    """

    def __init__(self, options: Optional[AgentOptions] = None):
        super().__init__(options)
        self.thread_id: Optional[str] = None
        self.cli_path = self.options.cli_path or shutil.which("codex")
        self._is_connected = False

        # We process messages per turn, using a queue for each turn
        self._message_queue: asyncio.Queue = asyncio.Queue()
        # The background read task for the current turn
        self._current_run_task: Optional[asyncio.Task] = None
        self._current_process: Optional[asyncio.subprocess.Process] = None

        if not self.cli_path:
            raise FileNotFoundError(
                "Codex CLI not found in PATH. Please install it or set 'codex_command' in settings."
            )
        LOG.info(f"Codex CLI path: {self.cli_path}")

    async def connect(self, prompt: Optional[str] = None) -> None:
        """
        Connect to Codex. Because it's a per-turn execution, 'connecting'
        just means we are ready. If there's an initial prompt, we send it.
        """
        if self._is_connected:
            raise RuntimeError("Client is already connected")

        self._is_connected = True

        if prompt:
            await self.send_message(prompt)

    async def send_message(self, content: str, parent_tool_use_id: Optional[str] = None) -> None:
        """
        Send a user message to Codex by spawning the CLI process.
        """
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        # Cancel any previous run that might still somehow be hanging
        if self._current_run_task and not self._current_run_task.done():
            self._current_run_task.cancel()

        cmd = [self.cli_path, "exec", "--json"]

        if self.thread_id:
            # "resume" only accepts: --config, --last, SESSION_ID, PROMPT
            # Model/sandbox settings are inherited from the original session.
            cmd.extend(["resume", self.thread_id])
        else:
            # First turn: set model, sandbox, and other exec-only options
            if self.options.model:
                cmd.extend(["--model", self.options.model])
            if self.options.sandbox_mode:
                cmd.extend(["--sandbox", self.options.sandbox_mode])
            if self.options.system_prompt:
                LOG.warning("system_prompt is not yet directly supported as an arg by codex CLI in this implementation")
            cmd.append("--skip-git-repo-check")

        LOG.info(f"Running Codex command: {' '.join(cmd)}")
        # We start a run.
        self._current_run_task = asyncio.create_task(
            self._run_codex_cli(cmd, content)
        )

    async def _run_codex_cli(self, cmd: List[str], input_text: str) -> None:
        """Spawn the codex CLI, pass input, read output."""
        env = os.environ.copy()

        if self.options.api_key is not None:
            env["CODEX_API_KEY"] = self.options.api_key
        if self.options.base_url is not None:
            env["OPENAI_BASE_URL"] = self.options.base_url

        try:
            self._current_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.options.cwd
            )

            # Write input and close stdin
            if self._current_process.stdin:
                self._current_process.stdin.write(input_text.encode('utf-8'))
                await self._current_process.stdin.drain()
                self._current_process.stdin.close()

            # Read streaming output
            if self._current_process.stdout:
                while True:
                    line = await self._current_process.stdout.readline()
                    if not line:
                        break

                    line_str = line.decode('utf-8').strip()
                    if not line_str:
                        continue

                    try:
                        data = json.loads(line_str)
                        # Process Codex ThreadEvent
                        await self._process_codex_event(data)
                    except json.JSONDecodeError:
                        LOG.debug(f"codex non-json output: {line_str}")

            # Read remaining stderr if any
            if self._current_process.stderr:
                stderr_data = await self._current_process.stderr.read()
                if stderr_data:
                    stderr_str = stderr_data.decode('utf-8', errors='replace').strip()
                    if stderr_str:
                        LOG.error(f"Codex stderr: {stderr_str}")

            # Wait for exit
            await self._current_process.wait()
            exit_code = self._current_process.returncode
            LOG.info(f"Codex process exited with code {exit_code}")

            if exit_code != 0:
                # If we had no success events but a failure exit code, notify user
                LOG.error(f"Codex CLI failed with exit code {exit_code}")

            # End of turn
            await self._message_queue.put(Message(MessageType.STOP.value))

        except asyncio.CancelledError:
            if self._current_process and self._current_process.returncode is None:
                self._current_process.terminate()
            raise
        except Exception as e:
            LOG.error(f"Error running codex: {e}")
            await self._message_queue.put(Message(MessageType.ERROR.value, content=str(e)))
            await self._message_queue.put(Message(MessageType.STOP.value))

    async def _process_codex_event(self, event: Dict[str, Any]) -> None:
        """Parse Codex ThreadEvent and map to BaseAgent Message."""
        event_type = event.get("type")

        if event_type == "thread.started":
            self.thread_id = event.get("thread_id")

        elif event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type")

            if item_type == "agent_message":
                text = item.get("text", "")
                blocks = [TextBlock(text)]
                msg = AssistantMessage(content=blocks, msg_id=item.get("id"))
                await self._message_queue.put(msg)

            elif item_type == "mcp_tool_call":
                # Convert to standard tool_use format
                tool_msg = Message(
                    MessageType.TOOL_USE.value,
                    content={
                        "name": f"{item.get('server')}/{item.get('tool')}",
                        "input": item.get('arguments', {}),
                        "result": item.get('result', {}),
                        "error": item.get('error', {}),
                        "status": item.get("status")
                    },
                    msg_id=item.get("id")
                )
                await self._message_queue.put(tool_msg)

            elif item_type == "command_execution":
                tool_msg = Message(
                    MessageType.TOOL_USE.value,
                    content={
                        "name": "command_execution",
                        "command": item.get("command"),
                        "output": item.get("aggregated_output"),
                        "exit_code": item.get("exit_code"),
                        "status": item.get("status")
                    },
                    msg_id=item.get("id")
                )
                await self._message_queue.put(tool_msg)

            elif item_type == "error":
                err_msg = Message(
                    MessageType.ERROR.value,
                    content=item.get("message", "Unknown error"),
                    msg_id=item.get("id")
                )
                await self._message_queue.put(err_msg)

        elif event_type == "turn.failed":
            error_data = event.get("error", {})
            err_msg = Message(
                MessageType.ERROR.value,
                content=error_data.get("message", "Turn failed unexpectedly")
            )
            await self._message_queue.put(err_msg)

        elif event_type == "error":
            err_msg = Message(
                MessageType.ERROR.value,
                content=event.get("message", "Thread Error")
            )
            await self._message_queue.put(err_msg)

    async def receive_messages(self) -> AsyncIterator[Message]:
        """
        Yields messages from the agent across all turns.
        Keeps running until disconnect, matching ClaudeAgent behavior so that
        chatview._receive_messages never exits prematurely.
        """
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        while self._is_connected:
            try:
                message = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=0.1
                )
                yield message

            except asyncio.TimeoutError:
                if self._current_run_task and self._current_run_task.done():
                    # Task is done but no STOP message in queue (maybe crashed)
                    if self._message_queue.empty():
                        self._current_run_task = None
                        yield Message(MessageType.STOP.value)
                continue

    async def disconnect(self) -> None:
        """Disconnect and cleanup resources"""
        self._is_connected = False

        if self._current_run_task and not self._current_run_task.done():
            self._current_run_task.cancel()
            try:
                await self._current_run_task
            except asyncio.CancelledError:
                pass

        if self._current_process and self._current_process.returncode is None:
            self._current_process.terminate()
            try:
                await asyncio.wait_for(self._current_process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._current_process.kill()
                await self._current_process.wait()


async def query(
    prompt: str,
    options: Optional[AgentOptions] = None
) -> AsyncIterator[Message]:
    """
    Query Codex for one-shot interactions.
    """
    if options is None:
        options = AgentOptions()

    client = CodexAgent(options=options)

    try:
        await client.connect(prompt=prompt)

        async for message in client.receive_messages():
            yield message

            msg_type = getattr(message, 'type', None)
            if msg_type == MessageType.STOP.value:
                break
    finally:
        await client.disconnect()


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async def chat(client, prompt):
        print(f"\n[User] {prompt}")
        await client.send_message(prompt)
        async for msg in client.receive_messages():
            if getattr(msg, 'type', None) == "assistant":
                for b in msg.content:
                    print(f"  Codex: {b.text if hasattr(b, 'text') else b}")
            elif getattr(msg, 'type', None) == MessageType.STOP.value:
                break

    async def test():
        async with CodexAgent() as client:
            await chat(client, "Tell me a random joke.")
            await chat(client, "Make it shorter.")

    asyncio.run(test())

