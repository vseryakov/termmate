import os
import shutil
import subprocess
import sys
import threading

import sublime
import sublime_plugin

from ..genfoundry.claude_agent import find_claude_cli
from ..genfoundry.codex_agent import find_codex_cli
from ..genfoundry.pi_agent import find_pi_cli

AGENT_CLI_NAME = {"claude": "claude", "codex": "codex", "pi": "pi"}
AGENT_FIND_FN  = {"claude": find_claude_cli, "codex": find_codex_cli, "pi": find_pi_cli}
AGENT_LABEL    = {"claude": "Claude Code",   "codex": "Codex",        "pi": "Pi Agent"}


def find_existing_cli(agent, settings=None):
    """
    Return the path to an installed agent CLI, or None.

    Resolution order:
      1. Custom path from settings (e.g. 'claude_command') if provided and executable
      2. shutil.which — anything on $PATH
      3. find_*_cli() — common off-PATH install locations
    """
    if settings is not None:
        custom = settings.get(f"{agent}_command")
        if custom and shutil.which(custom):
            return shutil.which(custom)
    return shutil.which(AGENT_CLI_NAME[agent]) or AGENT_FIND_FN[agent]()


def get_agent_install_info(agent):
    """
    Return (display_name, cmd, supported, extra_env).

    Installation targets (no root/sudo required):
      macOS/Linux — npm packages go to ~/.local/bin via --prefix ~/.local
                    Pi uses its own curl installer which targets ~/.local/bin
      Windows     — npm packages go to %APPDATA%\\npm (npm's default user-level
                    prefix, no elevation needed); Pi is not supported on Windows.
    """
    home   = os.path.expanduser("~")
    is_win = sys.platform == "win32"
    display = AGENT_LABEL.get(agent, agent)

    if agent == "claude":
        if is_win:
            return display, "npm install -g @anthropic-ai/claude-code", True, {}
        local_bin = os.path.join(home, ".local", "bin")
        return (
            display,
            "curl -fsSL https://claude.ai/install.sh | bash",
            True,
            {"PATH": local_bin + os.pathsep + os.environ.get("PATH", "")},
        )

    if agent == "codex":
        local_bin = os.path.join(home, ".local", "bin")
        if is_win:
            return (
                display,
                'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"',
                True,
                {},
            )
        return (
            display,
            "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
            True,
            {
                "PATH": local_bin + os.pathsep + os.environ.get("PATH", ""),
                "CODEX_NON_INTERACTIVE": "1",
            },
        )

    if agent == "pi":
        if is_win:
            return display, None, False, {}
        local_bin = os.path.join(home, ".local", "bin")
        return (
            display,
            "curl -fsSL https://pi.dev/install.sh | sh",
            True,
            {
                "PATH": local_bin + os.pathsep + os.environ.get("PATH", ""),
                "PI_NON_INTERACTIVE": "1",
                "CI": "1",
            },
        )

    return display, None, False, {}


def write_to_panel(panel, text):
    panel.run_command("append", {"characters": text, "force": True, "scroll_to_end": True})


def run_install(window, agent, on_success):
    """
    Open an output panel, optionally prompt the user if CLI already exists,
    then run the install command in a background thread.

    on_success(agent, display_name, write_fn) is called on the main thread
    after a successful install.
    """
    display_name, cmd, supported, extra_env = get_agent_install_info(agent)

    panel = window.create_output_panel("termmate_install")
    panel.settings().set("word_wrap", True)
    window.run_command("show_panel", {"panel": "output.termmate_install"})

    if not supported:
        write_to_panel(panel,
            f"{display_name} does not have an installer for Windows yet.\n"
            "Please check the project's documentation for manual setup instructions.\n"
        )
        return

    existing = find_existing_cli(agent)
    if existing:
        if not os.access(existing, os.W_OK):
            if sys.platform == "win32":
                prompt_note = "You may be asked to allow an administrator prompt."
                cmd_prefix = ""
            else:
                prompt_note = "You may be asked for your password."
                cmd_prefix = "$ "
            write_to_panel(panel,
                f"{display_name} is already installed at:\n  {existing}\n\n"
                f"It was installed by an administrator and cannot be updated automatically. To update, open a terminal and run:\n\n"
                f"  {cmd_prefix}{cmd}\n\n"
                f"{prompt_note}\n"
            )
            return
        write_to_panel(panel, f"{AGENT_LABEL[agent]} is already installed at:\n  {existing}\n\nUpdating...\n$ {cmd}\n\n")
    else:
        write_to_panel(panel, f"Installing {display_name}...\n$ {cmd}\n\n")

    def _worker():
        env = os.environ.copy()
        env.update(extra_env)
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                **kwargs,
            )
            for line in proc.stdout:
                sublime.set_timeout(lambda l=line: write_to_panel(panel, l), 0)
            proc.wait()
            if proc.returncode == 0:
                sublime.set_timeout(lambda: on_success(agent, display_name, lambda t: write_to_panel(panel, t)), 0)
            else:
                sublime.set_timeout(
                    lambda: write_to_panel(panel, f"\nInstall failed (exit code {proc.returncode}).\n"), 0
                )
        except Exception as exc:
            sublime.set_timeout(lambda: write_to_panel(panel, f"\nError: {exc}\n"), 0)

    threading.Thread(target=_worker, daemon=True).start()


def get_agent_list_items(settings):
    """Return ListInputItems for all agents, annotated with install status."""
    is_win = sys.platform == "win32"
    items = []
    for agent in AGENT_FIND_FN:
        existing = find_existing_cli(agent, settings)
        if is_win:
            location = "not supported on Windows" if agent == "pi" else "%APPDATA%\\npm"
        else:
            location = "~/.local/bin"
        status = f"installed: {existing}" if existing else location
        items.append(sublime.ListInputItem(AGENT_LABEL[agent], agent, annotation=status))
    return items
