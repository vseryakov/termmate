import logging
import os

import asyncio
import threading
import xml
import sublime
import sublime_plugin

from . import plugin
from .genfoundry.claude_agent import (
    ClaudeCodeAgent, ClaudeAgentOptions, AssistantMessage, TextBlock,
    PermissionResultAllow, PermissionResultDeny)

# logger by package name
LOG = logging.getLogger(__package__)

CHAT_VIEW_FLAG = "chatview_chat"
CHAT_INPUT_START = "chatview_input_start"
CHAT_WORKSPACE = "chatview_active_workspace"
CHAT_MODEL = "chatview_model"
CHAT_VIEW_NAME = "Chat View"
PROMPT_PREFIX = "\n❯ "
chatview_clients = {}

def plugin_loaded():
    """
    Called by Sublime Text when the plugin is loaded.
    """
    settings = sublime.load_settings("ChatView.sublime-settings")
    plugin.update_log_level(settings)


def get_best_dir(view):
    window = view.window()
    if window:
        # Check for explicitly set workspace
        custom_cwd = window.settings().get(CHAT_WORKSPACE)
        if custom_cwd and os.path.isdir(custom_cwd):
            return custom_cwd

        folders = window.folders()
        if folders:
            return folders[0]
    return ""


class LoadingAnimation:
    """
    Manages a loading animation phantom with start/stop control.
    """
    def __init__(self, view):
        self.view = view
        self.phantom_set = sublime.PhantomSet(view, "chatview_loading")
        self.is_loading = False
        self.frame_index = 0
        self.frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def start(self, region):
        """Start the loading animation at the specified region."""
        # ALWAYS update the region provider, even if already loading
        self.region_provider = region

        if not self.is_loading:
            self.is_loading = True
            self.frame_index = 0
            self._update_animation()

    def stop(self):
        """Stop the loading animation and clear the phantom."""
        self.is_loading = False
        # Clear on next tick to avoid thread issues if called from background
        sublime.set_timeout(lambda: self.phantom_set.update([]), 0)

    def _update_animation(self):
        """Update the loading animation frame."""
        if not self.is_loading:
            return

        # Resolve current region
        if callable(self.region_provider):
            region = self.region_provider()
        else:
            region = self.region_provider

        frame = self.frames[self.frame_index % len(self.frames)]

        html = f"""
        <body id="chatview-loading" style="background-color: transparent; margin: 0; padding: 0;">
            <style>
                .loading {{
                    color: var(--accent);
                    background-color: transparent;
                    font-weight: bold;
                    margin-right: 8px;
                    font-family: var(--font-mono);
                }}
            </style>
            <div class="loading">{frame}</div>
        </body>
        """

        self.phantom_set.update([sublime.Phantom(
            region,
            html,
            sublime.LAYOUT_BLOCK
        )])

        # Schedule next frame
        self.frame_index += 1
        sublime.set_timeout(lambda: self._update_animation(), 100)


class AgentThread(threading.Thread):
    """
    Background thread to run the asyncio Claude Agent.
    """
    def __init__(self, cwd, on_message, cli_path=None, anthropic_config=None):
        super().__init__()
        self.cwd = cwd
        self.on_message = on_message
        self.cli_path = cli_path
        self.anthropic_config = anthropic_config or {}
        self.loop = None
        self.agent = None
        self.input_queue = None
        self.running = True
        self.daemon = True

    def run(self):
        """Run the asyncio loop."""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.input_queue = asyncio.Queue()
            self.loop.run_until_complete(self._agent_loop())
        finally:
            if self.loop:
                self.loop.close()

    async def _agent_loop(self):
        """Main async loop for the agent."""
        options = ClaudeAgentOptions(
            cwd=self.cwd,
            cli_path=self.cli_path,
            api_key=self.anthropic_config.get("ANTHROPIC_API_KEY"),
            base_url=self.anthropic_config.get("ANTHROPIC_BASE_URL"),
            auth_token=self.anthropic_config.get("ANTHROPIC_AUTH_TOKEN"),
            model=self.anthropic_config.get("model"),
            can_use_tool=getattr(self, 'agent_options_callback', None)
        )

        try:
            async with ClaudeCodeAgent(options) as agent:
                self.agent = agent

                # Create tasks for reading inputs and handling agent messages
                input_task = asyncio.create_task(self._process_inputs())
                receive_task = asyncio.create_task(self._receive_messages())

                # Wait until we are stopped
                while self.running:
                    await asyncio.sleep(0.1)

                # Cleanup
                input_task.cancel()
                receive_task.cancel()
                try:
                    await input_task
                    await receive_task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            LOG.error(f"Agent error: {e}")
            error_msg = str(e)
            sublime.set_timeout(lambda: self.on_message(("error", error_msg)), 0)

    async def _process_inputs(self):
        """Read from input queue and send to agent."""
        while self.running:
            text = await self.input_queue.get()
            if text:
                await self.agent.send_message(text)

    async def _receive_messages(self):
        """Read messages from agent and callback."""
        async for message in self.agent.receive_messages():
            if not self.running:
                break

            # Dispatch to main thread
            sublime.set_timeout(lambda m=message: self.on_message(m), 0)

    async def _reset_agent(self):
        """Disconnect and reconnect the agent to clear conversation context."""
        try:
            # Disconnect current session
            await self.agent.disconnect()
            LOG.info("Agent disconnected for reset")

            # Reconnect to start fresh session
            await self.agent.connect()
            LOG.info("Agent reconnected with fresh session")

            # Notify UI that reset is complete
            sublime.set_timeout(
                lambda: self.on_message(("reset_complete", "Session reset successfully")), 0
            )
        except Exception as e:
            LOG.error(f"Error resetting agent: {e}")
            sublime.set_timeout(
                lambda: self.on_message(("error", f"Failed to reset session: {str(e)}")), 0
            )

    def send(self, text):
        """Queue input to be sent."""
        if self.loop and self.input_queue:
            LOG.debug(f"Send to agent msg: {text}")
            self.loop.call_soon_threadsafe(self.input_queue.put_nowait, text)

    def stop(self):
        """Signal thread to stop."""
        self.running = False

    def reset(self):
        """Reset the agent session by disconnecting and reconnecting."""
        if self.loop and self.agent:
            # Schedule the reset in the agent's event loop
            asyncio.run_coroutine_threadsafe(self._reset_agent(), self.loop)


