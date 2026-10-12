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


def build_preview(session_id):
    """Builds a formatted preview panel for fzf or interactive inspection."""
    s = get_session_by_id(session_id)
    if not s:
        return f"{RED}Session {session_id} not found.{RESET}"

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
    out.append("")

    if s["first_prompt"] and s["first_prompt"] != "(no user prompt)":
        out.append(f"{BOLD}{YELLOW}▶ INITIAL USER REQUEST:{RESET}")
        lines = s["first_prompt"].strip().splitlines()
        for l in lines[:7]:
            out.append(f"  {GRAY}│{RESET} {l}")
        if len(lines) > 7:
            out.append(f"  {GRAY}│ ... [{len(lines) - 7} more lines]{RESET}")
        out.append("")

    if s["last_reply"]:
        out.append(f"{BOLD}{GREEN}▶ LATEST ASSISTANT RESPONSE:{RESET}")
        lines = s["last_reply"].strip().splitlines()
        for l in lines[:9]:
            out.append(f"  {GRAY}│{RESET} {l}")
        if len(lines) > 9:
            out.append(f"  {GRAY}│ ... [{len(lines) - 9} more lines]{RESET}")
        out.append("")

    out.append(f"{GRAY}─────────────────────────────────────────────────────────────────────{RESET}")
    out.append(f"{BOLD}Resume CLI:{RESET}  {CYAN}cd '{s['cwd']}' ; claude --resume {s['session_id']} --setting-sources project{RESET}")
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


def resume_session(session_id):
    """Prepares and launches the resume command."""
    s = get_session_by_id(session_id)
    if not s:
        console.print(f"[red]Session {session_id} not found.[/red]")
        return

    set_resume_command(s["session_id"], s["cwd"])
    console.print(f"[bold green]Resuming session {s['session_id'][:8]} in {s['cwd']}...[/bold green]")


def resume_latest():
    """Resumes the most recently modified conversation."""
    sessions = query_sessions(show_all=False)
    if not sessions:
        sessions = query_sessions(show_all=True)
    if not sessions:
        console.print("[red]No previous Claude conversations found.[/red]")
        sys.exit(1)
    resume_session(sessions[0]["session_id"])


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


def run_fzf_tui(initial_query="", show_all=False):
    """Runs interactive fuzzy finder using fzf."""
    script_path = os.path.abspath(__file__)
    current_show_all = show_all

    while True:
        sessions = query_sessions(show_all=current_show_all)
        if not sessions and not current_show_all:
            current_show_all = True
            sessions = query_sessions(show_all=True)

        lines = []
        for s in sessions:
            rel_time = format_relative_time(s["mtime"])
            tag = "\033[31m[HIDDEN]\033[0m" if s["entrypoint"] == "sdk-cli" else "\033[32m●\033[0m"
            short_id = s["session_id"][:8]
            title = s["first_prompt"].replace("\n", " ").strip()
            if len(title) > 38:
                title = title[:35] + "..."
            cwd_disp = format_path(s["cwd"])
            cost_disp = f"${s['total_cost']:.2f}" if s["total_cost"] > 0 else "-"

            display_col = (
                f"{tag} \033[36m{short_id}\033[0m  "
                f"\033[1;37m{title:<38}\033[0m  "
                f"\033[33m{rel_time:<10}\033[0m  "
                f"\033[32m{s['model']:<10}\033[0m  "
                f"\033[35m{cost_disp:>7}\033[0m  "
                f"\033[2m{cwd_disp}\033[0m"
            )
            lines.append(f"{s['session_id']}\t{display_col}")

        input_data = "\n".join(lines)
        mode_str = "All Sessions (incl. SDK)" if current_show_all else "Main CLI Chats Only"
        header = (
            f"Mode: {mode_str}\n"
            "ENTER: Resume Session | CTRL-T: Full Transcript | CTRL-Y: Copy ID | CTRL-A: Toggle All | CTRL-U: Unhide | ESC: Quit"
        )

        fzf_cmd = [
            "fzf",
            "--ansi",
            "--delimiter=\t",
            "--with-nth=2..",
            f"--preview=python {script_path} --preview {{1}}",
            "--preview-window=right:52%:wrap",
            f"--header={header}",
            "--prompt=Resume Claude Session > ",
            "--expect=ctrl-t,ctrl-y,ctrl-a,ctrl-u,enter",
            "--height=90%",
            "--border=rounded",
            "--info=inline",
        ]
        if initial_query:
            fzf_cmd.append(f"--query={initial_query}")
            initial_query = ""

        try:
            proc = subprocess.Popen(
                fzf_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True
            )
            stdout, _ = proc.communicate(input=input_data)
        except FileNotFoundError:
            run_rich_interactive_menu(initial_query=initial_query, show_all=show_all)
            return

        if proc.returncode != 0 and not stdout:
            return

        out_lines = stdout.splitlines()
        if not out_lines:
            return

        key = out_lines[0].strip() if len(out_lines) >= 2 else ""
        selected_line = out_lines[1] if len(out_lines) >= 2 else out_lines[0]
        session_id = selected_line.split("\t")[0].strip()

        if key == "ctrl-a":
            current_show_all = not current_show_all
            continue
        elif key == "ctrl-u":
            unhide_all_sessions()
            continue
        elif key == "ctrl-t":
            show_transcript_pager(session_id)
            continue
        elif key == "ctrl-y":
            if copy_to_clipboard(session_id):
                console.print(f"[bold green]✔ Copied session ID {session_id} to clipboard.[/bold green]")
            else:
                console.print(f"Session ID: {session_id}")
            return
        else:
            resume_session(session_id)
            return


