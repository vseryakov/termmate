# TermMate

**Agentic Coding Mate from Design to meet Code**

![TermMate Screenshot](screenshot.jpg)

TermMate is a powerful agentic coding design tool for Sublime Text, bringing the full capabilities of AI-driven development directly into your editor. Built to work seamlessly with **ClaudeCode** and **Codex**, TermMate allows you to orchestrate complex coding tasks, refactor entire modules, and explore your codebase with a conversation-driven workflow that understands your project context.

## Getting Started

### 1. Prerequisites

TermMate relies on external agent cli. Install the required CLI tools:

**Claude Code:**
```bash
curl -fsSL https://claude.ai/install.sh | bash
```

**Codex:**
```bash
npm install -g @openai/codex
```

> **Note:** TermMate automatically detects CLI installation paths across multiple environments, including **Homebrew**, **npm-global**, **Yarn**, and common local binary directories. You typically don't need to manually configure environment variables or search paths.


### 2. Authentication

Authenticate the agents via your terminal:

**Claude Code:**
```bash
claude /login
```

**Codex:**
```bash
codex login
```

### 3. TermMate Installation

Clone this repository into your Sublime Text `Packages` directory as `TermMate`.

### 4. Start Chat

Open the Command Palette and search for `TermMate: Start Chat`. Alternatively, use the shortcut `Cmd+Alt+G` (macOS) or `Ctrl+Alt+G` (Windows/Linux) to start your first agentic coding session instantly.

## Usage & Key Features

**Quick Prompt Without Chat View**
Use the command palette (`TermMate: Prompt`) to send a quick instruction to the agent without opening the chat view manually.

**Clear Session**
To reset the current conversation history and start a completely fresh context, open the command palette and run **`TermMate: Clear Session`**. This will reload the agent and clear its memory for the current workspace.

**Set Working Space**
Right-click on any folder in the sidebar and select **Set TermMate Working Space** to set the working directory for the agent. This affects the current working directory when agents execute commands or access files. You can also use the command palette.

**Chat with Current File or Selection**
You can right-click in any file, tab, and select **Chat with TermMate agent**. This will:
- Open the TermMate chat view (if not already open).
- Insert a reference to the file (`@filename`) or selected line range (`@filename#L1-10`) into the message prompt.
- Tagged files will be automatically sent as context to the active agent.

**Smart Completion**
Type `@` in the chat view for real-time suggestions of files and workspace symbols.

### Advanced Control (Pro Features)
TermMate provides deep integration with agentic workflows via the Command Palette:

**Plan Mode**
Toggle between **Fast** (direct execution) and **Planning** (deliberative reasoning) via `TermMate: Plan Mode`.

**Approve Mode**
Agents perform various actions (tools) like reading files, searching the web, or executing commands. You can control how much manual approval is required via the command palette: `TermMate: Approve Mode`
- **Default**: Prompts for your approval by default.
- **Allow Edit**: Automatically approves "safe" read/edit operations; still prompts for "risky" commands.
- **Accept All**: Automatically approves all tool calls, including shell command execution for maximum autonomy.

**Switch Agents & Model Selection**
Effortlessly swap between Claude, Codex, or custom agent providers. Fine-tune performance by selecting specific LLM models for different tasks.

## Shortcuts & Commands

| Action | macOS | Windows/Linux | Command Palette |
| :--- | :--- | :--- | :--- |
| **Start New Chat** | - | - | `TermMate: Start Chat` |
| **Send Message** | `Cmd+Enter` | `Ctrl+Enter` | - |
| **Navigate Input History** | `Up` / `Down` | `Up` / `Down` | - |
| **Mention File** | `@` | `@` | - |
| **Set Workspace** | - | - | `TermMate: Set Working Directory` |
| **Switch Mode** | - | - | `TermMate: Plan Mode` |
| **Approve Mode** | - | - | `TermMate: Approve Mode` |

## Configuration

Customize TermMate by editing your settings:
`Preferences -> Package Settings -> TermMate -> Settings`

While TermMate automatically detects most installation paths, you may need to configure them manually if:
- You use a custom installation location not listed in the default paths.
- You have multiple versions installed and want to pin a specific binary.
- Automatic detection fails on your specific OS configuration.

```json
{
    "claude_command": "/path/to/your/custom/claude",
    "codex_command": "/path/to/your/custom/codex"
}
```

**Custom Keybindings**

By default, TermMate does not register a shortcut for `TermMate: Start Chat` to avoid conflicts. You can manually add a shortcut (like `Cmd+Alt+G` or `Ctrl+Alt+G`) by navigating to `Preferences -> Key Bindings` and adding the following configuration:

```json
[
    {
        "keys": ["primary+alt+g"],
        "command": "chat_view_cli",
        "args": {},
        "context":
        [
            { "key": "setting.is_widget", "operand": false }
        ]
    }
]
```

## 💡 Tips

- **Selection as Context**: Select code before starting a chat to focus the agent's attention on specific logic.
- **Iterative Refinement**: Use **Planning Mode** for large architectural changes to see the agent's proposed steps before they are applied.

## Context Interaction and Data Privacy

By default, TermMate **does not** send your entire workspace or file contents to external agents. Data is only sent to Claude Code or Codex in the following scenarios:

*   **Chat Messages**: Any text you type directly into the TermMate chat view.
*   **Explicit Context (@-mentions)**: When you use the `@filename` syntax to provide specific context.
*   **Tool-driven Context**: If the coding agent requests to read a file, list a directory, or execute a command (and you have granted permission via Approve Mode), that information is sent back to the model as part of the interaction.

All communication happens via the respective CLI tools (`claude` or `codex`) installed on your system, which connect directly to their respective servers using your configured authentication credentials.

## License

This project is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

It is free for personal, research, and non-commercial organizations. **Commercial use is strictly prohibited.**