class ModelPhantom:
    """
    Displays the current model name as a phantom above the prompt area.
    """
    def __init__(self, view, window):
        self.view = view
        self.window = window
        self.phantom_set = sublime.PhantomSet(view, "chatview_model")

    def update(self):
        """Update the model phantom display."""
        input_start = self.view.settings().get(CHAT_INPUT_START, self.view.size())
        region = sublime.Region(input_start, input_start)

        model = self.window.settings().get(CHAT_MODEL) or "default"

        html = f"""
        <body id="chatview-model" style="margin: 0; padding: 0;">
            <style>
                .model-row {{
                    background-color: color(var(--background) blend(var(--foreground) 90%));
                    padding: 4px 8px;
                    margin: 0;
                    border-bottom: 1px solid color(var(--foreground) alpha(0.1));
                }}
                .model-tag {{
                    color: color(var(--foreground) alpha(0.8));
                    background-color: color(var(--accent) alpha(0.2));
                    font-size: 0.85em;
                    font-family: var(--font-mono);
                    text-decoration: none;
                    padding: 3px 8px;
                    border-radius: 3px;
                }}
                .model-tag:hover {{
                    color: var(--foreground);
                    background-color: color(var(--accent) alpha(0.35));
                }}
            </style>
            <div class="model-row">
                <a href="set_model" class="model-tag">Model: {model}</a>
            </div>
        </body>
        """

        def on_navigate(href):
            if href == "set_model":
                self.window.run_command("chat_view_set_model")

        self.phantom_set.update([sublime.Phantom(
            region,
            html,
            sublime.LAYOUT_BLOCK,
            on_navigate
        )])

    def clear(self):
        """Clear the model phantom."""
        self.phantom_set.update([])


