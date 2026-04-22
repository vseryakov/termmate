import difflib
import logging
import os
import xml.etree.ElementTree

import sublime

from . import plugin

LOG = logging.getLogger(__package__)

class BaseChatMessageProcessor:
    """
    Handles buffering, formatting, and displaying messages from the agent.
    """
    def __init__(self, session):
        self.session = session
        self.markdown_formatter = plugin.MarkdownFormatter()
        self.last_is_tool_call = False
        self._plan_text = ""

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
            LOG.info("Session reset completed successfully")
            return

        if hasattr(message, "type"):
            self._handle_typed_message(message)

    def _handle_typed_message(self, message):
        raise NotImplementedError

    def _render_diff_block(self, diff_text):
        """Wraps diff text in an indented markdown code block."""
        if not diff_text:
            return ""
        block_content = f"```diff\n{diff_text.rstrip()}\n```"
        return "\n".join(" " + line for line in block_content.splitlines())

    def _format_tool_block(self, block):
        name = block.get("name")
        return f"⏺ {name}" if name else ""

    def append_content(self, text, flush=False):
        """Format and append text to the chat view."""
        formatted_text = self.markdown_formatter.format(text, flush=flush)
        if formatted_text:
            sublime.set_timeout(
                lambda: self.session.chat_view.run_command("term_chat_output_append",
                    {"text": formatted_text}),
                0
            )

    def append_error(self, error_msg):
        """Append error message to chat view."""
        sublime.set_timeout(
            lambda: self.session.chat_view.run_command("term_chat_output_append",
                {"text": f"\\n\\nError: {error_msg}\\n"}),
            0
        )

class ClaudeMessageProcessor(BaseChatMessageProcessor):
    def _handle_typed_message(self, message):
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
                    # Store session_id in view settings for persistence across restarts
                    self.session.set_view_session_id(self.session.chat_view, session_id)

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
                self.append_content(local_output.text)

        elif message.type == "error":
            self.append_error(message.content)
            self.session.stop_loading()

        elif message.type == "result":
            # Flush markdown formatter buffer
            self.append_content("", flush=True)
            # Stop loading on turn completion (heuristic)
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

        elif name in ("Agent", "Task"):
            description = input_data.get("description", "")
            subagent_type = input_data.get("subagent_type", "")

            parts = [f"⏺ {name}"]
            if subagent_type:
                parts.append(subagent_type)
            if description:
                parts.append(description)

            return " ".join(parts)

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
                header = f"⏺ {name} {rel_path}"

                # Render diff for Edit
                if name == "Edit" and "old_string" in input_data and "new_string" in input_data:
                    old_lines = input_data["old_string"].splitlines(keepends=True)
                    new_lines = input_data["new_string"].splitlines(keepends=True)

                    diff_lines = list(difflib.unified_diff(
                        old_lines, new_lines,
                        fromfile=f"a/{rel_path}",
                        tofile=f"b/{rel_path}"
                    ))

                    if len(diff_lines) > 2:
                        # Skip --- and +++ lines
                        diff_text = "".join(diff_lines[2:])
                        return header + "\n\n" + self._render_diff_block(diff_text)

                return header
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

        return f"⏺ {name}" if name else ""

class CodexMessageProcessor(BaseChatMessageProcessor):
    def _handle_typed_message(self, message):
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

            if text_content:
                self.append_content(text_content + "\n")

        elif message.type == "tool_use":
            if not self.last_is_tool_call:
                self.append_content("\n")
            self.last_is_tool_call = True
            self.append_content(self._format_tool_block(message.content) + "\n")

        elif message.type == "error":
            self.append_error(message.content)
            self.session.stop_loading()

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

        elif message.type == "thread_started":
            if hasattr(message, "content") and isinstance(message.content, dict):
                session_id = message.content.get("session_id")
                if session_id:
                    LOG.info(f"Codex thread session_id: {session_id}")
                    # Store session_id in view settings for persistence across restarts
                    self.session.set_view_session_id(self.session.chat_view, session_id)

        elif message.type == "models_update":
            if hasattr(message, "content") and isinstance(message.content, dict):
                models = message.content.get("models", [])
                if models:
                    self.session.available_models = models

        elif message.type == "result":
            # Flush markdown formatter buffer
            self.append_content("", flush=True)
            # Stop loading on turn completion (heuristic)
            self.session.stop_loading()
            self.append_content("\n")

        elif message.type == "plan_delta":
            # Codex plan mode: accumulate <proposed_plan> content
            content = message.content if isinstance(message.content, str) else ""
            if content:
                self._plan_text += content

        elif message.type == "turn_started":
            # Extract turnId if available
            self._active_turn_id = message.content.get("turnId")
            self.session.start_loading()

        elif message.type in ("thinking", "text"):
            self.session.start_loading()

        elif message.type == "stop":
            # Codex agent sends "stop" when its process completes
            self.append_content("", flush=True)
            self.session.stop_loading()
            self.append_content("\n")
            # If Codex plan mode produced a plan, open it in a new view
            if self._plan_text:
                plan_text = self._plan_text
                self._plan_text = ""

                self.append_content("\n")
                self.append_content(plan_text)
                self.append_content("\n")

                # Add Implement button if in plan mode
                if self.session.agent_thread and self.session.agent_thread.anthropic_config.get("plan_mode"):
                    sublime.set_timeout(lambda pt=plan_text: self.session.show_implement_plan_button(pt), 0)

    def _format_tool_block(self, block):
        name = block.get("name")

        if name == "command_execution":
            command = block.get("command", "")
            if command:
                lines = command.rstrip().splitlines()
                if len(lines) > 1:
                    first_line = lines[0]
                    indented_rest = "\n".join("    " + line for line in lines[1:])
                    return f"⏺ command ({first_line})\n\n{indented_rest}\n"
                elif len(lines) == 1:
                    return f"⏺ command ({lines[0]})"
            return "⏺ command"
        elif name == "fileChange":
            filenames = block.get("filenames", [])
            header = f"⏺ fileChange ({', '.join(filenames)})" if filenames else "⏺ fileChange"

            changes = block.get("changes", [])
            diffs = []
            for change in changes:
                diff_text = change.get("diff") or change.get("patch") or change.get("unified_diff")
                if diff_text:
                    diff_blocks = self._render_diff_block(diff_text)
                    if diff_blocks:
                        diffs.append(diff_blocks)

            if diffs:
                return header + "\n" + "\n\n".join(diffs)

            return header

        return f"⏺ {name}" if name else ""
