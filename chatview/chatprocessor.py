import difflib
import logging
import os
import xml.etree.ElementTree

import sublime

from . import utils

LOG = logging.getLogger("TermMate")

class BaseChatMessageProcessor:
    """
    Handles buffering, formatting, and displaying messages from the agent.
    """
    def __init__(self, session):
        self.session = session
        self.markdown_formatter = utils.MarkdownFormatter()
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
                {"text": f"\n\nError: {error_msg}\n"}),
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

class PiMessageProcessor(BaseChatMessageProcessor):
    def _handle_typed_message(self, message):
        if message.type == "text_delta":
            self.session.start_loading()
            if self.last_is_tool_call:
                self.append_content("\n")
                self.last_is_tool_call = False
            self.append_content(message.content)
            return
            
        if message.type == "text_end":
            # The text was already appended via text_delta, so we just ignore this message
            # to prevent duplicating the entire chunk.
            return
            
        if message.type in ("thinking_start", "thinking_delta"):
            self.session.start_loading()
            return

        if message.type == "assistant":
            self.session.start_loading()

            # Extract tool blocks
            if hasattr(message, "content"):
                for block in message.content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if not self.last_is_tool_call:
                            self.append_content("\n")
                        self.last_is_tool_call = True
                        content = self._format_tool_block(block)
                        if content:
                            self.append_content(content + "\n")

        elif message.type == "system":
            if hasattr(message, "content") and isinstance(message.content, dict):
                session_id = message.content.get("session_id")
                if session_id and message.content.get("subtype") == "init":
                    LOG.info(f"system session_id: {session_id}")
                    # Store session_id in view settings for persistence across restarts
                    self.session.set_view_session_id(self.session.chat_view, session_id)



        elif message.type == "extension_ui_request":
            request = message.content
            method = request.get("method")
            request_id = request.get("id")
            title = request.get("title", method)
            
            tool_name = f"extension_ui_{method}"
            self.session.permission_requests[request_id] = (tool_name, request)
            
            # Show in permission phantom
            self.session.show_permission_phantom(request_id, f"Extension UI: {title}", request)

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
        input_data = block.get("arguments", {})

        if name == "read":
            file_path = input_data.get("file_path") or input_data.get("path") or ""
            if file_path:
                try:
                    if os.path.isabs(file_path):
                        rel_path = os.path.relpath(file_path, self.session.agent_thread.cwd)
                    else:
                        rel_path = file_path
                except Exception:
                    rel_path = file_path

                offset = input_data.get("offset")
                limit = input_data.get("limit")

                if offset is not None and limit is not None:
                    start_line = offset
                    end_line = offset + limit - 1
                    return f"⏺ read {rel_path}#L{start_line}-L{end_line}"
                elif offset is not None:
                    return f"⏺ read {rel_path}#L{offset}"
                else:
                    return f"⏺ read {rel_path}"

        elif name in ("agent", "task"):
            description = input_data.get("description", "")
            subagent_type = input_data.get("subagent_type", "")

            parts = [f"⏺ {name}"]
            if subagent_type:
                parts.append(subagent_type)
            if description:
                parts.append(description)

            return " ".join(parts)

        elif name == "bash":
            command = input_data.get("command", "")
            if command:
                return f"⏺ bash {command}"

        elif name in ("write", "edit"):
            file_path = input_data.get("file_path") or input_data.get("path") or ""
            if file_path:
                try:
                    if os.path.isabs(file_path):
                        rel_path = os.path.relpath(file_path, self.session.agent_thread.cwd)
                    else:
                        rel_path = file_path
                except Exception:
                    rel_path = file_path
                header = f"⏺ {name} {rel_path}"

                old_text = input_data.get("old_string") or input_data.get("oldText")
                new_text = input_data.get("new_string") or input_data.get("newText")
                if name == "edit" and old_text is not None and new_text is not None:
                    if old_text and not old_text.endswith("\n"): old_text += "\n"
                    if new_text and not new_text.endswith("\n"): new_text += "\n"
                    old_lines = old_text.splitlines(keepends=True)
                    new_lines = new_text.splitlines(keepends=True)

                    diff_lines = list(difflib.unified_diff(
                        old_lines, new_lines,
                        fromfile=f"a/{rel_path}",
                        tofile=f"b/{rel_path}"
                    ))

                    if len(diff_lines) > 2:
                        diff_text = "".join(diff_lines[2:])
                        return header + "\n" + self._render_diff_block(diff_text)
                        
                elif name == "edit" and "edits" in input_data:
                    # pi agent edits array
                    edits = input_data.get("edits", [])
                    if edits and isinstance(edits, list):
                        diff_outputs = []
                        for edit in edits:
                            old_t = edit.get("oldText")
                            new_t = edit.get("newText")
                            if old_t is not None and new_t is not None:
                                if old_t and not old_t.endswith("\n"): old_t += "\n"
                                if new_t and not new_t.endswith("\n"): new_t += "\n"
                                o_lines = old_t.splitlines(keepends=True)
                                n_lines = new_t.splitlines(keepends=True)
                                d_lines = list(difflib.unified_diff(o_lines, n_lines, fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
                                if len(d_lines) > 2:
                                    diff_outputs.append("".join(d_lines[2:]))
                        if diff_outputs:
                            return header + "\n" + self._render_diff_block("\n".join(diff_outputs))

                return header
        elif name in ("grep", "find", "ls"):
            pattern = input_data.get("pattern") or input_data.get("query") or input_data.get("path") or input_data.get("regex") or ""
            return f"⏺ {name} {pattern}".strip()

        return f"⏺ {name}" if name else ""