class ChatSession:
    """
    Manages the state and UI for a single ChatView session.
    """
    def __init__(self, window, view, cwd):
        self.window = window
        self.chat_view = view
        self.loading_animation = LoadingAnimation(self.chat_view)
        self.model_phantom = ModelPhantom(self.chat_view, self.window)
        self.history = []
        self.history_index = 0
        self.history_stash = ""
        self.permission_phantoms = {} # Map of request_id -> PhantomSet
        self.permission_requests = {} # Map of request_id -> (tool_name, input_data)
        self.permission_diff_data = {} # Map of request_id -> (old_text, new_text, name)

        # Load cli_path from settings
        settings = sublime.load_settings("ChatView.sublime-settings")
        cli_path = settings.get("agent_command")
        if not cli_path:
            cli_path = None

        anthropic_config = {
            "ANTHROPIC_API_KEY": settings.get("ANTHROPIC_API_KEY"),
            "ANTHROPIC_BASE_URL": settings.get("ANTHROPIC_BASE_URL"),
            "ANTHROPIC_AUTH_TOKEN": settings.get("ANTHROPIC_AUTH_TOKEN"),
            "model": self.window.settings().get(CHAT_MODEL)
        }

        # Initialize background agent thread
        self.agent_thread = AgentThread(
            cwd, self._handle_agent_message, cli_path=cli_path, anthropic_config=anthropic_config
        )
        self.agent_thread.start()

    def show_permission_phantom(self, request_id, tool_name, input_data):
        """Show a phantom asking for permission."""
        input_start = self.chat_view.settings().get(CHAT_INPUT_START, self.chat_view.size())
        region = sublime.Region(input_start-1, input_start)

        phantom_set = sublime.PhantomSet(self.chat_view, f"permission_{request_id}")
        self.permission_phantoms[request_id] = phantom_set

        # Resolve request_id for the callback
        def on_navigate(action):
            self.handle_permission_action(request_id, action)

        # Detect Write/Edit tools and store diff data
        has_diff = False
        if tool_name == "Edit":
            old_text = input_data.get("old_string", "")
            new_text = input_data.get("new_string", "")
            file_path = input_data.get("file_path", "unknown")
            name = os.path.basename(file_path)
            self.permission_diff_data[request_id] = (old_text, new_text, name)
            has_diff = True
        elif tool_name == "Write":
            file_path = input_data.get("file_path", "")
            new_text = input_data.get("content", "")
            old_text = ""
            if file_path and os.path.exists(file_path):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        old_text = f.read()
                except Exception as e:
                    LOG.error(f"Failed to read file for diff: {e}")
            name = os.path.basename(file_path) if file_path else "new_file"
            self.permission_diff_data[request_id] = (old_text, new_text, name)
            has_diff = True

        # Prepare display content
        if has_diff:
            display_content = f'📄 <a href="show_diff" class="file-link">{name}</a>'
        else:
            # Format input data line by line
            display_lines = []
            for k, v in input_data.items():
                if isinstance(v, str):
                    display_lines.append(f"{k}: {v}")
            display_content = "\n".join(display_lines)

        html = f"""
        <body id="permission-{request_id}">
            <style>
                .permission-box {{
                    background-color: color(var(--background) blend(var(--foreground) 92%));
                    padding: 10px;
                    border: 1px solid var(--accent);
                    border-radius: 4px;
                    margin: 10px 0;
                }}
                .header {{
                    font-weight: bold;
                    color: var(--accent);
                    margin-bottom: 5px;
                }}
                .content {{
                    font-family: var(--font-mono);
                    font-size: 0.9em;
                    white-space: pre-wrap;
                    margin-bottom: 20px;
                }}
                .file-link {{
                    text-decoration: none;
                    color: var(--accent);
                    font-weight: bold;
                    font-size: 1.1em;
                    background-color: color(var(--background) blend(var(--foreground) 85%));
                    padding: 2px 6px;
                    border-radius: 3px;
                }}
                .actions {{
                    display: block;
                    margin-top: 10px;
                }}
                .btn {{
                    text-decoration: none;
                    padding: 4px 8px;
                    border-radius: 3px;
                    font-weight: bold;
                }}
                .btn-allow {{
                    background-color: var(--greenish);
                    color: var(--background);
                }}
                .btn-deny {{
                    background-color: var(--redish);
                    color: var(--background);
                    margin-left: 10px;
                }}
                .btn-diff {{
                    background-color: var(--accent);
                    color: var(--background);
                    margin-left: 10px;
                }}
            </style>
            <div class="permission-box">
                <div class="header">Tool Permission Request: {tool_name}</div>
                <div class="content">{display_content}</div>
                <div class="actions">
                    <a href="allow" class="btn btn-allow">Allow</a>
                    <a href="deny" class="btn btn-deny">Deny</a>
                </div>
            </div>
        </body>
        """

        phantom_set.update([sublime.Phantom(region, html, sublime.LAYOUT_BLOCK, on_navigate)])

        # Scroll to bottom to show request
        self.chat_view.show(self.chat_view.size())

    def clear_permission_phantom(self, request_id):
        """Remove the permission phantom."""
        if request_id in self.permission_phantoms:
            self.permission_phantoms[request_id].update([])
            del self.permission_phantoms[request_id]

    def handle_permission_action(self, request_id, action):
        """Handle allow/deny action from UI."""
        LOG.info(f"Permission action: {action} for request {request_id}")

        if action == "show_diff":
            if request_id in self.permission_diff_data:
                old_text, new_text, name = self.permission_diff_data[request_id]
                plugin.show_diff(self.window, old_text, new_text, name)
            return

        if request_id in self.permission_requests:
            tool_name, input_data = self.permission_requests[request_id]
            response_data = {}

            if action == "allow":
                # Assuming simple allow logic where we pass back input_data
                response_data = {
                    "behavior": "allow",
                    "updatedInput": input_data
                }
            else:
                response_data = {
                    "behavior": "deny",
                    "message": "User denied permission via UI"
                }

            self.send_permission_response(request_id, response_data)

            # Cleanup
            self.clear_permission_phantom(request_id)
            del self.permission_requests[request_id]
            if request_id in self.permission_diff_data:
                del self.permission_diff_data[request_id]

    def send_permission_response(self, request_id, response_data):
        """Send a control response back to the agent."""
        if self.agent_thread and self.agent_thread.loop and self.agent_thread.agent:
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": response_data
                }
            }

            async def send():
                try:
                    await self.agent_thread.agent._write_json(response)
                except Exception as e:
                    LOG.error(f"Failed to send permission response: {e}")

            asyncio.run_coroutine_threadsafe(send(), self.agent_thread.loop)

    def _handle_agent_message(self, message):
        """Handle messages received from the agent thread."""

        # LOG.info(f"agent message {message}")
        # Handle error strings passed from thread wrapper
        if message == "error":
            # The actual error message is passed as second arg in the thread,
            # but here we might receive the raw tuple or just be careful.
            # actually my AgentThread passes ("error", str(e))
            # but let's fix that signature in AgentThread
            pass

        # Check for error tuple/custom protocol from AgentThread
        if isinstance(message, tuple) and message[0] == "error":
            self.chat_view.run_command("chat_output_append", {"text": f"\n\nError: {message[1]}\n"})
            self.stop_loading()
            return

        # Check for reset_complete message
        if isinstance(message, tuple) and message[0] == "reset_complete":
            sublime.status_message(message[1])
            LOG.info("Session reset completed successfully")
            return

        # Handle Claude Agent Message objects
        if hasattr(message, "type"):
            if message.type == "assistant":
                sublime.set_timeout(lambda: self.loading_animation.start(self.loading_region), 0)

                # Extract text content
                text_content = ""
                if hasattr(message, "content"):
                    for block in message.content:
                        if hasattr(block, "text"):
                            text_content += block.text
                        elif isinstance(block, dict) and block.get("type") == "tool_use":
                            if block.get("name") == "Read":
                                file_path = block.get("input", {}).get("file_path", "")
                                if file_path:
                                    try:
                                        rel_path = os.path.relpath(file_path, self.agent_thread.cwd)
                                    except Exception:
                                        rel_path = file_path
                                    text_content += f"\n🟣 Read ({rel_path})\n"
                            elif block.get("name") == "Bash":
                                command = block.get("input", {}).get("command", "")
                                if command:
                                    text_content += f"\n🟣 Bash ({command})\n"
                            elif block.get("name") in ("Write", "Edit"):
                                tool_name = block.get("name")
                                file_path = block.get("input", {}).get("file_path", "")
                                if file_path:
                                    try:
                                        rel_path = os.path.relpath(file_path, self.agent_thread.cwd)
                                    except Exception:
                                        rel_path = file_path
                                    text_content += f"\n🟣 {tool_name} ({rel_path})\n"

                if text_content:
                    self.on_chat_content(text_content + "\n")
            elif message.type == "system":
                if hasattr(message, "content") and isinstance(message.content, dict):
                    session_id = message.content.get("session_id")
                    if session_id and message.content.get("subtype") == "init":
                        LOG.info(f"system session_id: {session_id}")

            elif message.type == "control_request":
                # Handle permission request directly
                request = message.content.get("request", {})
                subtype = request.get("subtype")
                if subtype == "can_use_tool":
                    tool_name = request.get("tool_name")
                    input_data = request.get("input", {})
                    request_id = message.content.get("request_id")

                    # Store input data for later use in response
                    self.permission_requests[request_id] = (tool_name, input_data)

                    self.show_permission_phantom(request_id, tool_name, input_data)

            elif message.type == "user":
                if (isinstance(message.content["content"], str) and
                    message.content["content"].startswith("<local-command-stdout>")):
                    local_output = xml.etree.ElementTree.fromstring(message.content["content"])
                    # local_output.tag is 'local-command-stdout'
                    self.on_chat_content(local_output.text)

            elif message.type == "error":
                self.chat_view.run_command(
                    "chat_output_append", {"text": f"\n\nError: {message.content}\n"}
                )
                self.stop_loading()

            elif message.type == "result":
                # Stop loading on turn completion (heuristic)
                self.stop_loading()
                self.on_chat_content("\n")

    def stop_loading(self):
        sublime.set_timeout(lambda: self.loading_animation.stop(), 0)

    def on_chat_content(self, text):
        sublime.set_timeout(
            lambda: self.chat_view.run_command("chat_output_append", {"text": text}), 0
        )

    def loading_region(self):
        """Get the region where the loading animation should be displayed."""
        input_start = self.chat_view.settings().get(CHAT_INPUT_START, self.chat_view.size())
        return sublime.Region(input_start-1, input_start)

    def stop(self):
        self.loading_animation.stop()
        self.model_phantom.clear()
        if self.agent_thread:
            self.agent_thread.stop()

    def send_input(self, user_input):
        """Start animation and send to agent."""
        self.agent_thread.send(user_input)

    def reset_session(self):
        """Reset the chat session by restarting the agent and notifying in UI."""
        # Stop any ongoing loading animation
        self.stop_loading()

        # Show reset message in the history
        reset_msg = "\n\nChatView session reset...\n"
        self.chat_view.run_command("chat_output_append", {"text": reset_msg})

        cwd = get_best_dir(self.chat_view)
        if cwd:
            self.chat_view.run_command("chat_output_append", {"text": f"cwd: {cwd}\n\n"})

        # Reset the agent (disconnect and reconnect)
        self.agent_thread.reset()


