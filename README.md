# claude-chats

Interactive Terminal User Interface (TUI) and fuzzy search browser to browse, inspect, and resume previous **Claude Code** (`claude`) conversations.

Ported and evolved from [**agy-chats**](https://github.com/RAZOR-NINJAS/agy-chats) for Anthropic's Claude Code agent CLI.

```
       _                 _             _           _       
  ___ | | __ _ _   _  __| | ___    ___| |__   __ _| |_ ___ 
 / __|| |/ _` | | | |/ _` |/ _ \  / __| '_ \ / _` | __/ __|
| (__ | | (_| | |_| | (_| |  __/ | (__| | | | (_| | |_\__ \
 \___||_|\__,_|\__,_|\__,_|\___|  \___|_| |_|\__,_|\__|___/
```

`claude-chats` (also aliased as `claude-history` and `csess`) connects directly to your local Claude Code state (`~/.claude/projects/`) to provide an instant, interactive conversation history browser.

It solves context loss and session fragmentation across multiple directories/workspaces by making local Claude agent trajectories instantly searchable, auditable, and resumable from the command line with automatic `cd` to the session's workspace.

---

## ⚡ Features

- ⚡ **Interactive Fuzzy Search**: Search across session prompts, IDs, models, and workspaces powered by `fzf` with live previews.
- 🖥️ **Rich Interactive Menu Fallback**: Zero-dependency fallback to built-in Rich terminal menu if `fzf` is not installed.
- 🔄 **Direct Resume & Directory Switch**: Automatically navigates (`cd`) to the original workspace and resumes Claude seamlessly (`claude --resume <id> --setting-sources project`).
- 📖 **Full Transcript Pager**: Read turn-by-turn prompts, assistant reasoning/thinking blocks, and tool executions (`Bash`, `FileEdit`, `Glob`, `Grep`) with syntax highlights in your default pager.
- 📋 **Clipboard Integration**: Instantly copy conversation UUIDs or resume commands to clipboard (`pyperclip` / Windows clipboard / `clip.exe`).
- 🤖 **Subagent & SDK Filter**: Toggle between interactive CLI chats and background SDK sessions (`-a` / `Ctrl-A`).
- 🔓 **SDK Session Unhider (`-u`)**: Patch hidden `sdk-cli` sessions to `cli` so Claude Code's native `/resume` command recognizes and displays them natively!
- 📊 **Tabular CLI Mode**: Quick non-interactive list overview via `claude-chats -l` (with turns count, cost in USD, model, and status).
- ⏩ **Quick Resume**: Jump straight back into your latest session with `claude-chats -c`.
- ⚡ **High-Performance SQLite Cache**: Uses an indexed SQLite cache (`~/.claude/claude_chats_cache.db`) with mtime checking for instant (<0.05s) startup across hundreds of session files.

---

## 📦 Installation

### Option 1: PowerShell Installer (Windows)
From PowerShell:
```powershell
.\install.ps1
```
This symlinks / installs `claude-chats.bat`, `claude_chats.py`, and adds the PowerShell functions `claude-chats`, `claude-history`, and `csess` to your `$PROFILE`.

### Option 2: Quick Install Script (Linux / macOS / WSL)
```bash
git clone https://github.com/52191314/claude-chats.git
cd claude-chats
./install.sh
```
This symlinks `claude-chats` into `~/.local/bin/`.

Ensure `~/.local/bin` is in your `PATH`:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Option 3: Python / Pip / Pipx
```bash
pip install .   # or pipx install .
```

---

## 🚀 Usage

### Interactive TUI Browser
Launch the interactive browser:
```bash
claude-chats    # or csess, claude-history
```

Filter by keyword immediately:
```bash
claude-chats "my-search-term"
```

Include SDK/background sessions:
```bash
claude-chats -a
```

### Non-Interactive & Quick Commands
- **List conversations in a table:**
  ```bash
  claude-chats --list         # or csess -l
  claude-chats -l -n 50       # show 50 sessions
  claude-chats -l -n all      # show all sessions
  claude-chats -l "ODM"       # filter by keyword
  ```

- **Launch browser with custom session limit:**
  ```bash
  csess -n 50                 # show 50 sessions in browser
  csess -n all                # show all sessions
  csess --menu                # force numbered Rich menu instead of fzf
  ```

- **Resume the most recent conversation immediately:**
  ```bash
  claude-chats -c             # or csess -c
  ```

- **View full transcript of a specific conversation in your pager:**
  ```bash
  claude-chats transcript <session-id>
  ```

- **Preview details of a conversation:**
  ```bash
  claude-chats preview <session-id>
  ```

- **Unhide all SDK sessions in Claude's native `/resume` menu:**
  ```bash
  claude-chats -u             # or csess -u
  ```

---

## ⌨️ Controls & Keybindings

### In `fzf` Mode:
| Key | Action |
| :--- | :--- |
| `Enter` | Jump to session directory (`cd`) and resume Claude Code |
| `Ctrl-T` | Open full turn-by-turn transcript in pager |
| `Ctrl-Y` | Copy session ID to system clipboard |
| `Ctrl-A` | Toggle all sessions (including SDK sessions) |
| `Ctrl-U` | Unhide all SDK sessions |
| `Shift-Up` / `Shift-Down` | Scroll preview pane line by line |
| `Ctrl-D` / `Ctrl-U` | Scroll preview pane half-page down / up |
| `Mouse Wheel` | Scroll preview pane with mouse |
| `Esc` / `Ctrl-C` | Exit cleanly without resuming |

### In Rich Interactive Menu Mode:
| Key / Command | Action |
| :--- | :--- |
| `1`-`N` | Enter session number to `cd` & resume |
| `p <#>` | Preview details of session |
| `t <#>` | View full turn-by-turn transcript |
| `y <#>` | Copy session ID to clipboard |
| `m` / `more` | Show more sessions (+25) |
| `m all` | Show all available sessions |
| `n <#>` | Set custom session display limit (e.g. `n 50`, `n all`) |
| `/<text>` | Filter sessions by keyword |
| `c` or `/` | Clear active search filter |
| `a` | Toggle SDK/hidden sessions |
| `u` | Unhide all SDK sessions in Claude |
| `Enter` | Exit |

---

## ⚙️ How It Works

Claude Code stores session metadata and turn trajectories locally in JSONL files partitioned by sanitized workspace paths:
- `~/.claude/projects/<project-dir>/<session-id>.jsonl`
- `~/.claude/history.jsonl`

`claude-chats`:
1. Scans `~/.claude/projects/` and caches parsed metadata in a local SQLite database (`~/.claude/claude_chats_cache.db`), refreshing only files whose `mtime` has changed.
2. Extracts user prompts (cleaning internal `<local-command-caveat>` and XML wrappers), assistant responses, thinking blocks, tool calls (`Bash`, `FileEdit`, etc.), and cost/token statistics.
3. Upon selecting a session to resume, sets the shell working directory to the session's workspace and launches `claude --resume <session-id> --setting-sources project` cleanly without double-execution.

---

## 📄 License
MIT © [Vaibhav Singh Shekhawat](https://github.com/RAZOR-NINJAS)
