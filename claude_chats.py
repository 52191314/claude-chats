#!/usr/bin/env python3
"""
claude-chats: Interactive Terminal User Interface (TUI) and fuzzy search browser
to inspect, explore, and seamlessly resume previous Claude Code (claude) conversations.

Ported from agy-chats (by RAZOR-NINJAS) for Claude Code.
"""

import os
import sys
import json
import sqlite3
import re
import shutil
import subprocess
import argparse
from datetime import datetime, timezone
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import fzf_ui  # fzf browser shell (shortcut bar + resume-commands area); same module as agy-chats

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECTS_DIR = os.path.expanduser(r"~/.claude/projects")
CACHE_DB_PATH = os.path.expanduser(r"~/.claude/claude_chats_cache.db")
RESUME_CMD_FILE = os.path.join(os.path.expanduser("~"), ".claude_resume_cmd.ps1")
STATE_FILE = os.path.join(os.path.expanduser("~"), ".claude_chats_show_all")
VIEW_STATE_FILE = os.path.join(os.path.expanduser("~"), ".claude_chats_preview_view")

console = Console()

# ANSI Color codes for preview and terminal outputs
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
GRAY = "\033[90m"
WHITE = "\033[97m"
RED = "\033[31m"


# ==============================================================================
# Preview view state: which extras the preview shows (prompts are always shown)
# ==============================================================================

VIEW_KEYS = ("tools", "replies")
_VIEW_DEFAULT = {"tools": True, "replies": False}
PREVIEW_REPLY_MAX_LINES = 8
TOOL_LABEL_MAX = 100


def parse_view(spec):
    """Parses a comma list like 'tools,replies' (empty string = prompts only)."""
    wanted = {p.strip().lower() for p in (spec or "").split(",") if p.strip()}
    return {k: (k in wanted) for k in VIEW_KEYS}


