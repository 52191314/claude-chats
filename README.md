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

It solves context loss and session fragmentation across multiple directories/workspaces by making local Claude agent trajectories instantly searchable, auditable, and resumable from the command line — it hands you a ready-to-paste `cd` + `claude --resume` command for the session's workspace.

---

## ⚡ Features

- ⚡ **Interactive Fuzzy Search**: Search across session prompts, IDs, models, and workspaces powered by `fzf` with live previews, running in the top ~75% of your terminal.
- ⌨️ **Shortcut Bar + Resume Commands Area**: A colored `[key] Action` shortcut bar and a **Resume commands** list live in the bottom ~25% of the screen. Single-letter shortcuts (`t` transcript, `y` copy id, `a` all/SDK, `u` unhide, `1`/`2` preview toggles); `/` opens the search box.
- 🖥️ **Rich Interactive Menu Fallback**: Zero-dependency fallback to built-in Rich terminal menu if `fzf` is not installed.
- 🔄 **Resume Command, Browser Stays Open**: Press `Enter` on a session and `claude-chats` copies the exact command that resumes it in its original workspace — `cd '<workspace>'; claude --resume <id> --setting-sources project` — to your clipboard. It does **not** launch Claude and does **not** exit, so you can keep browsing and grab more commands. Pass `--launch` for the old behavior (exit, then your PowerShell profile function `cd`s and starts Claude).
- 🎛️ **Toggleable Turn-by-Turn Preview**: The preview pane walks through the whole conversation. Your prompts are always shown; switch **Tools** (every tool call, one line each, e.g. `Bash(git status)`, `Read(tui.py)`) and assistant **Replies** on/off. Tool calls and replies are interleaved in the order they happened, and your choice is remembered between launches (`~/.claude_chats_preview_view`).
- 📖 **Full Transcript Pager**: Read turn-by-turn prompts, assistant reasoning/thinking blocks, and tool executions (`Bash`, `FileEdit`, `Glob`, `Grep`) with syntax highlights in your default pager.
- 📋 **Clipboard Integration**: Instantly copy conversation UUIDs or resume commands to clipboard (`pyperclip` / Windows clipboard / `clip.exe`).
- 🤖 **Subagent & SDK Filter**: Toggle between interactive CLI chats and background SDK sessions (`-a` / `a`).
- 🔓 **SDK Session Unhider (`-u`)**: Patch hidden `sdk-cli` sessions to `cli` so Claude Code's native `/resume` command recognizes and displays them natively!
- 📊 **Tabular CLI Mode**: Quick non-interactive list overview via `claude-chats -l` (with turns count, cost in USD, model, and status).
- ⏩ **Quick Resume Command**: Get the resume command for your latest session with `claude-chats -c`.
- ⚡ **High-Performance SQLite Cache**: Uses an indexed SQLite cache (`~/.claude/claude_chats_cache.db`) with mtime checking for instant (<0.05s) startup across hundreds of session files.

---

## 📦 Installation

### Option 1: PowerShell Installer (Windows)
From PowerShell:
```powershell
.\install.ps1
```
This installs `claude-chats.bat`, `claude_chats.py` and its helper module `fzf_ui.py` into `~/.local/bin`, and adds the PowerShell functions `claude-chats`, `claude-history`, and `csess` to your `$PROFILE`. If your profile already has the `claude-chats` function, only that function block is replaced.

> **Updating an existing install:** the `claude-chats` command runs the copy in `~/.local/bin`, not this repo. After pulling changes, re-run `.\install.ps1`, or just copy `claude_chats.py` **and** `fzf_ui.py` there.

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

- **Get the resume command for the most recent conversation** (one-shot: prints + copies it and exits; add `--launch` to start Claude directly):
  ```bash
  claude-chats -c             # or csess -c
  ```

- **View full transcript of a specific conversation in your pager:**
  ```bash
  claude-chats transcript <session-id>
  ```