def run_rich_interactive_menu(initial_query="", show_all=False, initial_limit=25):
    """Fallback interactive TUI using Rich when fzf is not installed."""
    current_show_all = show_all
    query = initial_query
    display_limit = initial_limit

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
        console.print(f"  - Enter [bold green]1-{len(displayed)}[/bold green] to cd & resume session")
        console.print("  - [bold cyan]p <#>[/bold cyan] Preview details  |  [bold cyan]t <#>[/bold cyan] Full transcript  |  [bold cyan]y <#>[/bold cyan] Copy ID")
        more_hint = f"  |  [bold magenta]m[/bold magenta] More (+25)" if len(sessions) > len(displayed) else ""
        console.print(f"  - [bold magenta]/<text>[/bold magenta] Search filter    |  [bold magenta]a[/bold magenta] Toggle SDK chats{more_hint}")
        console.print("  - [bold magenta]n <#>[/bold magenta] Change limit (e.g. 'n 50' or 'n all')  |  [bold magenta]u[/bold magenta] Unhide all in Claude")
        console.print("  - Press [bold red]Enter[/bold red] to exit")

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

        # Subcommands with number (e.g. p 1, t 1, y 1)
        parts = choice.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("p", "t", "y") and arg.isdigit():
            idx = int(arg)
            if 1 <= idx <= len(displayed):
                target_sid = displayed[idx - 1]["session_id"]
                if cmd == "p":
                    console.print(build_preview(target_sid))
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
                resume_session(displayed[idx - 1]["session_id"])
                return

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
        "--preview",
        metavar="SID",
        help=argparse.SUPPRESS  # Internal flag for fzf preview
    )

    args = parser.parse_args(normalized_args)

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
    if args.preview:
        console.print(build_preview(args.preview))
        return
    if args.query == "preview" and args.arg2:
        console.print(build_preview(args.arg2))
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
        resume_latest()
        return

    # Direct list
    if args.list:
        list_table(show_all=args.all, query=args.query, limit=limit_val if limit_val > 0 else 999999)
        return

    # Direct session ID match (UUID prefix)
    if args.query and not args.arg2 and re.match(r"^[0-9a-fA-F\-]{6,}$", args.query):
        s = get_session_by_id(args.query)
        if s:
            resume_session(s["session_id"])
            return

    # Interactive TUI
    if shutil.which("fzf"):
        run_fzf_tui(initial_query=args.query, show_all=args.all)
    else:
        run_rich_interactive_menu(initial_query=args.query, show_all=args.all, initial_limit=limit_val)


if __name__ == "__main__":
    main()