class ChatViewCliCommand(sublime_plugin.WindowCommand):
    """
    A Sublime Text plugin command for calling the ChatView
    """
    def run(self, initial_msg="", send_immediate=False):
        # Try to find and focus existing chat view
        for view in self.window.views():
            if view.settings().get(CHAT_VIEW_FLAG, False):
                self.window.focus_view(view)
                if initial_msg:
                    view.run_command("chat_input_prompt", {"text": initial_msg})
                return

        # Create a new view
        chat_view = self.window.new_file()
        chat_view.set_name(CHAT_VIEW_NAME)
        chat_view.set_scratch(True)
        chat_view.set_syntax_file("Packages/Markdown/Markdown.sublime-syntax")

        chat_view.settings().set("draw_minimap", False)
        chat_view.settings().set("line_numbers", False)
        chat_view.settings().set("word_wrap", True)
        chat_view.settings().set(CHAT_VIEW_FLAG, True)

        shortcut = "Command+Enter" if sublime.platform() == "osx" else "Control+Enter"
        welcome_text = "\nType your message and press %s to send.\n\n" % shortcut

        chat_view.run_command("append", {"characters": "Starting ChatView CLI session...\n"})
        cwd = get_best_dir(chat_view)
        if cwd:
            chat_view.run_command("append", {"characters": f"cwd: {cwd}\n"})
        chat_view.run_command("append", {"characters": welcome_text})

        # Set input start position
        chat_view.settings().set(CHAT_INPUT_START, chat_view.size())

        # Create and start the ChatSession
        session = ChatSession(self.window, chat_view, cwd)
        window_id = self.window.id()
        chatview_clients[window_id] = session

        # Show initial prompt (this will also update the model phantom)
        chat_view.run_command("chat_input_prompt", {"text": initial_msg})


