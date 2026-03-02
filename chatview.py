import logging
import enum
import os

import asyncio
import threading
import xml
import sublime
import sublime_plugin

from . import plugin
from .genfoundry import (
    ClaudeCodeAgent, CodexAgent, AgentOptions, AssistantMessage, TextBlock,
    PermissionResultAllow, PermissionResultDeny)

# Constants for gutter highlights
PROMPT_HIGHLIGHT_KEY = "chatview_prompt_highlight"
PROMPT_HIGHLIGHT_SCOPE = "region.purplish"
PROMPT_HIGHLIGHT_ICON = "dot"
PROMPT_HIGHLIGHT_FLAGS = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.PERSISTENT


# logger by package name
LOG = logging.getLogger(__package__)

CHAT_VIEW_FLAG = "chatview_chat"
CHAT_INPUT_START = "chatview_input_start"
CHAT_WORKSPACE = "chatview_active_workspace"
CHAT_MODEL = "chatview_model"
CHAT_PLAN_MODE = "chatview_plan_mode"
CHAT_AGENT = "chatview_agent_provider"
CHAT_VIEW_NAME = "Chat View"
PACKAGE_NAME = "ChatView"
PROMPT_PREFIX = "\n❯ "

# Global store for active ChatSession: window_id -> ChatSession
chatview_clients = {}

class PlanMode(enum.Enum):
    FAST = "fast"
    PLANNING = "planning"


CHAT_APPROVE_MODE = "chatview_approve_mode"


class ApproveMode(enum.Enum):
    DEFAULT = "default"
    ALLOW_EDIT = "allow-edit"
    ACCEPT_ALL = "accept-all"


def plugin_loaded():
    """
    Called by Sublime Text when the plugin is loaded.
    """
    settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
    plugin.update_log_level(settings)
    # Defer scan to allow ST to finish restoring all scratch views
    sublime.set_timeout(_restore_chat_sessions, 500)


def _restore_chat_sessions():
    """Scan all windows for orphaned chat views and reconnect their agents."""
    for window in sublime.windows():
        for view in window.views():
            if (view.settings().get(CHAT_VIEW_FLAG, False) and
                    window.id() not in chatview_clients):
                _reconnect_chat_view(view)