def load_view():
    """Loads the persisted preview view; falls back to defaults if missing or corrupt."""
    view = dict(_VIEW_DEFAULT)
    try:
        with open(VIEW_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in VIEW_KEYS:
            if isinstance(data.get(k), bool):
                view[k] = data[k]
    except Exception:
        pass
    return view


def toggle_view(key):
    """Flips one preview switch ('tools' or 'replies'), persists it, and returns the new view."""
    view = load_view()
    if key in VIEW_KEYS:
        view[key] = not view[key]
        try:
            with open(VIEW_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(view, f)
        except Exception:
            pass
    return view


def get_cache_db():
    """Initializes and returns SQLite database connection for session metadata caching."""
    os.makedirs(os.path.dirname(CACHE_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(CACHE_DB_PATH, timeout=10.0)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                file_path TEXT,
                mtime REAL,
                size_kb REAL,
                cwd TEXT,
                project TEXT,
                model TEXT,
                entrypoint TEXT,
                first_prompt TEXT,
                last_prompt TEXT,
                last_reply TEXT,
                user_turns_count INTEGER,
                total_steps INTEGER,
                total_cost REAL,
                total_duration_ms INTEGER,
                last_timestamp TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mtime ON sessions(mtime DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entrypoint ON sessions(entrypoint)")
    return conn


def clean_prompt_text(text):
    """Cleans up internal Claude Code command caveats, XML tags, and wrappers."""
    if not text:
        return ""
    # Strip local command caveat wrappers
    text = re.sub(r"<local-command-caveat>.*?</local-command-caveat>", "", text, flags=re.DOTALL)
    # Strip command wrappers
    text = re.sub(r"<command-name>.*?</command-name>", "", text, flags=re.DOTALL)
    text = re.sub(r"<command-message>.*?</command-message>", "", text, flags=re.DOTALL)
    text = re.sub(r"<command-args>.*?</command-args>", "", text, flags=re.DOTALL)
    text = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "", text, flags=re.DOTALL)
    # Strip any remaining XML/HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def parse_session_file(full_path):
    """Parses a single Claude Code session JSONL file and extracts key metadata."""
    try:
        stat = os.stat(full_path)
    except OSError:
        return None

    size_kb = round(stat.st_size / 1024, 1)
    mtime = stat.st_mtime
    filename = os.path.basename(full_path)
    session_id = filename[:-6] if filename.endswith(".jsonl") else filename

    cwd = ""
    project = os.path.basename(os.path.dirname(full_path))
    model = ""
    entrypoint = "unknown"
    first_prompt = ""
    last_prompt = ""
    last_reply = ""
    user_turns_count = 0
    total_steps = 0
    total_cost = 0.0
    total_duration_ms = 0
    last_timestamp = ""

    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                total_steps += 1
                try:
                    d = json.loads(line_str)
                except Exception:
                    continue

                if "entrypoint" in d and entrypoint == "unknown":
                    entrypoint = d["entrypoint"]

                if "cwd" in d and not cwd:
                    cwd = d["cwd"]

                if "timestamp" in d and d["timestamp"]:
                    last_timestamp = d["timestamp"]

                stype = d.get("type")

                if stype == "attachment":
                    att = d.get("attachment", {})
                    if att.get("type") == "model":
                        model = att.get("identity", {}).get("marketingName", model)

                elif stype == "cost-state":
                    total_cost = d.get("totalCostUSD", total_cost)
                    total_duration_ms = d.get("totalDuration", total_duration_ms)

                elif stype == "user":
                    msg = d.get("message", {})
                    content = msg.get("content", "")
                    prompt_str = ""
                    if isinstance(content, str):
                        prompt_str = content
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and "text" in item:
                                prompt_str = item["text"]
                                break
                    cleaned = clean_prompt_text(prompt_str)
                    if cleaned:
                        user_turns_count += 1
                        if not first_prompt:
                            first_prompt = cleaned
                        last_prompt = cleaned

                elif stype == "assistant":
                    msg = d.get("message", {})
                    content = msg.get("content", [])
                    reply_str = ""
                    if isinstance(content, str):
                        reply_str = content
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                reply_str = item.get("text", "")
                                break
                    if reply_str.strip():
                        last_reply = reply_str.strip()

    except Exception:
        pass

    if not cwd:
        cwd = os.path.dirname(full_path)

    return {
        "session_id": session_id,
        "file_path": full_path,
        "mtime": mtime,
        "size_kb": size_kb,
        "cwd": cwd,
        "project": project,
        "model": model or "default",
        "entrypoint": entrypoint,
        "first_prompt": first_prompt or "(no user prompt)",
        "last_prompt": last_prompt or "(no user prompt)",
        "last_reply": last_reply or "",
        "user_turns_count": user_turns_count,
        "total_steps": total_steps,
        "total_cost": total_cost,
        "total_duration_ms": total_duration_ms,
        "last_timestamp": last_timestamp or datetime.fromtimestamp(mtime).isoformat(),
    }


def sync_cache():
    """Scans ~/.claude/projects and synchronizes the SQLite cache using file mtime."""
    if not os.path.exists(PROJECTS_DIR):
        return

    conn = get_cache_db()
    cur = conn.cursor()

    cur.execute("SELECT session_id, mtime FROM sessions")
    cached = {row[0]: row[1] for row in cur.fetchall()}

    files_on_disk = {}
    for root, dirs, files in os.walk(PROJECTS_DIR):
        for f in files:
            if f.endswith(".jsonl") and not f.endswith(".bak"):
                full_path = os.path.join(root, f)
                session_id = f[:-6]
                files_on_disk[session_id] = full_path

    # Remove deleted sessions from cache
    deleted_ids = set(cached.keys()) - set(files_on_disk.keys())
    if deleted_ids:
        cur.executemany("DELETE FROM sessions WHERE session_id = ?", [(i,) for i in deleted_ids])

    # Insert or update modified files
    to_update = []
    for session_id, full_path in files_on_disk.items():
        try:
            mtime = os.path.getmtime(full_path)
        except OSError:
            continue

        if session_id not in cached or cached[session_id] != mtime:
            data = parse_session_file(full_path)
            if data:
                to_update.append((
                    data["session_id"],
                    data["file_path"],
                    data["mtime"],
                    data["size_kb"],
                    data["cwd"],
                    data["project"],
                    data["model"],
                    data["entrypoint"],
                    data["first_prompt"],
                    data["last_prompt"],
                    data["last_reply"],
                    data["user_turns_count"],
                    data["total_steps"],
                    data["total_cost"],
                    data["total_duration_ms"],
                    data["last_timestamp"]
                ))

    if to_update:
        cur.executemany("""
            INSERT OR REPLACE INTO sessions (
                session_id, file_path, mtime, size_kb, cwd, project, model,
                entrypoint, first_prompt, last_prompt, last_reply,
                user_turns_count, total_steps, total_cost, total_duration_ms, last_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, to_update)

    conn.commit()
    conn.close()


def query_sessions(show_all=False, query=""):
    """Queries sessions from SQLite cache."""
    sync_cache()
    conn = get_cache_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    sql = "SELECT * FROM sessions"
    clauses = []
    params = []

    if not show_all:
        clauses.append("entrypoint != 'sdk-cli'")

    if query:
        q_like = f"%{query.lower()}%"
        clauses.append("""
            (LOWER(first_prompt) LIKE ? OR
             LOWER(cwd) LIKE ? OR
             LOWER(model) LIKE ? OR
             LOWER(session_id) LIKE ?)
        """)
        params.extend([q_like, q_like, q_like, q_like])

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    sql += " ORDER BY mtime DESC"
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def format_relative_time(mtime_sec):
    """Formats timestamp into human-readable relative time (e.g. 5m ago, 2h ago)."""
    if not mtime_sec:
        return "Never"
    diff = datetime.now().timestamp() - mtime_sec
    secs = max(0, int(diff))
    if secs < 60:
        return "Just now"
    elif secs < 3600:
        return f"{secs // 60}m ago"
    elif secs < 86400:
        return f"{secs // 3600}h ago"
    elif secs < 86400 * 7:
        return f"{secs // 86400}d ago"
    else:
        dt = datetime.fromtimestamp(mtime_sec)
        return dt.strftime("%b %d, %Y")


def format_path(path_str):
    """Formats path with tilde for home directory."""
    if not path_str:
        return "~"
    home = os.path.expanduser("~")
    if path_str.startswith(home):
        return "~" + path_str[len(home):]
    return path_str


def get_session_by_id(session_id):
    """Finds a session by exact ID or prefix match."""
    sync_cache()
    conn = get_cache_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM sessions WHERE session_id = ? OR session_id LIKE ? LIMIT 1",
                (session_id, f"{session_id}%"))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


_CMD_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def preview_prompt_text(raw):
    """Prompt text for the preview. Like clean_prompt_text(), but keeps '/command args' for slash
    commands (clean_prompt_text strips <command-args>, which drops e.g. '/plan <the real request>')."""
    if not raw:
        return ""
    m = _CMD_NAME_RE.search(raw)
    if m:
        args_m = _CMD_ARGS_RE.search(raw)
        cmd_args = (args_m.group(1) if args_m else "").strip()
        if not cmd_args:
            return ""  # bare command like /model: not a prompt
        return f"{m.group(1).strip()} {cmd_args}".strip()
    return clean_prompt_text(raw)


def _squash(text, limit):
    """Collapses whitespace/newlines to single spaces and truncates."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def format_tool_label(name, tool_input):
    """One-line label for a tool_use block, e.g. Bash(git status), Read(tui.py), Grep(pattern)."""
    inp = tool_input if isinstance(tool_input, dict) else {}

    def pick(*keys):
        for k in keys:
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v
        return ""

    if name in ("Bash", "PowerShell"):
        arg = pick("command")
    elif name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = pick("file_path", "notebook_path")
        arg = os.path.basename(path.replace("\\", "/").rstrip("/")) or path
    elif name in ("Grep", "Glob"):
        arg = pick("pattern")
    elif name in ("Agent", "Task"):
        arg = pick("description", "prompt")
    elif name == "WebFetch":
        arg = pick("url")
    elif name == "WebSearch":
        arg = pick("query")
    elif name == "Skill":
        arg = pick("skill")
    else:
        arg = next((v for v in inp.values() if isinstance(v, str) and v.strip()), "")

    if name.startswith("mcp__"):
        parts = name.split("__")
        name = f"{parts[1]}:{parts[-1]}" if len(parts) >= 3 else name
    return f"{name}({_squash(arg, TOOL_LABEL_MAX)})"


def load_turns(file_path):
    """Reads a session JSONL into turns: {index, ts, prompt, events:[('reply', text) | ('tool', label)]}.
    A turn starts at each real user prompt; replies and tool calls follow in the order they happened."""
    turns = []
    cur = None
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
                    continue

                stype = d.get("type")
                content = (d.get("message") or {}).get("content", "")

                if stype == "user":
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = "\n".join(
                            it.get("text", "") for it in content
                            if isinstance(it, dict) and it.get("type") == "text" and it.get("text")
                        )
                    else:
                        text = ""
                    prompt = preview_prompt_text(text)
                    if prompt and not prompt.startswith("[Request interrupted"):
                        cur = {"index": len(turns) + 1, "ts": d.get("timestamp", ""), "prompt": prompt, "events": []}
                        turns.append(cur)

                elif stype == "assistant" and cur is not None:
                    if isinstance(content, str):
                        if content.strip():
                            cur["events"].append(("reply", content.strip()))
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype == "text":
                                txt = (block.get("text") or "").strip()
                                if txt:
                                    cur["events"].append(("reply", txt))
                            elif btype == "tool_use":
                                cur["events"].append(
                                    ("tool", format_tool_label(block.get("name", "Tool"), block.get("input")))
                                )
    except Exception:
        pass
    return turns


def build_resume_cmd(s):
    """Shell command that resumes a session in its original workspace (PowerShell-friendly)."""
    cwd = (s["cwd"] or "").replace("'", "''")
    return f"cd '{cwd}'; claude --resume {s['session_id']} --setting-sources project"


def print_preview(text):
    """Prints preview text (which already carries ANSI colors) with no Rich markup parsing or re-wrapping.
    Prompts and tool labels can contain '[...]' that Rich would otherwise treat as markup."""
    console.print(text, markup=False, highlight=False, emoji=False, soft_wrap=True)


def build_preview(session_id, view=None):
    """Builds a formatted preview panel for fzf or interactive inspection.

    Prompts are always shown; view["tools"] / view["replies"] switch tool calls / assistant replies on or off.
    """
    s = get_session_by_id(session_id)
    if not s:
        return f"{RED}Session {session_id} not found.{RESET}"
    if view is None:
        view = load_view()
    show_tools = bool(view.get("tools"))
    show_replies = bool(view.get("replies"))

    dt = datetime.fromtimestamp(s["mtime"])
    rel_time = format_relative_time(s["mtime"])
    exact_time = dt.strftime("%Y-%m-%d %H:%M:%S")
    disp_path = format_path(s["cwd"])
    is_hidden = s["entrypoint"] == "sdk-cli"
    status_label = f"{RED}HIDDEN (SDK session){RESET}" if is_hidden else f"{GREEN}VISIBLE (Native CLI){RESET}"
    cost_str = f"${s['total_cost']:.3f} USD" if s["total_cost"] > 0 else "Free / Unknown"

    title_text = s["first_prompt"].replace("\n", " ").strip()
    if len(title_text) > 65:
        title_text = title_text[:62] + "..."

    out = []
    out.append(f"{BOLD}{CYAN}╭───────────────────────────────────────────────────────────────────╮{RESET}")
    out.append(f"{BOLD}{CYAN}│{RESET} {BOLD}{WHITE}{title_text:<65}{RESET} {BOLD}{CYAN}│{RESET}")
    out.append(f"{BOLD}{CYAN}╰───────────────────────────────────────────────────────────────────╯{RESET}")
    out.append("")
    out.append(f"{BOLD}Session ID:{RESET}  {GRAY}{s['session_id']}{RESET}")
    out.append(f"{BOLD}Status:{RESET}      {status_label}")
    out.append(f"{BOLD}Model:{RESET}       {GREEN}{s['model']}{RESET}")
    out.append(f"{BOLD}Active:{RESET}      {YELLOW}{rel_time}{RESET} {DIM}({exact_time}){RESET}")
    out.append(f"{BOLD}Stats:{RESET}       {BLUE}{s['user_turns_count']} user turns{RESET} • {CYAN}{s['total_steps']} steps{RESET} • {MAGENTA}{cost_str}{RESET}")
    out.append(f"{BOLD}Workspace:{RESET}   {GREEN}{disp_path}{RESET}")
    chips = "  ".join(
        f"{GREEN if on else GRAY}{'☑' if on else '☐'} {label}{RESET}"
        for label, on in (("Tools", show_tools), ("Replies", show_replies))
    )
    out.append(f"{BOLD}View:{RESET}        {GREEN}☑ Prompts{RESET}  {chips}")
    out.append("")

    turns = load_turns(s["file_path"]) if os.path.exists(s["file_path"]) else []
    if not turns:
        out.append(f"{DIM}(no user prompts found in this session){RESET}")
        out.append("")

    for turn in turns:
        head = "▶ INITIAL USER REQUEST:" if turn["index"] == 1 else f"▶ USER TURN #{turn['index']}:"
        out.append(f"{BOLD}{YELLOW}{head}{RESET}")
        for l in turn["prompt"].splitlines():
            out.append(f"  {GRAY}│{RESET} {l}")
        out.append("")

        if not (show_tools or show_replies):
            continue

        tool_no = 0
        prev_kind = None
        for kind, val in turn["events"]:
            if kind == "tool":
                tool_no += 1
                if show_tools:
                    out.append(f"  {CYAN}▸{RESET} {GRAY}#{tool_no}{RESET} {YELLOW}{val}{RESET}")
                    prev_kind = "tool"
            elif kind == "reply" and show_replies:
                if prev_kind == "tool":
                    out.append("")
                out.append(f"{BOLD}{GREEN}▶ ASSISTANT:{RESET}")
                reply_lines = val.splitlines()
                for l in reply_lines[:PREVIEW_REPLY_MAX_LINES]:
                    out.append(f"  {GRAY}│{RESET} {DIM}{l}{RESET}")
                if len(reply_lines) > PREVIEW_REPLY_MAX_LINES:
                    out.append(f"  {GRAY}│ … [{len(reply_lines) - PREVIEW_REPLY_MAX_LINES} more lines]{RESET}")
                out.append("")
                prev_kind = "reply"
        if prev_kind == "tool":
            out.append("")

    out.append(f"{GRAY}─────────────────────────────────────────────────────────────────────{RESET}")
    out.append(f"{BOLD}Resume CLI:{RESET}  {CYAN}{build_resume_cmd(s)}{RESET}")
    return "\n".join(out)


def show_transcript_pager(session_id):
    """Parses full session trajectory and presents turn-by-turn transcript in pager."""
    s = get_session_by_id(session_id)
    if not s:
        console.print(f"[red]Session {session_id} not found.[/red]")
        return

    full_path = s["file_path"]
    if not os.path.exists(full_path):
        console.print(f"[red]Session file not found at {full_path}[/red]")
        return

    buf = []
    buf.append("=" * 75)
    buf.append(f"CLAUDE CODE TRANSCRIPT: {s['session_id']}")
    buf.append(f"Workspace: {s['cwd']}")
    buf.append(f"Model: {s['model']} | Cost: ${s['total_cost']:.4f} USD")
    buf.append("=" * 75)
    buf.append("")

    turn_idx = 1
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue

                stype = d.get("type")
                ts = d.get("timestamp", "")

                if stype == "user":
                    msg = d.get("message", {})
                    content = msg.get("content", "")
                    prompt_str = ""
                    if isinstance(content, str):
                        prompt_str = content
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict):
                                if item.get("type") == "tool_result":
                                    res_content = str(item.get("content", ""))
                                    buf.append(f"{DIM}{GRAY}◀ Tool Result:{RESET}")
                                    for rline in res_content.strip().splitlines()[:15]:
                                        buf.append(f"  {DIM}{GRAY}{rline}{RESET}")
                                    if len(res_content.strip().splitlines()) > 15:
                                        buf.append(f"  {DIM}{GRAY}... [truncated]{RESET}")
                                elif "text" in item:
                                    prompt_str = item["text"]

                    cleaned = clean_prompt_text(prompt_str)
                    if cleaned:
                        buf.append(f"{BOLD}{YELLOW}━━━ [USER TURN #{turn_idx}] ━━━━━━━━━━━━━ {ts} ━━━{RESET}")
                        buf.append(cleaned)
                        buf.append("")
                        turn_idx += 1

                elif stype == "assistant":
                    msg = d.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, str):
                        if content.strip():
                            buf.append(f"{BOLD}{GREEN}━━━ [ASSISTANT] ━━━━━━━━━━━━━━━━━━━━━━ {ts} ━━━{RESET}")
                            buf.append(content.strip())
                            buf.append("")
                    elif isinstance(content, list):
                        has_header = False
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype == "thinking":
                                th = block.get("thinking", "").strip()
                                if th:
                                    buf.append(f"{DIM}{MAGENTA}[Thinking]: {th[:250]}...{RESET}")
                            elif btype == "text":
                                txt = block.get("text", "").strip()
                                if txt:
                                    if not has_header:
                                        buf.append(f"{BOLD}{GREEN}━━━ [ASSISTANT] ━━━━━━━━━━━━━━━━━━━━━━ {ts} ━━━{RESET}")
                                        has_header = True
                                    buf.append(txt)
                                    buf.append("")
                            elif btype == "tool_use":
                                tname = block.get("name", "Tool")
                                tinput = block.get("input", {})
                                buf.append(f"{DIM}{BLUE}▶ Tool Call: {tname}{RESET}")
                                if isinstance(tinput, dict):
                                    if "command" in tinput:
                                        buf.append(f"  {DIM}$ {tinput['command']}{RESET}")
                                    elif "file_path" in tinput:
                                        buf.append(f"  {DIM}File: {tinput['file_path']}{RESET}")
                                    else:
                                        buf.append(f"  {DIM}Args: {json.dumps(tinput)[:120]}{RESET}")

    except Exception as e:
        buf.append(f"Error reading transcript: {e}")

    transcript_text = "\n".join(buf)

    # Use pager or print
    pager = os.environ.get("PAGER", "less -R" if shutil.which("less") else "")
    if pager:
        try:
            proc = subprocess.Popen(pager, shell=True, stdin=subprocess.PIPE, text=True)
            proc.communicate(input=transcript_text)
            return
        except Exception:
            pass

    # Rich pager fallback
    with console.pager(styles=True):
        console.print(transcript_text)


def copy_to_clipboard(text):
    """Copies text to system clipboard using pyperclip or Windows clip.exe."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass

    if shutil.which("clip.exe"):
        try:
            proc = subprocess.Popen(["clip.exe"], stdin=subprocess.PIPE)
            proc.communicate(input=text.encode("utf-8"))
            return True
        except Exception:
            pass

    if shutil.which("wl-copy"):
        try:
            subprocess.run(["wl-copy"], input=text.encode("utf-8"), check=False)
            return True
        except Exception:
            pass

    return False


def set_resume_command(session_id, cwd):
    """Writes launcher script for PowerShell to cd and launch claude."""
    try:
        with open(RESUME_CMD_FILE, "w", encoding="utf-8") as f:
            f.write(f"Set-Location '{cwd}'; claude --resume {session_id} --setting-sources project\n")
    except Exception as e:
        console.print(f"[red]Failed to write resume command: {e}[/red]")


def resume_session(session_id, launch=False):
    """Hands back the resume command for a session (and copies it).

    With launch=True, instead queues the command for the PowerShell profile wrapper to run
    (the old behavior), which cd's into the workspace and starts Claude.
    """
    s = get_session_by_id(session_id)
    if not s:
        console.print(f"[red]Session {session_id} not found.[/red]")
        return

    if launch:
        set_resume_command(s["session_id"], s["cwd"])
        console.print(f"[bold green]Resuming session {s['session_id'][:8]} in {s['cwd']}...[/bold green]")
        return

    cmd = build_resume_cmd(s)
    copied = copy_to_clipboard(cmd)
    console.print("[bold green]Resume command[/bold green]" + (" [dim](copied to clipboard)[/dim]" if copied else "") + ":\n")
    # Plain write: console.print would wrap a long command and parse '[...]' as markup.
    sys.stdout.write(cmd + "\n")
    sys.stdout.flush()
    return cmd


def resume_latest(launch=False):
    """Resumes (hands back the resume command for) the most recently modified conversation."""
    sessions = query_sessions(show_all=False)
    if not sessions:
        sessions = query_sessions(show_all=True)
    if not sessions:
        console.print("[red]No previous Claude conversations found.[/red]")
        sys.exit(1)
    resume_session(sessions[0]["session_id"], launch=launch)


def unhide_all_sessions():
    """Patches all sdk-cli sessions in ~/.claude/projects to cli so Claude's native /resume sees them."""
    console.print("[bold cyan]Scanning and unhiding all SDK sessions...[/bold cyan]")
    sync_cache()
    conn = get_cache_db()
    cur = conn.cursor()
    cur.execute("SELECT session_id, file_path FROM sessions WHERE entrypoint = 'sdk-cli'")
    rows = cur.fetchall()
    conn.close()

    count = 0
    for sid, full_path in rows:
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            patched = content.replace('"entrypoint":"sdk-cli"', '"entrypoint":"cli"')
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(patched)
            count += 1
        except Exception as e:
            console.print(f"[red]Failed to patch {sid}: {e}[/red]")

    # Resync cache after patching
    sync_cache()
    console.print(f"[bold green]Done! Patched {count} sessions to 'cli' entrypoint.[/bold green]\n")


def list_table(show_all=False, query="", limit=50):
    """Displays non-interactive tabular list of conversations."""
    sessions = query_sessions(show_all=show_all, query=query)
    if not sessions:
        console.print("[yellow]No sessions found matching query.[/yellow]")
        return

    mode_str = "All Sessions (incl. SDK)" if show_all else "Main CLI Sessions"
    displayed = sessions if (limit <= 0 or limit >= len(sessions)) else sessions[:limit]
    limit_note = f" | Showing {len(displayed)} of {len(sessions)}" if len(displayed) < len(sessions) else ""
    console.print(f"\n[bold magenta]Claude Code Sessions[/bold magenta] [dim]({len(sessions)} total | {mode_str}{limit_note})[/dim]")
    console.print(f"{BOLD}{CYAN}{'#':<4} {'ACTIVE':<12} {'STATUS':<9} {'MODEL':<12} {'COST':<8} {'DIRECTORY':<22} {'PROMPT / SESSION ID'}{RESET}")
    console.print(f"{GRAY}{'─'*4} {'─'*12} {'─'*9} {'─'*12} {'─'*8} {'─'*22} {'─'*45}{RESET}")

    for idx, s in enumerate(displayed, 1):
        rel_time = format_relative_time(s["mtime"])
        status = f"{RED}HIDDEN{RESET}" if s["entrypoint"] == "sdk-cli" else f"{GREEN}VISIBLE{RESET}"
        cost_disp = f"${s['total_cost']:.2f}" if s["total_cost"] > 0 else "-"
        cwd_disp = format_path(s["cwd"])
        if len(cwd_disp) > 21:
            cwd_disp = "..." + cwd_disp[-18:]

        prompt_disp = s["first_prompt"].replace("\n", " ").strip()
        if len(prompt_disp) > 45:
            prompt_disp = prompt_disp[:42] + "..."

        idx_str = f"{idx:>2}."
        sid_dim = f"{GRAY}({s['session_id'][:8]}){RESET}"
        console.print(f"{GRAY}{idx_str:<4}{RESET} {YELLOW}{rel_time:<12}{RESET} {status:<18} {GREEN}{s['model'][:11]:<12}{RESET} {MAGENTA}{cost_disp:<8}{RESET} {BLUE}{cwd_disp:<22}{RESET} {WHITE}{prompt_disp:<45}{RESET} {sid_dim}")
    console.print()


# ==============================================================================
# fzf shell: fzf in the top ~75% of the screen, shortcut bar + resume commands in the bottom ~25% (see fzf_ui.py)
# ==============================================================================

def build_fzf_items(state):
    """fzf input for the session list, honoring state: show_all, limit (newest N) and group (by working folder)."""
    show_all = bool(state.get("show_all"))
    sessions = query_sessions(show_all=show_all)
    if not sessions and not show_all:
        sessions = query_sessions(show_all=True)

    entries = []
    for s in sessions:
        rel_time = format_relative_time(s["mtime"])
        tag = "\033[31m[HIDDEN]\033[0m" if s["entrypoint"] == "sdk-cli" else "\033[32m●\033[0m"
        title = s["first_prompt"].replace("\n", " ").replace("\t", " ").strip()
        if len(title) > 38:
            title = title[:35] + "..."
        cost_disp = f"${s['total_cost']:.2f}" if s["total_cost"] > 0 else "-"
        display_col = (
            f"{tag} \033[36m{s['session_id'][:8]}\033[0m  "
            f"\033[1;37m{title:<38}\033[0m  "
            f"\033[33m{rel_time:<10}\033[0m  "
            f"\033[32m{s['model']:<10}\033[0m  "
            f"\033[35m{cost_disp:>7}\033[0m  "
            f"\033[2m{format_path(s['cwd'])}\033[0m"
        )
        cwd = s["cwd"] or ""
        label = os.path.basename(cwd.rstrip("\\/")) or cwd or "(no workspace)"
        entries.append((s["session_id"], display_col, cwd, label, format_path(cwd)))
    return fzf_ui.assemble_items(entries, state)


def build_dir_preview(group_id):
    """Overview of one working folder (the preview for a folder row in grouped mode)."""
    sessions = [s for s in query_sessions(show_all=True) if (s["cwd"] or "") == group_id]
    label = os.path.basename(group_id.rstrip("\\/")) or group_id or "(no workspace)"
    total_cost = sum(s["total_cost"] for s in sessions)
    total_turns = sum(s["user_turns_count"] for s in sessions)
    out = [
        f"{BOLD}{YELLOW}╭{'─' * (len(label) + 6)}╮{RESET}",
        f"{BOLD}{YELLOW}│ ▼ {label} │{RESET}",
        f"{BOLD}{YELLOW}╰{'─' * (len(label) + 6)}╯{RESET}",
        "",
        f"{BOLD}Folder:{RESET}      {CYAN}{group_id or '(none)'}{RESET}",
        f"{BOLD}Sessions:{RESET}    {GREEN}{len(sessions)}{RESET}",
        f"{BOLD}User turns:{RESET}  {BLUE}{total_turns}{RESET}",
        f"{BOLD}Total cost:{RESET}  {MAGENTA}${total_cost:.2f} USD{RESET}",
    ]
    if sessions:
        out.append(f"{BOLD}Last active:{RESET} {YELLOW}{format_relative_time(sessions[0]['mtime'])}{RESET}")
    out += ["", f"{GRAY}─────────────────────────────────────────────────────────────────────{RESET}",
            f"{BOLD}Sessions in this folder:{RESET}", ""]
    for i, s in enumerate(sessions[:15], 1):
        title = s["first_prompt"].replace("\n", " ").strip()
        title = title if len(title) <= 34 else title[:31] + "..."
        out.append(f" {GRAY}{i:2d}.{RESET} {CYAN}{s['session_id'][:6]}{RESET} {title:<34} {GRAY}{format_relative_time(s['mtime'])}{RESET}")
    if len(sessions) > 15:
        out.append(f"{DIM}  ... and {len(sessions) - 15} more{RESET}")
    out += ["", f"{BOLD}{CYAN}Enter or Space folds / unfolds this folder.{RESET}"]
    return "\n".join(out)


def _h_trans(sid, st):
    if sid and not fzf_ui.is_group(sid):
        show_transcript_pager(sid)


def _h_all(arg, st):
    st["show_all"] = not st["show_all"]


def _h_unhide(arg, st):
    unhide_all_sessions()
    try:
        input("\nPress Enter to return to the browser...")
    except (EOFError, KeyboardInterrupt):
        pass


def _h_launch(sid, st):
    if not sid or fzf_ui.is_group(sid):
        return None
    resume_session(sid, launch=True)
    return "quit"


SHELL_HANDLERS = {
    "trans": _h_trans,
    "all": _h_all,
    "unhide": _h_unhide,
    "launch": _h_launch,
}


def _resume_cmd_for(sid):
    s = get_session_by_id(sid) if sid else None
    return build_resume_cmd(s) if s else None


def make_shell_config(launch=False):
    """The claude-chats flavor of the fzf shell: keys, shortcut bar, resume command."""
    return fzf_ui.ShellConfig(
        script=os.path.abspath(__file__),
        title="claude-chats",
        prompt="Resume Claude Session > ",
        bar_items=[
            ("Enter", "Copy Resume Cmd", "bold green"),
            ("t", "Trans", "white"),
            ("y", "Copy ID", "blue"),
            ("a", "All/SDK", "yellow"),
            ("u", "Unhide SDK", "magenta"),
            ("g", "Group", "bold magenta"),
            ("#", "Limit", "bold yellow"),
            ("Space", "Fold/Unfold", "cyan", "group"),
            ("e/c", "Expand/Collapse All", "cyan", "group"),
            ("1", "Tools", "bold green"),
            ("2", "Replies", "bold green"),
            ("j/k", "Move", "dim"),
            ("J/K", "Scroll Preview", "green"),
            ("/", "Search", "white"),
            ("q", "Quit", "bold red"),
        ],
        keys={
            "t": "become(echo trans {1})",
            "a": "become(echo all {1})",
            "u": "become(echo unhide {1})",
        },
        resume_cmd=_resume_cmd_for,
        copy=copy_to_clipboard,
        launch=launch,
    )


def run_fzf_tui(initial_query="", show_all=False, limit=0, launch=False, group=False):
    """Runs the fzf shell. Returns False when the console can't be driven (caller falls back to the Rich menu)."""
    def header_for(st, count):
        # one line inside the (narrow) list pane: keep it under ~70 columns or fzf truncates it
        mode = "All (incl. SDK)" if st.get("show_all") else "Main CLI Chats Only"
        lim = f"Top {st['limit']}" if st.get("limit") else "All"
        grp = "ON" if st.get("group") else "OFF"
        return f"{mode} | {count} sessions | Limit: {lim} [#] | Group: {grp} [g]"

    return fzf_ui.run_shell(
        make_shell_config(launch),
        build_items=build_fzf_items,
        header_for=header_for,
        handlers=SHELL_HANDLERS,
        show_all=show_all,
        initial_query=initial_query,
        limit=limit,
        group=group,
    )


def run_rich_interactive_menu(initial_query="", show_all=False, initial_limit=25, launch=False):
    """Fallback interactive TUI using Rich when fzf is not installed."""
    current_show_all = show_all
    query = initial_query
    display_limit = initial_limit
    last_cmd = ""  # most recent resume command handed out; re-shown under the actions so it stays visible

    while True:
        sessions = query_sessions(show_all=current_show_all, query=query)
        if not sessions and not current_show_all and not query:
            current_show_all = True
            sessions = query_sessions(show_all=True, query=query)

        # Handle zero matching sessions
        if not sessions:
            mode_desc = "[yellow]All Sessions (incl. SDK)[/yellow]" if current_show_all else "[green]Main CLI Sessions[/green]"
            console.print(f"\n[bold magenta]Claude Code Sessions[/bold magenta] [dim](0 total | {mode_desc})[/dim]")
            if query:
                console.print(f"\n[yellow]No sessions found matching filter '[bold cyan]{query}[/bold cyan]'.[/yellow]")
                console.print("\n[bold yellow]Actions:[/bold yellow]")
                console.print("  - Type [bold cyan]c[/bold cyan] or [bold cyan]/[/bold cyan] (then Enter) to [bold green]clear search filter[/bold green]")
                console.print("  - Type [bold magenta]a[/bold magenta] to toggle SDK sessions")
                console.print("  - Type [bold cyan]/<text>[/bold cyan] to search another term")
                console.print("  - Press [bold red]Enter[/bold red] to exit")
            else:
                console.print("[yellow]No Claude Code sessions found on this machine.[/yellow]")
                return

            try:
                choice = input("\nSelect > ").strip()
            except (KeyboardInterrupt, EOFError):
                return
            if not choice:
                return
            if choice.lower() in ("c", "clear") or choice == "/":
                query = ""
                continue
            if choice.startswith("/"):
                query = choice[1:].strip()
                continue
            if choice.lower() == "a":
                current_show_all = not current_show_all
                continue
            continue

        displayed = sessions if (display_limit <= 0 or display_limit >= len(sessions)) else sessions[:display_limit]

        mode_desc = "[yellow]All Sessions (incl. SDK)[/yellow]" if current_show_all else "[green]Main CLI Sessions[/green]"
        query_desc = f" | Filter: '[cyan]{query}[/cyan]'" if query else ""
        limit_desc = f" | Showing {len(displayed)} of {len(sessions)}" if len(displayed) < len(sessions) else ""
        table = Table(
            title=f"Claude Code Sessions ({len(sessions)} total | {mode_desc}{query_desc}{limit_desc})",
            header_style="bold magenta",
            show_lines=False
        )
        table.add_column("#", style="dim", justify="right", no_wrap=True)
        table.add_column("Time", style="cyan", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Model", style="green", no_wrap=True)
        table.add_column("Cost", style="magenta", justify="right", no_wrap=True)
        table.add_column("Directory", style="blue", no_wrap=True, overflow="ellipsis")
        table.add_column("Prompt / Session ID", style="white", no_wrap=True, overflow="ellipsis")

        for idx, s in enumerate(displayed, 1):
            rel_time = format_relative_time(s["mtime"])
            status = "[red]HIDDEN[/red]" if s["entrypoint"] == "sdk-cli" else "[green]VISIBLE[/green]"
            cost_disp = f"${s['total_cost']:.2f}" if s["total_cost"] > 0 else "-"
            cwd_disp = format_path(s["cwd"])
            if len(cwd_disp) > 21:
                cwd_disp = "..." + cwd_disp[-18:]

            prompt_disp = s["first_prompt"].replace("\n", " ").strip()
            if len(prompt_disp) > 52:
                prompt_disp = prompt_disp[:49] + "..."

            table.add_row(
                str(idx),
                rel_time,
                status,
                s["model"],
                cost_disp,
                cwd_disp,
                f"{prompt_disp} [dim]({s['session_id'][:8]})[/dim]"
            )

        console.print(table)

        console.print("\n[bold yellow]Actions:[/bold yellow]")
        console.print(f"  - Enter [bold green]1-{len(displayed)}[/bold green] to copy the resume command for a session (menu stays open)")
        console.print("  - [bold cyan]p <#>[/bold cyan] Preview details  |  [bold cyan]t <#>[/bold cyan] Full transcript  |  [bold cyan]y <#>[/bold cyan] Copy ID")
        console.print("  - [bold cyan]v tools[/bold cyan] / [bold cyan]v replies[/bold cyan] Toggle what [bold cyan]p <#>[/bold cyan] shows (prompts are always shown; [bold cyan]v[/bold cyan] = status)")
        more_hint = f"  |  [bold magenta]m[/bold magenta] More (+25)" if len(sessions) > len(displayed) else ""
        console.print(f"  - [bold magenta]/<text>[/bold magenta] Search filter    |  [bold magenta]a[/bold magenta] Toggle SDK chats{more_hint}")
        console.print("  - [bold magenta]n <#>[/bold magenta] Change limit (e.g. 'n 50' or 'n all')  |  [bold magenta]u[/bold magenta] Unhide all in Claude")
        console.print("  - Press [bold red]Enter[/bold red] to exit")
        if last_cmd:
            console.print("\n[bold green]Last resume command (copied):[/bold green]")
            sys.stdout.write(last_cmd + "\n")
            sys.stdout.flush()

        try:
            choice = input("\nSelect > ").strip()
        except (KeyboardInterrupt, EOFError):
            return

        if not choice:
            return

        # Pagination & limit commands
        if choice.lower() in ("m", "more"):
            display_limit += 25
            continue
        if choice.lower() in ("m all", "more all"):
            display_limit = len(sessions)
            continue
        if choice.lower() in ("c", "clear"):
            query = ""
            continue
        if choice.lower().startswith("n "):
            val = choice[2:].strip().lower()
            if val in ("all", "0"):
                display_limit = len(sessions)
            elif val.isdigit():
                display_limit = max(1, int(val))
            continue

        # Search filter
        if choice.startswith("/"):
            query = choice[1:].strip()
            continue

        if choice.lower() == "a":
            current_show_all = not current_show_all
            continue

        if choice.lower() == "u":
            unhide_all_sessions()
            continue

        # Preview view switches: 'v' (status), 'v tools', 'v replies'
        if choice.lower() == "v" or choice.lower().startswith("v "):
            which = choice[1:].strip().lower()
            if which in VIEW_KEYS:
                toggle_view(which)
            elif which:
                console.print(f"[red]Unknown view '{which}'. Use: v tools | v replies[/red]")
            v = load_view()
            console.print(
                "[bold]Preview view:[/bold] [green]☑ Prompts[/green]  "
                + ("[green]☑ Tools[/green]" if v["tools"] else "[dim]☐ Tools[/dim]") + "  "
                + ("[green]☑ Replies[/green]" if v["replies"] else "[dim]☐ Replies[/dim]")
            )
            continue

        # Subcommands with number (e.g. p 1, t 1, y 1)
        parts = choice.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("p", "t", "y") and arg.isdigit():
            idx = int(arg)
            if 1 <= idx <= len(displayed):
                target_sid = displayed[idx - 1]["session_id"]
                if cmd == "p":
                    print_preview(build_preview(target_sid))
                    input("\nPress Enter to return to sessions list...")
                    continue
                elif cmd == "t":
                    show_transcript_pager(target_sid)
                    continue
                elif cmd == "y":
                    if copy_to_clipboard(target_sid):
                        console.print(f"[bold green]✔ Copied session ID {target_sid} to clipboard.[/bold green]")
                    return

        # Direct number selection -> resume
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(displayed):
                if launch:
                    resume_session(displayed[idx - 1]["session_id"], launch=True)
                    return
                # Hand back the command and keep the menu running
                last_cmd = resume_session(displayed[idx - 1]["session_id"]) or last_cmd
                continue

        console.print("[red]Invalid selection. Try again.[/red]")


def main():
    # Normalize Windows-style flags (/l, /a, /c, /u, /h, /?, /n:50) and strip leading / from queries
    raw_args = sys.argv[1:]
    normalized_args = []
    for a in raw_args:
        if a.lower() in ("/l", "/list"):
            normalized_args.append("-l")
        elif a.lower() in ("/a", "/all"):
            normalized_args.append("-a")
        elif a.lower() in ("/c", "/last", "/continue"):
            normalized_args.append("-c")
        elif a.lower() in ("/u", "/unhide"):
            normalized_args.append("-u")
        elif a.lower() in ("/h", "/help", "/?"):
            normalized_args.append("-h")
        elif a.lower().startswith("/n:"):
            normalized_args.extend(["-n", a[3:]])
        elif a.lower() in ("csess", "/csess", "claude-chats", "/claude-chats"):
            # Avoid filtering on own command/alias name
            pass
        elif a.startswith("/") and len(a) > 1 and not a.startswith("//"):
            # If user passed /searchterm on CLI, treat as searchterm
            normalized_args.append(a[1:])
        else:
            normalized_args.append(a)

    parser = argparse.ArgumentParser(
        description="Interactive TUI and fuzzy search browser for Claude Code sessions."
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="",
        help="Optional search query, session ID, or subcommand ('transcript', 'preview')"
    )
    parser.add_argument(
        "arg2",
        nargs="?",
        default="",
        help="Subcommand argument (e.g. session ID for 'transcript' or 'preview')"
    )
    parser.add_argument(
        "-a", "--all",
        action="store_true",
        help="Show all conversations, including SDK-hidden sessions"
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="Print session list in tabular format and exit"
    )
    parser.add_argument(
        "-c", "--continue", "--last",
        dest="resume_last",
        action="store_true",
        help="Resume the most recent conversation immediately"
    )
    parser.add_argument(
        "-u", "--unhide",
        action="store_true",
        help="Unhide all SDK sessions so Claude Code's native /resume displays them"
    )
    parser.add_argument(
        "-n", "--limit", "--count",
        dest="limit",
        default="25",
        help="Number of sessions to show (e.g. 50, 100, or 'all', default: 25)"
    )
    parser.add_argument(
        "-g", "--group",
        action="store_true",
        help="Start the fzf browser grouped by working folder (toggle with 'g')"
    )
    parser.add_argument(
        "--menu", "--rich",
        dest="force_menu",
        action="store_true",
        help="Force numbered Rich table menu instead of fzf TUI"
    )
    parser.add_argument(
        "--preview",
        metavar="SID",
        help=argparse.SUPPRESS  # Internal flag for fzf preview
    )
    parser.add_argument(
        "--view",
        metavar="LIST",
        default=None,
        help="With --preview / 'preview <id>': comma list of extras to show ('tools', 'replies'; '' = prompts only). Overrides the saved view."
    )
    parser.add_argument(
        "--toggle-view",
        choices=list(VIEW_KEYS),
        default=None,
        help=argparse.SUPPRESS  # Internal flag for fzf key bindings
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Resume by queueing the command for the PowerShell wrapper to run (cd + claude) instead of just handing back the command"
    )
    # Internal: fzf key-binding callbacks (see fzf_ui.py)
    parser.add_argument("--fzf-key", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--state", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--sel", default="", help=argparse.SUPPRESS)

    args = parser.parse_args(normalized_args)

    # fzf key binding: flip a preview switch and exit (must not touch anything else)
    if args.toggle_view:
        toggle_view(args.toggle_view)
        return

    # fzf key-binding callback (modal keys, resume-commands area): prints the fzf action to run, if any.
    if args.fzf_key:
        out = fzf_ui.handle_key(make_shell_config(launch=args.launch), args.fzf_key, args.state, args.sel,
                                copy_id=copy_to_clipboard)
        sys.stdout.write(out)
        sys.stdout.flush()
        return

    # Parse limit value
    limit_val = 25
    if str(args.limit).lower() in ("all", "0", "-1"):
        limit_val = 0
    else:
        try:
            limit_val = max(1, int(args.limit))
        except ValueError:
            limit_val = 25

    # Clear residual resume command script on new run
    if os.path.exists(RESUME_CMD_FILE):
        try:
            os.remove(RESUME_CMD_FILE)
        except OSError:
            pass

    # Helper subcommands
    view_override = parse_view(args.view) if args.view is not None else None
    if args.preview and fzf_ui.is_group(args.preview):
        print_preview(build_dir_preview(fzf_ui.decode_group(args.preview)))   # folder row in grouped mode
        return
    if args.preview:
        print_preview(build_preview(args.preview, view=view_override))
        return
    if args.query == "preview" and args.arg2:
        print_preview(build_preview(args.arg2, view=view_override))
        return
    if args.query == "transcript" and args.arg2:
        show_transcript_pager(args.arg2)
        return

    # Unhide mode
    if args.unhide:
        unhide_all_sessions()
        return

    # Direct resume latest
    if args.resume_last:
        resume_latest(launch=args.launch)
        return

    # Direct list
    if args.list:
        list_table(show_all=args.all, query=args.query, limit=limit_val if limit_val > 0 else 999999)
        return

    # Direct session ID match (UUID prefix)
    if args.query and not args.arg2 and re.match(r"^[0-9a-fA-F\-]{6,}$", args.query):
        s = get_session_by_id(args.query)
        if s:
            resume_session(s["session_id"], launch=args.launch)
            return

    # Interactive TUI: the fzf shell (preview + shortcut bar + resume-commands area) unless forced to the menu,
    # fzf is missing, or there is no console to drive
    if shutil.which("fzf") and not args.force_menu and sys.stdout.isatty():
        if run_fzf_tui(initial_query=args.query, show_all=args.all, limit=limit_val, launch=args.launch,
                       group=args.group):
            return
    run_rich_interactive_menu(initial_query=args.query, show_all=args.all, initial_limit=limit_val, launch=args.launch)


if __name__ == "__main__":
    main()
