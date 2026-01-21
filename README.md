# Agent ChatView for Sublime Text

This package provides an interface to agentic coding directly within Sublime Text.

## Installation

### 1. Prerequisite: Claude Code CLI
ChatView requires the Claude Code CLI to be installed on your system.
```bash
curl -fsSL https://claude.ai/install.sh | bash
```

### 2. Auth in Claude Code
Run `claude` command to authenticate the CLI:
```bash
/login
```

### 3. Plugin Setup
Clone or download this repository into your Sublime Text `Packages` directory as `ChatView`:

### 4. Configuration
Open `Preferences -> Package Settings -> ChatView -> Settings` and configure user settings

## Usage

### 🚀 Starting a Chat
- **Keybinding**: Press `Ctrl+Alt+G` (Windows/Linux) or `Cmd+Alt+G` (macOS).
- **Command Palette**: Search for `ChatView: Start Chat`.
- **Text Selection**: Right-click and select `Chat with ChatView` to start a session with the selected code as context.

### 💬 Chat
- **Send Message**: Press `Ctrl+Enter` or `Cmd+Enter` to send your prompt.
- **Auto-completion**: Type `@` in the prompt area to search for files and add them to the context.

### 📂 Workspace & Context
- **Set Workspace**: Right-click a folder in the **Sidebar** and select `Set ChatView WorkSpace`, or use the `ChatView: Set Working Directory` command from the palette.
- **Current File Context**: The plugin uses the currently open file or selection to provide context to the agent when mentioned via `@filename`.