def _reconnect_chat_view(view):
    """
    Reconnect an existing chat view to a new ChatSession after a restart.
    Appends a reconnection notice, but does not redraw historical phantoms.
    """
    window = view.window()
    if not window:
        return
    window_id = window.id()
    if window_id in chatview_clients:
        return

    cwd = get_best_dir(view)
    session = ChatSession(window, view, cwd)
    chatview_clients[window_id] = session
    # Restore the model phantom at the existing CHAT_INPUT_START position
    session.model_phantom.update(plan_mode=session.plan_mode)
    view.run_command("chat_output_append", {"text": "\n\n[Reconnected after restart]\n"})
    LOG.info(f"Reconnected ChatView agent for window {window_id}, cwd={cwd}")


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
        options = AgentOptions(
            cwd=self.cwd,
            cli_path=self.cli_path,
            api_key=self.anthropic_config.get("ANTHROPIC_API_KEY"),
            base_url=self.anthropic_config.get("ANTHROPIC_BASE_URL"),
            auth_token=self.anthropic_config.get("ANTHROPIC_AUTH_TOKEN"),
            model=self.anthropic_config.get("model"),
            can_use_tool=getattr(self, 'agent_options_callback', None),
            plan_mode=self.anthropic_config.get("plan_mode", False),
            allowed_tools=self.anthropic_config.get("allowed_tools"),
            approve_mode=self.anthropic_config.get("approve_mode")
        )

        agent_provider = self.anthropic_config.get("agent_provider", "claude")
        AgentClass = CodexAgent if agent_provider == "codex" else ClaudeCodeAgent

        try:
            async with AgentClass(options) as agent:
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

    async def _send_permission_response(self, request_id, response_data):
        """Internal async method to send a permission response."""
        if isinstance(self.agent, CodexAgent):
            # Codex agent: route through its approval response handler
            await self.agent.send_approval_response(request_id, response_data)
        else:
            # Claude agent: send control_response JSON
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": response_data
                }
            }
            try:
                await self.agent._write_json(response)
            except Exception as e:
                LOG.error(f"Failed to send permission response: {e}")

    def send_permission_response(self, request_id, response_data):
        """Schedule a permission response to be sent."""
        if self.loop and self.agent:
            asyncio.run_coroutine_threadsafe(
                self._send_permission_response(request_id, response_data),
                self.loop
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


class ModelPanel:
    """
    Displays the current model name as a phantom above the prompt area.
    """
    def __init__(self, view, window):
        self.view = view
        self.window = window
        self.phantom_set = sublime.PhantomSet(view, "chatview_model")

    def update(self, plan_mode=PlanMode.FAST):
        """Update the model phantom display."""
        input_start = self.view.settings().get(CHAT_INPUT_START, self.view.size())
        region = sublime.Region(input_start, input_start)

        agent_provider = self.window.settings().get(CHAT_AGENT, "claude")
        model = self.window.settings().get(f"chatview_model_{agent_provider}") or "default"
        # Keep the display key in sync
        self.window.settings().set(CHAT_MODEL, model)

        plan_tag_html = ""
        if plan_mode == PlanMode.PLANNING:
            plan_tag_html = """
                <a href="toggle_plan" class="model-tag" style="margin-left: 8px;">
                    <span class="label">PlanMode:</span>
                    <span class="value">planning</span>
                </a>
            """
        else:
            plan_tag_html = """
                <a href="toggle_plan" class="model-tag" style="margin-left: 8px;">
                    <span class="label">PlanMode:</span>
                    <span class="value">fast</span>
                </a>
            """

        html = f"""
        <body id="chatview-model" style="margin: 0; padding: 0;">
            <style>
                .model-row {{
                    margin: 0 0 6px 0;
                    padding: 8px 0;
                    border-bottom: 1px solid color(var(--foreground) alpha(0.06));
                }}
                .model-tag {{
                    color: var(--accent);
                    background-color: color(var(--accent) alpha(0.08));
                    border: 1px solid color(var(--accent) alpha(0.15));
                    font-size: 0.85em;
                    font-family: var(--font-mono);
                    text-decoration: none;
                    padding: 4px 6px;
                    border-radius: 4px;
                    line-height: 1.2;
                }}
                .model-tag:hover {{
                    background-color: color(var(--accent) alpha(0.15));
                    border-color: color(var(--accent) alpha(0.3));
                }}
                .icon {{
                    padding-right: 2px;
                }}
                .label {{
                    opacity: 0.7;
                    padding-right: 2px;
                }}
                .value {{
                    font-weight: bold;
                }}
            </style>
            <div class="model-row">
                <a href="set_agent" class="model-tag">
                    <span class="icon">✨</span>
                    <span class="label">Agent:</span>
                    <span class="value">{agent_provider}</span>
                </a>
                <a href="set_model" class="model-tag" style="margin-left: 8px;">
                    <span class="label">Model:</span>
                    <span class="value">{model}</span>
                </a>{plan_tag_html}
            </div>
        </body>
        """

        def on_navigate(href):
            if href == "set_agent":
                self.window.run_command("chat_view_set_agent")
            elif href == "set_model":
                self.window.run_command("chat_view_set_model")
            elif href == "toggle_plan":
                self.window.run_command("chat_view_toggle_plan_mode")

        self.phantom_set.update([sublime.Phantom(
            region,
            html,
            sublime.LAYOUT_BLOCK,
            on_navigate
        )])

    def clear(self):
        """Clear the model phantom."""
        self.phantom_set.update([])


class PermissionPanel:
    """
    Displays permission request phantoms for tool authorization.
    """
    def __init__(self, view, window, on_action):
        """
        :param view: Sublime View object.
        :param window: Sublime Window object.
        :param on_action: Callback function(request_id, action) for handling user actions.
        """
        self.view = view
        self.window = window
        self.on_action = on_action
        self.phantom_sets = {}  # Map of request_id -> PhantomSet
        self.diff_data = {}  # Map of request_id -> (old_text, new_text, name)

    def show(self, request_id, tool_name, input_data, approve_mode=None):
        """Show a permission phantom for a tool request."""
        input_start = self.view.settings().get(CHAT_INPUT_START, self.view.size())
        region = sublime.Region(input_start - 1, input_start)

        phantom_set = sublime.PhantomSet(self.view, f"permission_{request_id}")
        self.phantom_sets[request_id] = phantom_set

        def on_navigate(action):
            self._handle_navigate(request_id, action)

        # Prepare display content based on tool type
        display_content = self._prepare_display_content(request_id, tool_name, input_data)
        has_diff = request_id in self.diff_data

        html = self._build_html(request_id, tool_name, display_content, has_diff, approve_mode)
        phantom_set.update([sublime.Phantom(region, html, sublime.LAYOUT_BLOCK, on_navigate)])

        # Scroll to bottom to show request
        self.view.show(self.view.size())

    @staticmethod
    def _html_escape(text):
        """Escape HTML special characters in text."""
        return (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    def _prepare_display_content(self, request_id, tool_name, input_data):
        """Prepare the display content for a permission request."""
        if tool_name == "Edit":
            old_text = input_data.get("old_string", "")
            new_text = input_data.get("new_string", "")
            file_path = input_data.get("file_path", "unknown")
            name = os.path.basename(file_path)
            self.diff_data[request_id] = (old_text, new_text, name)
            return f'📄 <a href="show_diff" class="file-link">{name}</a>'

        elif tool_name == "ExitPlanMode":
            plan = input_data.get("plan", "")
            first_line = plan.split("\n")[0] if plan else "Empty Plan"
            self.diff_data[request_id] = ("", plan, "Implementation Plan")

            # Automatically open the plan in a new view
            def open_plan():
                plan_view = self.window.new_file()
                plan_view.set_name("Implementation Plan")
                plan_view.run_command("append", {"characters": plan})
                plan_view.set_syntax_file("Packages/Markdown/Markdown.sublime-syntax")
                plan_view.set_scratch(True)
            sublime.set_timeout(open_plan, 0)

            return (
                f'📄 <a href="show_plan" class="file-link">plan</a>\n\n'
                f'{first_line}'
            )

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
            self.diff_data[request_id] = (old_text, new_text, name)
            return f'📄 <a href="show_diff" class="file-link">{name}</a>'
        elif tool_name == "command_execution":
            command = input_data.get("command", "")
            cwd = input_data.get("cwd", "")
            display = self._html_escape(command)
            if cwd:
                display += f'\n<span style="opacity:0.6">cwd: {self._html_escape(cwd)}</span>'
            return display

        elif tool_name == "file_change":
            processed_diff = input_data.get("processed_diff")
            if not processed_diff:
                return f"<b>{tool_name}</b> (no changes)"

            old_text = processed_diff.get("old_text", "")
            new_text = processed_diff.get("new_text", "")
            display_name = processed_diff.get("display_name", "file")
            self.diff_data[request_id] = (old_text, new_text, display_name)

            files = processed_diff.get("files", [])
            count = processed_diff.get("count", 1)
            summary = f'📄 <a href="show_diff" class="file-link">{self._html_escape(display_name)}</a>'
            if count > 1:
                limit = 5
                details = "<br>".join([f"&nbsp;&nbsp;- {self._html_escape(f)}" for f in files[:limit]])
                if len(files) > limit:
                    details += f"<br>&nbsp;&nbsp;- ... and {len(files) - limit} more"
                return f"{summary}<br>{details}"
            return summary

        else:
            # Format input data line by line
            display_lines = []
            for k, v in input_data.items():
                if isinstance(v, str):
                    display_lines.append(f"{self._html_escape(k)}: {self._html_escape(v)}")
            return "\n".join(display_lines)

    def _build_html(self, request_id, tool_name, display_content, has_diff, approve_mode=None):
        """Build the HTML for the permission phantom."""
        allow_chat_btn = ""
        if approve_mode in (ApproveMode.DEFAULT.value, ApproveMode.ALLOW_EDIT.value):
            allow_chat_btn = '<a href="allow_chat" class="btn btn-chat">Allow for this chat</a>'

        return f"""
        <body id="permission-{request_id}">
            <style>
                .permission-box {{
                    background-color: color(var(--background) blend(var(--foreground) 92%));
                    padding: 10px 10px 15px 10px;
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
                    margin-top: 15px;
                    margin-bottom: 5px;
                }}
                .btn {{
                    display: inline-block;
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
                .btn-chat {{
                    background-color: color(var(--background) blend(var(--foreground) 75%));
                    color: var(--foreground);
                    margin-left: 30px;
                    border: 1px solid color(var(--foreground) alpha(0.2));
                }}
                .btn-diff {{
                    background-color: var(--accent);
                    color: var(--background);
                    margin-left: 10px;
                }}
            </style>
            <div class="permission-box">
                <div class="header">Tool Permission: {tool_name}</div>
                <div class="content">{display_content}</div>
                <div class="actions">
                    <a href="allow" class="btn btn-allow">Allow</a>
                    <a href="deny" class="btn btn-deny">Deny</a>
                    {allow_chat_btn}
                </div>
            </div>
        </body>
        """

    def _handle_navigate(self, request_id, action):
        """Handle navigation actions from the phantom."""
        if action == "show_diff":
            if request_id in self.diff_data:
                old_text, new_text, name = self.diff_data[request_id]
                plugin.show_diff(self.window, old_text, new_text, name)
            return
        elif action == "show_plan":
            if request_id in self.diff_data:
                _, plan, name = self.diff_data[request_id]
                plan_view = self.window.new_file()
                plan_view.set_name(name)
                plan_view.run_command("append", {"characters": plan})
                plan_view.set_syntax_file("Packages/Markdown/Markdown.sublime-syntax")
                plan_view.set_scratch(True)
            return

        # For allow/deny actions, delegate to the callback
        self.on_action(request_id, action)

    def clear(self, request_id):
        """Remove a permission phantom."""
        if request_id in self.phantom_sets:
            self.phantom_sets[request_id].update([])
            del self.phantom_sets[request_id]
        if request_id in self.diff_data:
            del self.diff_data[request_id]

    def clear_all(self):
        """Remove all permission phantoms."""
        for phantom_set in self.phantom_sets.values():
            phantom_set.update([])
        self.phantom_sets.clear()
        self.diff_data.clear()


class SelectPanel:
    """
    A generic UI component for showing a Sublime Quick Panel.
    Supports both single and multi-select modes.
    """
    def __init__(self, window, items, on_done, placeholder="", multi_select=False):
        """
        :param window: Sublime Window object.
        :param items: List of (label, description) tuples.
        :param on_done: Callback function(indices or None).
        :param placeholder: Text to show in the input.
        :param multi_select: Boolean, if True allows selecting multiple items.
        """
        self.window = window
        self.items = items
        self.on_done = on_done
        self.placeholder = placeholder
        self.multi_select = multi_select
        self.selected_indices = set()

    def show(self):
        """Show the quick panel."""
        display_items = []

        if self.multi_select:
            # Add Done option for multi-select
            display_items.append(sublime.QuickPanelItem("Done", "Finish selection", kind=sublime.KIND_ID_AMBIGUOUS))
            for i, (label, desc) in enumerate(self.items):
                prefix = "✅ " if i in self.selected_indices else "⬜ "
                display_items.append(sublime.QuickPanelItem(prefix + label, desc))
        else:
            # Single select items
            for label, desc in self.items:
                display_items.append(sublime.QuickPanelItem(label, desc))

        # Show panel
        flags = sublime.KEEP_OPEN_ON_FOCUS_LOST if self.multi_select else 0
        self.window.show_quick_panel(
            display_items,
            self._handle_done,
            flags=flags,
            placeholder=self.placeholder
        )

    def _handle_done(self, index):
        """Internal callback for the quick panel."""
        if index == -1:
            # User cancelled (Esc)
            self.on_done(None)
            return

        if self.multi_select:
            # Handle Done option (index 0)
            if index == 0:
                self.on_done(list(self.selected_indices))
                return

            # Toggle selection (adjust index for "Done" item)
            real_index = index - 1
            if real_index in self.selected_indices:
                self.selected_indices.remove(real_index)
            else:
                self.selected_indices.add(real_index)

            # Re-show panel to allow more selections
            sublime.set_timeout(self.show, 0)
        else:
            # Single select - Submit immediately
            self.on_done([index])


class AskUserQuestionHandler:
    """
    Handles the AskUserQuestion tool using SelectPanel.
    """
    def __init__(self, session, request_id, input_data):
        self.session = session
        self.request_id = request_id
        self.input_data = input_data

    def run(self):
        """Start the question flow."""
        questions = self.input_data.get("questions", [])
        if not questions:
            self._cleanup()
            return

        # Currently only handling the first question as per original implementation
        question_data = questions[0]
        options = question_data.get("options", [])
        items = [(opt.get("label", ""), opt.get("description", "")) for opt in options]
        multi_select = question_data.get("multiSelect", False)
        question_text = question_data.get("question", "")

        def on_done(indices):
            if indices is None:
                # User cancelled (Esc) - Deny permission
                self.session.send_permission_response(self.request_id, {
                    "behavior": "deny",
                    "message": "User cancelled selection"
                })
            else:
                # Submit selections
                self._submit(indices, question_data)
            self._cleanup()

        SelectPanel(
            self.session.window,
            items,
            on_done,
            placeholder=question_text,
            multi_select=multi_select
        ).show()

    def _submit(self, indices, question_data):
        """Submit the selected options back to the agent."""
        options = question_data.get("options", [])
        selected_labels = [options[i]["label"] for i in indices if i < len(options)]

        answers = {}
        if selected_labels:
            question_text = question_data.get("question", "")
            multi_select = question_data.get("multiSelect", False)
            if multi_select:
                answers[question_text] = ", ".join(selected_labels)
            else:
                answers[question_text] = selected_labels[0]

        # Update the input_data with answers
        updated_input = self.input_data.copy()
        updated_input["answers"] = answers

        self.session.send_permission_response(self.request_id, {
            "behavior": "allow",
            "updatedInput": updated_input
        })

    def _cleanup(self):
        """Remove the request from the session's pending list."""
        if self.request_id in self.session.permission_requests:
            del self.session.permission_requests[self.request_id]


class ChatMessageProcessor:
    """
    Handles buffering, formatting, and displaying messages from the agent.
    """
    def __init__(self, session):
        self.session = session
        self.markdown_formatter = plugin.MarkdownFormatter()
        self.last_is_tool_call = False

    def handle_message(self, message):
        """Dispatch agent message to appropriate handler."""
        # Handle error strings passed from thread wrapper
        if message == "error":
            pass

        # Check for error tuple/custom protocol from AgentThread
        if isinstance(message, tuple) and message[0] == "error":
            self.append_error(message[1])
            self.session.stop_loading()
            return

        # Check for reset_complete message
        if isinstance(message, tuple) and message[0] == "reset_complete":
            sublime.status_message(message[1])
            LOG.info("Session reset completed successfully")
            return

        # Handle Claude Agent Message objects
        if hasattr(message, "type"):
            if message.type == "assistant":
                self.session.start_loading()

                # Extract text content
                text_content = ""
                if hasattr(message, "content"):
                    for block in message.content:
                        if hasattr(block, "text"):
                            if self.last_is_tool_call:
                                text_content += "\n"
                            self.last_is_tool_call = False

                            text_content += block.text

                        elif isinstance(block, dict) and block.get("type") == "tool_use":
                            if not self.last_is_tool_call:
                                text_content += "\n"
                            self.last_is_tool_call = True
                            text_content += self._format_tool_block(block)

                if text_content:
                    self.append_content(text_content + "\n")
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
                    self.session.permission_requests[request_id] = (tool_name, input_data)

                    self.session.show_permission_phantom(request_id, tool_name, input_data)

            elif message.type == "user":
                if (isinstance(message.content["content"], str) and
                    message.content["content"].startswith("<local-command-stdout>")):
                    local_output = xml.etree.ElementTree.fromstring(message.content["content"])
                    # local_output.tag is 'local-command-stdout'
                    self.append_content(local_output.text)

            elif message.type == "tool_use":
                if not self.last_is_tool_call:
                    self.append_content("\n")
                self.last_is_tool_call = True
                self.append_content(self._format_tool_block(message.content) + "\n")

            elif message.type == "error":
                self.append_error(message.content)
                self.session.stop_loading()

            elif message.type == "result":
                # Flush markdown formatter buffer
                self.append_content("", flush=True)
                # Stop loading on turn completion (heuristic)
                self.session.stop_loading()
                self.append_content("\n")

            elif message.type == "stop":
                # Codex agent sends "stop" when its process completes
                self.append_content("", flush=True)
                self.session.stop_loading()
                self.append_content("\n")

            elif message.type == "control_response":
                if hasattr(message, "content") and isinstance(message.content, dict):
                    response_outer = message.content.get("response", {})
                    if response_outer.get("subtype") == "success":
                        response_data = response_outer.get("response", {})
                        models = response_data.get("models", [])
                        if models:
                            self.session.available_models = models

            elif message.type == "models_update":
                if hasattr(message, "content") and isinstance(message.content, dict):
                    models = message.content.get("models", [])
                    if models:
                        self.session.available_models = models

    def _format_tool_block(self, block):
        """Format a tool use block into a string."""
        name = block.get("name")
        input_data = block.get("input", {})

        if name == "Read":
            file_path = input_data.get("file_path", "")
            if file_path:
                try:
                    rel_path = os.path.relpath(file_path, self.session.agent_thread.cwd)
                except Exception:
                    rel_path = file_path

                # Extract offset and limit for line number display
                offset = input_data.get("offset")
                limit = input_data.get("limit")

                if offset is not None and limit is not None:
                    start_line = offset
                    end_line = offset + limit - 1
                    return f"⏺ Read {rel_path}#L{start_line}-L{end_line}"
                elif offset is not None:
                    return f"⏺ Read {rel_path}#L{offset}"
                else:
                    return f"⏺ Read {rel_path}"

        elif name == "Bash":
            command = input_data.get("command", "")
            if command:
                return f"⏺ Bash {command}"

        elif name in ("Write", "Edit"):
            file_path = input_data.get("file_path", "")
            if file_path:
                try:
                    rel_path = os.path.relpath(file_path, self.session.agent_thread.cwd)
                except Exception:
                    rel_path = file_path
                return f"⏺ {name} {rel_path}"
        elif name in ("Grep", "Glob"):
            pattern = input_data.get("pattern")
            if pattern:
                return f"⏺ {name} {pattern}"
        elif name == "WebFetch":
            url = input_data.get("url", "")
            if url:
                return f"⏺ WebFetch {url}"
        elif name == "WebSearch":
            query = input_data.get("query", "")
            if query:
                return f"⏺ WebSearch ({query})"
        elif name == "command_execution":
            command = block.get("command", "")
            if command:
                return f"⏺ command ({command})"
            return "⏺ command"
        elif name == "file_change":
            filenames = block.get("filenames", [])
            if filenames:
                return f"⏺ file_change ({', '.join(filenames)})"
            return "⏺ file_change"
        else:
            return f"⏺ {name}" if name else ""

        return ""

    def append_content(self, text, flush=False):
        """Format and append text to the chat view."""
        formatted_text = self.markdown_formatter.format(text, flush=flush)
        if formatted_text:
            sublime.set_timeout(
                lambda: self.session.chat_view.run_command("chat_output_append",
                    {"text": formatted_text}),
                0
            )

    def append_error(self, error_msg):
        """Append error message to chat view."""
        sublime.set_timeout(
            lambda: self.session.chat_view.run_command("chat_output_append",
                {"text": f"\\n\\nError: {error_msg}\\n"}),
            0
        )


class ChatSession:
    """
    Manages the state and UI for a single ChatView session.
    """
    def __init__(self, window, view, cwd):
        self.window = window
        self.chat_view = view
        self.loading_animation = LoadingAnimation(self.chat_view)
        self.model_phantom = ModelPanel(self.chat_view, self.window)
        self.permission_panel = PermissionPanel(
            self.chat_view, self.window, self._handle_permission_decision
        )
        self.history = []
        self.history_index = 0
        self.history_stash = ""
        self.permission_requests = {} # Map of request_id -> (tool_name, input_data)
        self.available_models = []  # Will be populated from control_response
        self.prompt_regions = [] # List of Regions for submitted prompts
        self.session_allow_all = False

        self.message_processor = ChatMessageProcessor(self)

        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")

        # Determine agent provider early
        agent_provider = self.window.settings().get(CHAT_AGENT, settings.get("agent_provider", "claude"))

        # Load cli_path from settings (provider-specific only, no fallback to avoid mixing CLIs)
        cli_path = settings.get(f"{agent_provider}_command")
        if not cli_path:
            cli_path = None  # Let the agent class find its own CLI via shutil.which()

        # Use provider-specific model key so switching agents won't carry over incompatible models
        model = self.window.settings().get(f"chatview_model_{agent_provider}") or None

        anthropic_config = {
            "ANTHROPIC_API_KEY": settings.get("ANTHROPIC_API_KEY"),
            "ANTHROPIC_BASE_URL": settings.get("ANTHROPIC_BASE_URL"),
            "ANTHROPIC_AUTH_TOKEN": settings.get("ANTHROPIC_AUTH_TOKEN"),
            "model": model,
            "plan_mode": self.window.settings().get(CHAT_PLAN_MODE) == PlanMode.PLANNING.value,
            "allowed_tools": settings.get("allowed_tools"),
            "agent_provider": agent_provider,
            "approve_mode": self.window.settings().get(CHAT_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value)
        }

        # Initialize background agent thread
        self.agent_thread = AgentThread(
            cwd, self._handle_agent_message, cli_path=cli_path, anthropic_config=anthropic_config
        )
        self.agent_thread.start()

    def show_permission_phantom(self, request_id, tool_name, input_data):
        """Show a phantom asking for permission."""
        if tool_name == "AskUserQuestion":
            self.handle_ask_user_question(request_id, input_data)
            return

        if self.session_allow_all:
            self._auto_approve(request_id, input_data)
            return

        approve_mode = self.window.settings().get(CHAT_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value)

        if approve_mode == ApproveMode.ACCEPT_ALL.value:
            self._auto_approve(request_id, input_data)
            return

        risky_tools = ("Bash",)
        if approve_mode == ApproveMode.ALLOW_EDIT.value and tool_name not in risky_tools:
            self._auto_approve(request_id, input_data)
            return

        self.permission_panel.show(request_id, tool_name, input_data, approve_mode=approve_mode)

    def _auto_approve(self, request_id, input_data):
        """Auto-approve a permission request without showing a phantom."""
        if request_id in self.permission_requests:
            response_data = {
                "behavior": "allow",
                "updatedInput": input_data
            }
            self.send_permission_response(request_id, response_data)
            del self.permission_requests[request_id]

    def handle_ask_user_question(self, request_id, input_data):
        """Handle AskUserQuestion tool using Quick Panel."""
        handler = AskUserQuestionHandler(self, request_id, input_data)
        handler.run()

    def clear_permission_phantom(self, request_id):
        """Remove the permission phantom."""
        self.permission_panel.clear(request_id)

    def _handle_permission_decision(self, request_id, action):
        """Handle allow/deny decision from PermissionPanel."""
        LOG.info(f"Permission decision: {action} for request {request_id}")

        if request_id in self.permission_requests:
            tool_name, input_data = self.permission_requests[request_id]

            if action == "allow":
                response_data = {
                    "behavior": "allow",
                    "updatedInput": input_data
                }
            elif action == "allow_chat":
                self.session_allow_all = True
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

    def send_permission_response(self, request_id, response_data):
        """Send a control response back to the agent via agent_thread."""
        if self.agent_thread:
            self.agent_thread.send_permission_response(request_id, response_data)

    def _handle_agent_message(self, message):
        """Handle messages received from the agent thread."""
        self.message_processor.handle_message(message)

    def start_loading(self):
        """Start the loading animation."""
        sublime.set_timeout(lambda: self.loading_animation.start(self.loading_region), 0)

    def stop_loading(self):
        sublime.set_timeout(lambda: self.loading_animation.stop(), 0)

    def loading_region(self):
        """Get the region where the loading animation should be displayed."""
        input_start = self.chat_view.settings().get(CHAT_INPUT_START, self.chat_view.size())
        return sublime.Region(input_start-1, input_start)

    def stop(self):
        self.loading_animation.stop()
        self.model_phantom.clear()
        self.permission_panel.clear_all()
        if self.agent_thread:
            self.agent_thread.stop()

    @property
    def plan_mode(self):
        val = self.window.settings().get(CHAT_PLAN_MODE, PlanMode.FAST.value)
        try:
            return PlanMode(val)
        except ValueError:
            return PlanMode.FAST

    @plan_mode.setter
    def plan_mode(self, value):
        if isinstance(value, PlanMode):
            self.window.settings().set(CHAT_PLAN_MODE, value.value)
        else:
            self.window.settings().set(CHAT_PLAN_MODE, value)

    def send_input(self, user_input, region=None):
        """Start animation and send to agent."""
        if region:
            self.add_prompt_highlight(region)
        self.agent_thread.send(user_input)

    def add_prompt_highlight(self, region):
        """Add a gutter highlight to the specified prompt region."""
        self.prompt_regions.append(region)
        self.chat_view.add_regions(
            PROMPT_HIGHLIGHT_KEY,
            self.prompt_regions,
            PROMPT_HIGHLIGHT_SCOPE,
            PROMPT_HIGHLIGHT_ICON,
            PROMPT_HIGHLIGHT_FLAGS
        )

    def clear_prompt_highlights(self):
        """Clear all prompt gutter highlights."""
        self.prompt_regions = []
        self.chat_view.erase_regions(PROMPT_HIGHLIGHT_KEY)

    def reset_session(self):
        """Reset the chat session by restarting the agent and notifying in UI."""
        # Stop any ongoing loading animation
        self.stop_loading()
        self.clear_prompt_highlights()
        self.session_allow_all = False

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
                # Reconnect if session was lost (e.g., after a restart)
                if self.window.id() not in chatview_clients:
                    _reconnect_chat_view(view)
                if initial_msg:
                    view.run_command("chat_input_prompt", {"text": initial_msg})
                return

        # Create a new view
        chat_view = self.window.new_file()
        chat_view.set_name(CHAT_VIEW_NAME)
        chat_view.set_scratch(True)
        chat_view.set_syntax_file(f"Packages/{PACKAGE_NAME}/ChatMD.sublime-syntax")

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
        session.send_input(user_input, region=input_region)
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
    def on_load(self, view):
        """
        Reconnect a restored chat view when it is loaded asynchronously
        (e.g., after a Sublime Text restart).
        """
        if not view.settings().get(CHAT_VIEW_FLAG, False):
            return
        window = view.window()
        if window and window.id() not in chatview_clients:
            sublime.set_timeout(lambda: _reconnect_chat_view(view), 100)

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


class ChatInputPromptCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        self.view.insert(edit, self.view.size(), "\n\n\n")
        self.view.settings().set(CHAT_INPUT_START, self.view.size())

        # Update model phantom at new position
        window = self.view.window()
        if window and window.id() in chatview_clients:
            session = chatview_clients[window.id()]
            session.model_phantom.update(plan_mode=session.plan_mode)

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


class ChatViewSetModelListHandler(sublime_plugin.ListInputHandler):
    def name(self):
        return "model"

    def list_items(self):
        # Get the active ChatSession to access available_models
        window = sublime.active_window()
        if window:
            window_id = window.id()
            if window_id in chatview_clients:
                session = chatview_clients[window_id]
                if session.available_models:
                    # Return list of tuples: (display_text, value)
                    return [
                        (f"{m['displayName']} - {m['description']}", m['value'])
                        for m in session.available_models
                    ]

        return []

    def placeholder(self):
        return "Select a model"

    def description(self, value, text):
        return f"Set Model: {value}"


class ChatViewSetModelTextHandler(sublime_plugin.TextInputHandler):
    def name(self):
        return "model"

    def placeholder(self):
        return "Enter model name (e.g., sonnet, opus, haiku)"

    def description(self, text):
        return "Set Model: " + text if text else "Set Model Name"

    def validate(self, text):
        return bool(text.strip())


class ChatViewAgentProviderInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, current_agent=None):
        self.current_agent = current_agent

    def name(self):
        return "agent"

    def list_items(self):
        items = [
            ("claude: (Claude Code CLI by Anthropic)", "claude"),
            ("codex: (Codex CLI by OpenAI)", "codex"),
        ]

        if self.current_agent:
            for i, item in enumerate(items):
                if item[1] == self.current_agent:
                    items.insert(0, items.pop(i))
                    break

        return items

    def placeholder(self):
        return "Select agent provider"

    def description(self, agent, text):
        return f"Agent: {agent}"


class ChatViewSetAgentCommand(sublime_plugin.WindowCommand):
    """
    Sets the agent provider for ChatView sessions in the current window.
    """
    def run(self, agent):
        if agent:
            self.window.settings().set(CHAT_AGENT, agent)
            sublime.status_message(f"ChatView agent provider set to: {agent}")

            # Update the model phantom if session exists
            window_id = self.window.id()
            if window_id in chatview_clients:
                session = chatview_clients[window_id]
                session.model_phantom.update(plan_mode=session.plan_mode)

    def input(self, args):
        current_agent = self.window.settings().get(CHAT_AGENT, "claude")
        return ChatViewAgentProviderInputHandler(current_agent)


class ChatViewSetModelCommand(sublime_plugin.WindowCommand):
    """
    Sets the model for ChatView sessions in the current window.
    """
    def run(self, model):
        if model:
            # Store model under provider-specific key
            agent_provider = self.window.settings().get(CHAT_AGENT, "claude")
            self.window.settings().set(f"chatview_model_{agent_provider}", model.strip())
            # Also update the display key
            self.window.settings().set(CHAT_MODEL, model.strip())
            sublime.status_message(f"ChatView model set to: {model}")

            # Update the model phantom if session exists
            window_id = self.window.id()
            if window_id in chatview_clients:
                session = chatview_clients[window_id]
                session.model_phantom.update(plan_mode=session.plan_mode)
                # For Codex agent, update the running agent directly
                if agent_provider == "codex":
                    agent_thread = session.agent_thread
                    if agent_thread and agent_thread.agent:
                        agent_thread.agent.set_model(model.strip())

    def input(self, args):
        # Check if ChatSession has available models
        window_id = self.window.id()
        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            if session.available_models:
                # Use ListInputHandler for dropdown selection
                return ChatViewSetModelListHandler()

        # Fallback to TextInputHandler for manual input
        return ChatViewSetModelTextHandler()


class ChatViewPlanModeInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, current_mode=None):
        self.current_mode = current_mode

    def name(self):
        return "mode"

    def list_items(self):
        items = [
            ("fast: (executing task straightly, complete faster)", PlanMode.FAST.value),
            ("planning: (make plan and todo-list before execute)", PlanMode.PLANNING.value),
        ]

        if self.current_mode:
            for i, item in enumerate(items):
                if item[1] == self.current_mode:
                    items.insert(0, items.pop(i))
                    break

        return items

    def placeholder(self):
        return "Select mode"

    def description(self, mode, text):
        return f"Plan Mode: {mode}"


class ChatViewApproveModeInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, current_mode=None):
        self.current_mode = current_mode

    def name(self):
        return "mode"

    def list_items(self):
        items = [
            ("default: ask for confirmation on tool call", ApproveMode.DEFAULT.value),
            ("allow-edit: auto-approve file edits", ApproveMode.ALLOW_EDIT.value),
            ("accept-all: accept all without asking", ApproveMode.ACCEPT_ALL.value),
        ]

        if self.current_mode:
            # Find the item with the current mode and move it to the front
            for i, item in enumerate(items):
                if item[1] == self.current_mode:
                    items.insert(0, items.pop(i))
                    break

        return items

    def placeholder(self):
        if self.current_mode:
            return f" ( {self.current_mode} ); select approve mode on tool call"
        return "select approve mode on tool call"


class ChatViewSetApproveModeCommand(sublime_plugin.WindowCommand):
    """Set permission approve mode for the current ChatView session."""
    def run(self, mode):
        self.window.settings().set(CHAT_APPROVE_MODE, mode)
        sublime.status_message(f"Approve mode set to: {mode}")

    def input(self, args):
        if "mode" not in args:
            current_mode = self.window.settings().get(CHAT_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value)
            return ChatViewApproveModeInputHandler(current_mode)
        return None


class ChatViewTogglePlanModeCommand(sublime_plugin.WindowCommand):
    """
    Toggle plan mode for the current ChatView session.
    """
    def run(self, mode):
        # mode is a string value from PlanMode
        if mode == PlanMode.PLANNING.value:
            plan_mode_enum = PlanMode.PLANNING
        else:
            plan_mode_enum = PlanMode.FAST

        self.window.settings().set(CHAT_PLAN_MODE, plan_mode_enum.value)

        window_id = self.window.id()
        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            session.model_phantom.update(plan_mode=plan_mode_enum)

        status = "planning" if plan_mode_enum == PlanMode.PLANNING else "fast"
        sublime.status_message(f"Plan mode set to: {status}")

    def input(self, args):
        if "mode" not in args:
            current_mode = self.window.settings().get(CHAT_PLAN_MODE, PlanMode.FAST.value)
            return ChatViewPlanModeInputHandler(current_mode)
        return None


class ChatViewPermissionActionCommand(sublime_plugin.WindowCommand):
    """
    Handle permission actions from the phantom UI.
    """
    def run(self, action, request_id):
        window_id = self.window.id()
        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            session.handle_permission_action(request_id, action)

