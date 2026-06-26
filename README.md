# TermMate

**Agentic Coding Mate from Mind to Code**

![TermMate Screenshot](screenshot.jpg)

TermMate is a professional AI coding agent for Sublime Text that supports multi-agent providers, including **Claude Code**, **Codex**, and **[Pi Agent](https://pi.dev)**. It builds a seamless native agentic interface directly within your editor for autonomous task execution, codebase exploration, and smart refactoring. **TermMate Agent, native to your editor.**

For detailed usage, please refer to the [TermMate Documentation](https://termmate.app/docs/setup).

## Getting Started

### 1. Prerequisites

TermMate relies on external agent CLIs. Install at least one of the following CLI tools based on your preference:

**Claude Code:**
```bash
curl -fsSL https://claude.ai/install.sh | bash
```

**Codex:**
```bash
npm install -g @openai/codex
```

**Pi Agent:**
```bash
curl -fsSL https://pi.dev/install.sh | sh
```

> **Note:** TermMate automatically detects CLI installation paths across multiple environments, including **Homebrew**, **npm-global**, **Yarn**, and common local binary directories. You typically don't need to manually configure environment variables or search paths.


### 2. Authentication

Authenticate the agents via your terminal:

**Claude Code:**
```bash
/login
```

**Codex:**
```bash
codex login
```

**Pi Agent:**
```bash
/login
```

Alternatively, you can authenticate by setting API keys via environment variables in TermMate's settings. See [Custom Environment Variables](#custom-environment-variables) for details.

### 3. TermMate Installation

Install TermMate via [Package Control](https://packagecontrol.io/packages/TermMate):

1. Open the Command Palette (`Cmd+Shift+P` on macOS, `Ctrl+Shift+P` on Windows/Linux).
2. Type `Package Control: Install Package` and press `Enter`.
3. Search for `TermMate` and press `Enter`.

**Or install manually from [Releases](https://github.com/flashmodel/termmate/releases):**

1. Download `TermMate.sublime-package` from the [latest release](https://github.com/flashmodel/termmate/releases).
2. Copy `TermMate.sublime-package` into your Sublime Text **Installed Packages** directory:
   - **macOS**: `~/Library/Application Support/Sublime Text/Installed Packages/`
   - **Windows**: `%APPDATA%\Sublime Text\Installed Packages\`
   - **Linux**: `~/.config/sublime-text/Installed Packages/`
3. Restart Sublime Text.

### 4. Start Chat

- Open the command palette (`Cmd+Shift+P` on macOS, `Ctrl+Shift+P` on Windows/Linux).
- Type `TermMate: Start Chat` and press `Enter`.
- A new view will open for the TermMate chat.
- Type your message and press `Cmd+Enter` (macOS) or `Ctrl+Enter` (Windows/Linux) to send.
- You can stop a running conversation at any time. Use the shortcut `Cmd+Escape` (Mac) / `Shift+Escape` (Windows/Linux) in the chat window, or run `TermMate: Stop Conversation` from the command palette.

## Usage & Key Features

**1. Quick Prompt Without Chat View**

Use the command palette (`TermMate: Prompt`) to send a quick instruction to the agent without opening the chat view manually.

**2. Clear Session**

To reset the current conversation history and start a completely fresh context, open the command palette and run **`TermMate: Clear Session`**. This will reload the agent and clear its memory for the current workspace.

**3. Resume Previous Conversation**

Run **`TermMate: Resume Session`** from the command palette to continue a past conversation. A quick panel lists previous sessions for the current workspace, each showing a short summary and timestamp. Select one and the agent picks up exactly where it left off.

**4. Set Working Directory**

Right-click on any folder in the sidebar and select **Set Working Directory** to set the working directory for the agent. This affects the current working directory when agents execute commands or access files. You can also use the command palette.

**5. Chat with Current File or Selection**

You can right-click in any file, tab, and select **Chat with Agent**. This will:

- Open the TermMate chat view (if not already open).
- Insert a reference to the file (`@filename`) or selected line range (`@filename#L1-10`) into the message prompt.
- Tagged files will be automatically sent as context to the active agent.

**6. Smart Completion**

Type `@` in the chat view for real-time suggestions of files and workspace symbols.

**7. Split Chat Window**

You can use the command palette (`TermMate: Split Chat Window`) or right-click the chat view tab and select **TermMate: Split Chat Window** to split the editor layout and place the chat view into its own dedicated pane. By default, this pane is isolated so opening other files will not overwrite the chat view. This isolation behavior can be configured via the `dedicated_chat_pane` setting.

**8. Rewind Conversation**

Hover over any gutter dot or click the `↩` button that appears at the prompt line to rewind the conversation to that point. A confirmation panel lets you confirm or cancel before the rewind takes effect.

When confirmed, TermMate forks the session at the selected prompt, removes all subsequent messages from the chat view - letting you explore a different direction without losing the original context.

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

Effortlessly swap between Claude, Codex, Pi Agent, or custom agent providers. Fine-tune performance by selecting specific LLM models for different tasks.

## Shortcuts & Commands

| Action | macOS | Windows/Linux | Command Palette |
| :--- | :--- | :--- | :--- |
| **Start New Chat** | - | - | `TermMate: Start Chat` |
| **Split Chat Window** | - | - | `TermMate: Split Chat Window` |
| **Send Message** | `Cmd+Enter` | `Ctrl+Enter` | - |
| **Stop Conversation** | `Cmd+Escape` | `Shift+Escape` | `TermMate: Stop Conversation` |
| **Navigate Input History** | `Up` / `Down` | `Up` / `Down` | - |
| **Mention File** | `@` | `@` | - |
| **Set Workspace** | - | - | `TermMate: Set Working Directory` |
| **Switch Mode** | - | - | `TermMate: Plan Mode` |
| **Approve Mode** | - | - | `TermMate: Approve Mode` |

## Configuration

Customize TermMate by editing your settings: `Preferences -> Package Settings -> TermMate -> Settings`

### Agent CLI Paths

While TermMate automatically detects most agent CLI installation paths, you may need to configure them manually if:

- You use a custom installation location not listed in the default paths.
- You have multiple versions installed and want to pin a specific binary.
- Automatic detection fails on your specific OS configuration.

```json
{
    "claude_command": "/path/to/your/custom/claude",
    "codex_command": "/path/to/your/custom/codex",
    "pi_command": "/path/to/your/custom/pi"
}
```

### Custom Environment Variables

The `env` configuration allows you to inject custom environment variables directly into the process when starting an agent CLI command. This is useful for providing API keys, custom base URLs, or passing specific environment values without altering your global system configuration.

For example, to configure **OpenRouter** for Claude, you can provide your OpenRouter API key and base URL in the `env` section of your settings(ANTHROPIC_API_KEY should be empty in the case):

```json
{
    "env": {
        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api/v1",
        "ANTHROPIC_AUTH_TOKEN": "sk-openrouter-token",
        "ANTHROPIC_API_KEY": ""
    }
}
```

### Custom Keybindings

By default, TermMate does not register a shortcut for `TermMate: Start Chat` to avoid conflicts. You can manually add a shortcut (like `Cmd+Alt+G` or `Ctrl+Alt+G`) by navigating to `Preferences -> Key Bindings` and adding the following configuration:

```json
[
    {
        "keys": ["primary+alt+g"],
        "command": "term_chat_cli",
        "args": {},
        "context":
        [
            { "key": "setting.is_widget", "operand": false }
        ]
    }
]
```

If you prefer using just the `Escape` key to interrupt the conversation when the chat view is focused, you can add this:

```json
[
    {
        "keys": ["escape"],
        "command": "term_chat_interrupt",
        "context":
        [
            { "key": "setting.chatview_chat", "operator": "equal", "operand": true }
        ]
    }
]
```

## 💡 TermMate Agent Tips

- **Selection as Context**: Select code before starting a chat to focus the agent's attention on specific logic.
- **Iterative Refinement**: Use **Planning Mode** for large architectural changes to see the agent's proposed steps before they are applied.

## Privacy & Data Handling

**TermMate does not send your entire workspace or file contents to any external servers.**

Your data will only be sent to the respective LLM services (Claude Code, Codex, or Pi) under the following specific conditions:

**What data is sent:**

- Any text you manually type into the TermMate ChatView.
- The contents of specific files you explicitly tag using the `@filename` syntax.
- The outputs of shell commands, directory listings, or file contents that the agent explicitly requests to read.

**How TermMate interacts with Agents:**

- **Local Execution**: The core plugin logic runs entirely on your local machine. All communication happens locally via the official CLI tools (`claude`, `codex`, or `pi`) installed on your system.
- **No Data Collection**: TermMate does not collect, store, or transmit any of your source code or usage telemetry to our servers. TermMate does not send data to any third-party middleman servers; data goes directly to Anthropic or OpenAI using your own configured authentication credentials.
- Data is only sent when you actively hit command+enter(or ctrl+enter) in the ChatView, or when the agent executes a tool (if you have granted permission via your `Approve Mode` settings).


## License

TermMate is provided under the **Apache License, Version 2.0** with the **Commons Clause** condition.

This means it's free to use, modify, and redistribute the code for personal or internal use. However, **commercial resale or providing a paid service is strictly prohibited**.

For complete details, please see the [LICENSE](LICENSE) file.