class ChatViewSendInputCommand(sublime_plugin.TextCommand):
    """
    Handles the input submission (bound to Ctrl+Enter).
    """
    def run(self, edit):
        window = self.view.window()
        if not window:
            return

        window_id = window.id()
        if window_id not in chatview_clients:
            sublime.status_message("No active ChatView session found")
            return

        input_start = self.view.settings().get(CHAT_INPUT_START, 0)
        input_region = sublime.Region(input_start + len(PROMPT_PREFIX), self.view.size())
        user_input = self.view.substr(input_region).strip()

        if not user_input:
            return

        sublime.status_message("Sending message...")

        # Show input text and next prompt (simulated local echo/confirmation)
        self.view.run_command("chat_input_prompt", {"text": ""})

        # Send to session
        session = chatview_clients[window_id]
        session.history.append(user_input)
        session.history_index = len(session.history)
        session.history_stash = ""
        session.send_input(user_input)
        LOG.info(f"User enter prompt {user_input}")


class ChatViewHistoryUpCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        window = self.view.window()
        if not window or window.id() not in chatview_clients:
            return

        session = chatview_clients[window.id()]
        input_start = self.view.settings().get(CHAT_INPUT_START, 0)
        editable_start = input_start + len(PROMPT_PREFIX)

        # History navigation
        if session.history_index == len(session.history):
            # Stash current input
            current_input_region = sublime.Region(editable_start, self.view.size())
            session.history_stash = self.view.substr(current_input_region)

        if session.history_index > 0:
            session.history_index -= 1
            self._replace_input(edit, session.history[session.history_index], editable_start)

    def _replace_input(self, edit, text, start_point):
        region = sublime.Region(start_point, self.view.size())
        self.view.replace(edit, region, text)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(self.view.size()))
        self.view.show(self.view.size())


class ChatViewHistoryDownCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        window = self.view.window()
        if not window or window.id() not in chatview_clients:
            return

        session = chatview_clients[window.id()]

        # History navigation
        if session.history_index < len(session.history):
            session.history_index += 1

            text_to_show = ""
            if session.history_index == len(session.history):
                text_to_show = session.history_stash
            else:
                text_to_show = session.history[session.history_index]

            input_start = self.view.settings().get(CHAT_INPUT_START, 0)
            editable_start = input_start + len(PROMPT_PREFIX)
            self._replace_input(edit, text_to_show, editable_start)

    def _replace_input(self, edit, text, start_point):
        region = sublime.Region(start_point, self.view.size())
        self.view.replace(edit, region, text)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(self.view.size()))
        self.view.show(self.view.size())


