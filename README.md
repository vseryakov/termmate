# TermMate

**Agentic Coding Mate from Design to meet Code**

![TermMate Screenshot](screenshot.jpg)

TermMate is a powerful agentic coding design tool for Sublime Text, bringing the full capabilities of AI-driven development directly into your editor. Built to work seamlessly with **ClaudeCode** and **Codex**, TermMate allows you to orchestrate complex coding tasks, refactor entire modules, and explore your codebase with a conversation-driven workflow that understands your project context.

---

## 🚀 Getting Started

### 1. Prerequisites
TermMate relies on external coding agents. Install the required CLI tools:

**Claude Code:**
```bash
curl -fsSL https://claude.ai/install.sh | bash
```

**Codex:**
```bash
npm install -g @openai/codex
```

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

### 3. Installation
Clone this repository into your Sublime Text `Packages` directory as `TermMate`.

### 4. Start Chat
Open the Command Palette and search for `TermMate: Start Chat`. Alternatively, use the shortcut `Cmd+Alt+G` (macOS) or `Ctrl+Alt+G` (Windows/Linux) to start your first agentic coding session instantly.

---

## 🛠️ Key Features

### Conversational Workflow
- **Start New Chat**: Launch an agentic session instantly.
- **Contextual Awareness**: Mention files with `@filename` to provide specific context.
- **Smart Completion**: Real-time suggestions for files and workspace symbols.

### Advanced Control (Pro Features)
TermMate provides deep integration with agentic workflows via the Command Palette:

- **Plan Mode**: Toggle between **Fast** (direct execution) and **Planning** (deliberative reasoning).
- **Approve Mode**: Configure tool permissions. Choose from **Default** (ask every time), **Allow Edit**, or **Accept All** for maximum autonomy.
- **Switch Agents**: Effortlessly swap between Claude, Codex, or custom agent providers.
- **Model Selection**: Fine-tune performance by selecting specific LLM models for different tasks.

### Workspace Management
- **Set Workspace**: Define the agent's working directory from the sidebar or palette.
- **Clear Session**: Reset the agent's memory and state for a fresh start.

---

## Shortcuts & Commands

| Action | macOS | Windows/Linux | Command Palette |
| :--- | :--- | :--- | :--- |
| **Start New Chat** | `Cmd+Alt+G` | `Ctrl+Alt+G` | `TermMate: Start Chat` |
| **Send Message** | `Cmd+Enter` | `Ctrl+Enter` | - |
| **Mention File** | `@` | `@` | - |
| **Set Workspace** | - | - | `TermMate: Set Working Directory` |
| **Switch Mode** | - | - | `TermMate: Plan Mode` |
| **Approve Mode** | - | - | `TermMate: Approve Mode` |

---

## Configuration
Customize TermMate by editing your settings:
`Preferences -> Package Settings -> TermMate -> Settings`

```json
{
    "claude_command": "your-claude-path",
    "codex_command": "your-codex-path"
}
```

---

## 💡 Pro Tips
- **Selection as Context**: Select code before starting a chat to focus the agent's attention on specific logic.
- **Iterative Refinement**: Use **Planning Mode** for large architectural changes to see the agent's proposed steps before they are applied.
- **Tool Autonomy**: Use **Accept All** in Approve Mode when performing repetitive tasks across many files.