- **Preview details of a conversation:**
  ```bash
  claude-chats preview <session-id>                     # uses your saved Tools/Replies switches
  claude-chats preview <session-id> --view=             # prompts only
  claude-chats preview <session-id> --view tools,replies
  ```

- **Unhide all SDK sessions in Claude's native `/resume` menu:**
  ```bash
  claude-chats -u             # or csess -u
  ```

---

## ⌨️ Controls & Keybindings

### In `fzf` Mode:
The browser is **modal**: single letters are shortcuts. Press `/` to open the search box (letters then type into it); `Enter` or `Esc` returns to shortcuts, keeping the filter. The bar at the bottom shows `[LIST]` or `[SEARCH]`.

| Key | Action |
| :--- | :--- |
| `↑` / `↓` / `j` / `k` | Move through sessions |
| `J` / `K`, `Ctrl-D` / `Ctrl-U`, `Mouse Wheel` | Scroll the preview pane |
| `Enter` / `r` | Copy the resume command (`cd` + `claude --resume`) and list it in the bottom **Resume commands** area — fzf stays open |
| `1` / `2` (or `Ctrl-O` / `Ctrl-R`) | Toggle **Tools** / assistant **Replies** in the preview (prompts are always shown) |
| `t` | Open full turn-by-turn transcript in your pager |
| `y` | Copy the session ID (confirmation shows in the bottom area) |
| `a` | Toggle all sessions (including SDK sessions) |
| `u` | Unhide all SDK sessions (rewrites their `entrypoint`, then waits for Enter) |
| `g` | **Group by working folder** on/off: each folder gets a `▼ name (N chats)` header row above its sessions; the preview on a header shows a folder overview |
| `Enter` / `Space` on a folder row | Fold / unfold that folder (the cursor stays on it) |
| `e` / `c` | Expand all / collapse all folders (grouped mode) |
| `#` | Limit how many sessions are listed (newest N; `0` = all) — prompts for a number. Defaults to 25, like `-n` |
| `/` | Search (`Enter` keeps the filter, `Esc` stops searching) |
| `Esc` | Clear the filter if there is one, otherwise quit |
| `q` / `Ctrl-C` | Quit |

Start grouped with `claude-chats -g`, or with a different limit via `-n 50` / `-n all`. Actions that restart the list (`a`, `g`, `#`, fold/unfold) put the cursor back on the row you were on. On quit, every resume command you collected is reprinted. Set `CHATS_UI_LOG=<file>` to log each key-binding callback if a shortcut misbehaves.

### In Rich Interactive Menu Mode:
| Key / Command | Action |
| :--- | :--- |
| `1`-`N` | Enter session number to copy its resume command (menu stays open; last command is re-shown under the actions) |
| `p <#>` | Preview details of session |
| `v` / `v tools` / `v replies` | Show / toggle what `p <#>` includes (prompts are always shown) |
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
3. Upon selecting a session, builds `cd '<workspace>'; claude --resume <session-id> --setting-sources project`, copies it to the clipboard, and keeps the browser open. With `--launch` it instead queues the command for the PowerShell profile function (`~/.claude_resume_cmd.ps1`) to run after the browser exits.

---

## 🗂️ Project Structure

```
claude-chats/
├── claude_chats.py    # CLI entrypoint, session scanner/cache, Rich menu (--menu)
├── fzf_ui.py          # fzf shell (default UI) - identical copy lives in agy-chats
├── claude-chats(.bat) # Launchers (bash / Windows)
├── install.ps1        # Windows installer (writes .bat launchers in ~/.local/bin pointing at this repo)
├── install.sh         # Linux/macOS/WSL installer (symlinks into ~/.local/bin)
├── pyproject.toml     # Package metadata; py-modules + console scripts
└── LICENSE
```

> `claude_chats.py` imports `fzf_ui.py`, so both files must stay side by side.

---

## 📄 License
MIT © [Vaibhav Singh Shekhawat](https://github.com/RAZOR-NINJAS)