class ChatViewListener(sublime_plugin.EventListener):
    def on_close(self, view):
        """
        Cleanup session when the chat view is closed.
        """
        if view.name() == CHAT_VIEW_NAME:
            window = view.window()
            if window is None:
                window = sublime.active_window()

            if window is not None:
                window_id = window.id()
                if window_id in chatview_clients:
                    try:
                        chatview_clients[window_id].stop()
                    except Exception:
                        pass
                    del chatview_clients[window_id]
                    LOG.info(f"Exit ChatView CLI for window {window_id}")
            LOG.info("ChatView closed")

    def on_selection_modified(self, view):
        """
        Restrict cursor movement to the editable area.
        Allows selecting history for copy, but prevents placing the caret in history.
        """
        if not view.settings().get(CHAT_VIEW_FLAG, False) and view.name() != CHAT_VIEW_NAME:
            return
        if not view.settings().has(CHAT_INPUT_START):
            return

        input_start = view.settings().get(CHAT_INPUT_START, 0)
        editable_start = input_start + len(PROMPT_PREFIX)

        new_sel = []
        changed = False

        for sel in view.sel():
            # Only restrict empty regions (cursor carets), allowing user to select history to copy
            if sel.empty() and sel.begin() < editable_start:
                new_sel.append(sublime.Region(editable_start))
                changed = True
            else:
                new_sel.append(sel)

        if changed:
            view.sel().clear()
            view.sel().add_all(new_sel)


    def _redirect_cursor(self, view):
        """Helper to move cursor to the end of the view."""
        end_pos = view.size()
        view.sel().clear()
        view.sel().add(sublime.Region(end_pos))
        view.show(end_pos)

    def on_text_command(self, view, command_name, args):
        """Intercept text commands to protect content before prompt area."""
        # Only monitor ChatView chat views
        if not view.settings().get(CHAT_VIEW_FLAG, False) and view.name() != CHAT_VIEW_NAME:
            return None

        input_start = view.settings().get(CHAT_INPUT_START, 0)
        editable_start = input_start + len(PROMPT_PREFIX)

        # Handle move commands for history navigation
        if command_name == "move" and args and args.get("by") == "lines":
            is_up = not args.get("forward", True)
            if len(view.sel()) > 0:
                sel = view.sel()[0]
                if sel.empty():
                    if is_up:
                        row_sel, _ = view.rowcol(sel.begin())
                        row_start, _ = view.rowcol(editable_start)
                        if row_sel == row_start:
                            return ("chat_view_history_up", {})
                    else:
                        row_sel, _ = view.rowcol(sel.end())
                        row_last, _ = view.rowcol(view.size())
                        if row_sel == row_last:
                            return ("chat_view_history_down", {})

        # Handle deletion commands - block if they affect content before prompt
        delete_commands = ("left_delete", "right_delete", "delete_word", "delete_word_backward",
                          "delete_to_mark", "run_macro_file", "cut",)

        if command_name in delete_commands:
            for sel in view.sel():
                # Block deletion if cursor is in protected area
                if sel.begin() < editable_start:
                    self._redirect_cursor(view)
                    return ("noop", {})

                # Special case for backspace: if at the exact boundary,
                # it deletes backward into protected area
                if (command_name in ("left_delete", "delete_word_backward") and
                    sel.empty() and sel.begin() == editable_start):
                    self._redirect_cursor(view)
                    return ("noop", {})

        # Handle insert/modification commands - redirect to end if in protected area
        mod_commands = ("insert", "paste", "insert_characters", "insert_snippet",
                       "append", "yank", "paste_and_indent", "clipboard_history_paste")

        if command_name in mod_commands:
            should_redirect = False
            for sel in view.sel():
                if sel.begin() < editable_start:
                    should_redirect = True
                    break

            if should_redirect:
                self._redirect_cursor(view)
                return ("noop", {})

        return None

    def on_query_completions(self, view, prefix, locations):
        """
        Provide filename completions when typing '@' in the prompt area.
        Shows three categories: open files, current directory files, and subdirectories.
        """
        if not view.settings().get(CHAT_VIEW_FLAG, False):
            return None

        # Check if in editable area
        input_start = view.settings().get(CHAT_INPUT_START, 0)
        editable_start = input_start + len(PROMPT_PREFIX)
        pos = locations[0]

        if pos < editable_start:
            return None

        # Check if the prefix is preceded by '@'
        trigger_pos = pos - len(prefix) - 1
        if trigger_pos < 0 or view.substr(trigger_pos) != '@':
            return None

        completions = []
        window = view.window()
        if not window:
            return None

        # Get current directory (first workspace folder)
        current_dir = None
        folders = window.folders()
        if folders:
            current_dir = folders[0]

        # Category 1: Currently open files
        seen_files = set()
        for v in window.views():
            file_path = v.file_name()
            if not file_path:
                continue

            # Skip the chat view itself
            if v.settings().get(CHAT_VIEW_FLAG, False):
                continue

            file_name = os.path.basename(file_path)
            if file_name in seen_files:
                continue

            seen_files.add(file_name)

            # Use relative path as hint if available
            rel_path = file_name
            if current_dir and file_path.startswith(current_dir):
                rel_path = os.path.relpath(file_path, current_dir)

            completions.append(sublime.CompletionItem(
                file_name,
                annotation=f"📂 {rel_path}",
                completion=file_name,
                kind=sublime.KIND_VARIABLE
            ))

        # Category 2: Files in current directory
        if current_dir and os.path.isdir(current_dir):
            try:
                for item in os.listdir(current_dir):
                    item_path = os.path.join(current_dir, item)
                    if os.path.isfile(item_path) and not item.startswith('.'):
                        if item not in seen_files:
                            seen_files.add(item)
                            completions.append(sublime.CompletionItem(
                                item,
                                annotation="📄 current dir",
                                completion=item,
                                kind=sublime.KIND_AMBIGUOUS
                            ))
            except OSError:
                pass

        # Category 3: Subdirectories in current directory
        if current_dir and os.path.isdir(current_dir):
            try:
                for item in os.listdir(current_dir):
                    item_path = os.path.join(current_dir, item)
                    if os.path.isdir(item_path) and not item.startswith('.'):
                        completions.append(sublime.CompletionItem(
                            item + "/",
                            annotation="📁 subdirectory",
                            completion=item + "/",
                            kind=sublime.KIND_NAMESPACE
                        ))
            except OSError:
                pass

        return sublime.CompletionList(completions, flags=sublime.INHIBIT_WORD_COMPLETIONS)

    def on_modified_async(self, view):
        """
        Trigger autocompletion immediately when '@' is typed.
        """
        if not view.settings().get(CHAT_VIEW_FLAG, False):
            return

        # Check if the last character typed was '@'
        sel = view.sel()
        if not sel:
            return

        pos = sel[0].begin()
        if pos <= 0:
            return

        # Check if in editable area
        input_start = view.settings().get(CHAT_INPUT_START, 0)
        editable_start = input_start + len(PROMPT_PREFIX)
        if pos < editable_start:
            return

        last_char = view.substr(pos - 1)
        if last_char == '@':
            # Run auto_complete command
            view.run_command("auto_complete", {
                "disable_auto_insert": True,
                "api_completions_only": True,
                "next_completion_if_showing": False
            })


class ChatOutputAppendCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        input_start = self.view.settings().get(CHAT_INPUT_START, 0) - 1
        inserted = self.view.insert(edit, input_start, text)
        new_pos = input_start + inserted
        self.view.settings().set(CHAT_INPUT_START, new_pos+1)
        self.view.show(self.view.size())

        # Update model phantom at new position
        # window = self.view.window()
        # if window and window.id() in chatview_clients:
        #     session = chatview_clients[window.id()]
        #     session.model_phantom.update()


class ChatInputPromptCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        self.view.insert(edit, self.view.size(), "\n\n\n")
        self.view.settings().set(CHAT_INPUT_START, self.view.size())

        # Update model phantom at new position
        window = self.view.window()
        if window and window.id() in chatview_clients:
            session = chatview_clients[window.id()]
            session.model_phantom.update()

        # Next input prompt
        self.view.insert(edit, self.view.size(), PROMPT_PREFIX)
        if text:
            self.view.insert(edit, self.view.size(), text + " ")
        end = self.view.size()
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(end))
        self.view.show(end)


class ChatViewAddContextCommand(sublime_plugin.TextCommand):
    """
    Command to add current file context to the ChatView chat prompt.
    """
    def run(self, edit):
        view = self.view
        window = view.window()
        if not window:
            return

        file_path = view.file_name()
        if not file_path:
            return

        # Get line numbers (1-based)
        sel = view.sel()[0]
        row_start, _ = view.rowcol(sel.begin())
        row_end, _ = view.rowcol(sel.end())

        # Format as @file_path#L(A)-(B)
        # Handle single line selection vs range
        if row_start == row_end:
            context_tag = f"@{file_path}#L{row_start + 1}"
        else:
            context_tag = f"@{file_path}#L{row_start + 1}-{row_end + 1}"

        # Find or create ChatView chat view
        chat_view = None
        for v in window.views():
            if v.settings().get(CHAT_VIEW_FLAG, False):
                chat_view = v
                break

        if not chat_view:
            # If no chat view, create one and pass the context tag immediately
            window.run_command("chat_view_cli", {"initial_msg": context_tag})
        else:
            window.focus_view(chat_view)
            self._insert_tag(chat_view, context_tag)

    def _insert_tag(self, chat_view, context_tag):
        # Insert at the end of the view (current prompt area)
        end_pos = chat_view.size()
        chat_view.run_command("insert", {"characters": context_tag + " "})
        # Move cursor to end
        chat_view.sel().clear()
        chat_view.sel().add(sublime.Region(chat_view.size()))
        chat_view.show(chat_view.size())


class ChatViewPromptHandler(sublime_plugin.TextInputHandler):
    def name(self):
        return "prompt"

    def placeholder(self):
        return "Enter your prompt for ChatView..."

    def description(self, text):
        return "ChatView: " + text if text else "ChatView Prompt"


class ChatViewPromptCommand(sublime_plugin.WindowCommand):
    def run(self, prompt):
        if not prompt:
            return

        # Try to find existing chat view
        chat_view = None
        for v in self.window.views():
            if v.settings().get(CHAT_VIEW_FLAG, False):
                chat_view = v
                break

        if chat_view:
            self.window.focus_view(chat_view)
            chat_view.run_command("insert", {"characters": prompt})
            chat_view.run_command("chat_view_send_input")
        else:
            # Start a new session and send immediately
            self.window.run_command("chat_view_cli", {
                "initial_msg": prompt,
                "send_immediate": True
            })

    def input(self, args):
        return ChatViewPromptHandler()


class ChatViewSetWorkspaceInputHandler(sublime_plugin.TextInputHandler):
    def name(self):
        return "path"

    def placeholder(self):
        return "Enter workspace path..."

    def description(self, text):
        return "Set WorkSpace: " + text if text else "Set WorkSpace Path"

    def validate(self, text):
        return os.path.isdir(os.path.expanduser(text))


class ChatViewSetWorkspaceInputCommand(sublime_plugin.WindowCommand):
    """
    Command that asks for input and then calls ChatViewSetWorkspaceCommand.
    """
    def run(self, path):
        if path:
            full_path = os.path.expanduser(path)
            # Delegate to the existing command
            self.window.run_command("chat_view_set_workspace", {"dirs": [full_path]})

    def input(self, args):
        return ChatViewSetWorkspaceInputHandler()


class ChatViewSetWorkspaceCommand(sublime_plugin.WindowCommand):
    """
    Sets the active workspace for ChatView based on the selected folder in sidebar.
    """
    def run(self, files=[], dirs=[]):
        # Handle both files and dirs arguments, though typically called with dirs from sidebar
        paths = files + dirs
        LOG.info(f"set workspace path {paths}")
        if not paths:
            return

        # Find the first valid directory
        target_dir = None
        for path in paths:
            if os.path.isdir(path):
                target_dir = path
                break
            else:
                # If it's a file, use its parent directory
                parent = os.path.dirname(path)
                if os.path.isdir(parent):
                    target_dir = parent
                    break

        if target_dir:
            self.window.settings().set(CHAT_WORKSPACE, target_dir)
            sublime.status_message(f"ChatView Dir set to: {target_dir}")
        else:
            sublime.status_message("No valid directory for ChatView Workspace")

    def is_visible(self, files=[], dirs=[]):
        # Show only if at least one item is selected
        return bool(files or dirs)


class ChatViewClearSessionCommand(sublime_plugin.WindowCommand):
    """
    Clears the current chat session by disconnecting and reconnecting the agent.
    This resets the conversation history similar to Claude Code's reset session.
    """
    def run(self):
        window_id = self.window.id()
        if window_id not in chatview_clients:
            sublime.status_message("No active ChatView session found")
            return

        # Reset the session (disconnect and reconnect agent)
        session = chatview_clients[window_id]
        session.reset_session()
        sublime.status_message("Resetting chat session...")
        LOG.info("Resetting chat session via disconnect/reconnect")

    def is_enabled(self):
        # Only enable if there's an active session
        return self.window.id() in chatview_clients


class ChatViewSetModelInputHandler(sublime_plugin.TextInputHandler):
    def name(self):
        return "model"

    def placeholder(self):
        return "Select a model(sonnet, opus, haiku)"

    def description(self, text):
        return "Set Model: " + text if text else "Set Model Name"

    def validate(self, text):
        return bool(text.strip())


class ChatViewSetModelCommand(sublime_plugin.WindowCommand):
    """
    Sets the model for ChatView sessions in the current window.
    """
    def run(self, model):
        if model:
            self.window.settings().set(CHAT_MODEL, model.strip())
            sublime.status_message(f"ChatView model set to: {model}")

            # Update the model phantom if session exists
            window_id = self.window.id()
            if window_id in chatview_clients:
                session = chatview_clients[window_id]
                session.model_phantom.update()

    def input(self, args):
        return ChatViewSetModelInputHandler()


class ChatViewPermissionActionCommand(sublime_plugin.WindowCommand):
    """
    Handle permission actions from the phantom UI.
    """
    def run(self, action, request_id):
        window_id = self.window.id()
        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            session.handle_permission_action(request_id, action)

