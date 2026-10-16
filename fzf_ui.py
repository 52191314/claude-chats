"""
fzf_ui: the fzf-based browser shell (claude-chats look) with the agy-chats shortcut bar and resume-commands area.

Screen layout (terminal rows):

    top ~75%   fzf, running inline (--height=<rows>): instant filtering + big preview
    next       colored shortcut bar (wraps to as many rows as the width needs)
    the rest   "Resume commands" rule + the commands handed out so far (newest last)

fzf cannot draw a full-width strip under its own panes, so *we* paint those bottom rows with absolute cursor
addressing straight to the console (bypassing fzf's pipes) - once before fzf starts, and live from fzf key-binding
helpers (they re-invoke the owning script with `--fzf-key <name> --state <file> --sel <id>`).

Keys are modal, like the old Rich TUI: single letters are shortcuts (fzf's search box is hidden); `/` opens the
search box (letters type); Enter/Esc leave it.  The owning script supplies a ShellConfig and forwards `--fzf-key`
callbacks to handle_key().
"""

import io
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from rich.console import Console
from rich.rule import Rule
from rich.text import Text

CSI = "\x1b["
RESET = "\x1b[0m"

# Shortcuts that build_fzf_cmd() binds itself (the tool adds its own via ShellConfig.keys). All of them must stop
# being shortcuts while the search box is open.
FIXED_MODAL_KEYS = ("/", "r", "y", "q", "j", "k", "J", "K", "1", "2", "g", "#", "e", "c", "space")

# Item ids starting with this are folder (group) header rows in grouped mode, not chats.
GROUP_PREFIX = "dir:"


def is_group(sel: str) -> bool:
    return bool(sel) and sel.startswith(GROUP_PREFIX)


def encode_group(gid: str) -> str:
    """Folder id -> safe token (hex), so odd characters never reach a shell command line."""
    return GROUP_PREFIX + gid.encode("utf-8").hex()


def decode_group(sel: str) -> str:
    try:
        return bytes.fromhex(sel[len(GROUP_PREFIX):]).decode("utf-8", "replace")
    except ValueError:
        return ""


# ==============================================================================
# Console access (writes straight to the terminal, independent of fzf's stdin/stdout)
# ==============================================================================

class ConsoleWriter:
    """Writes text (incl. ANSI/VT sequences) directly to the console and reports its size.

    Windows: CONOUT$ via WriteConsoleW (correct Unicode regardless of code page; VT processing enabled).
    POSIX: /dev/tty.  Tests: CHATS_TTY_PATH=<file> (+ CHATS_TTY_SIZE=160x54) captures output instead.
    """

    def __init__(self):
        self._file = None
        self._handle = None
        self._fd = None
        self._k32 = None
        self._size: Optional[Tuple[int, int]] = None

        test_path = os.environ.get("CHATS_TTY_PATH")
        if test_path:
            self._file = open(test_path, "a", encoding="utf-8", newline="")
            w, _, h = os.environ.get("CHATS_TTY_SIZE", "160x54").partition("x")
            self._size = (int(w), int(h))
        elif os.name == "nt":
            self._open_windows()
        else:
            try:
                self._fd = os.open("/dev/tty", os.O_WRONLY)
            except OSError:
                self._fd = None

    @property
    def ok(self) -> bool:
        return any(x is not None for x in (self._file, self._handle, self._fd))

    def _open_windows(self):
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateFileW.restype = ctypes.c_void_p
            k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
            # GENERIC_READ|GENERIC_WRITE, FILE_SHARE_READ|FILE_SHARE_WRITE, OPEN_EXISTING
            h = k32.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
            if h in (None, 0, ctypes.c_void_p(-1).value):
                return
            k32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
            k32.SetConsoleMode.argtypes = [ctypes.c_void_p, wintypes.DWORD]
            k32.WriteConsoleW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            mode = wintypes.DWORD()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                # ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
                k32.SetConsoleMode(h, mode.value | 0x0001 | 0x0004)
            self._k32, self._handle = k32, h
        except Exception:
            self._handle = None

    def size(self) -> Tuple[int, int]:
        """(columns, rows) of the visible console window."""
        if self._size:
            return self._size
        if self._handle is not None:
            try:
                import ctypes
                from ctypes import wintypes

                class COORD(ctypes.Structure):
                    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

                class SMALL_RECT(ctypes.Structure):
                    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

                class INFO(ctypes.Structure):
                    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wintypes.WORD),
                                ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]

                info = INFO()
                self._k32.GetConsoleScreenBufferInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(INFO)]
                if self._k32.GetConsoleScreenBufferInfo(self._handle, ctypes.byref(info)):
                    w = info.srWindow
                    return (w.Right - w.Left + 1, w.Bottom - w.Top + 1)
            except Exception:
                pass
        if self._fd is not None:
            try:
                sz = os.get_terminal_size(self._fd)
                return (sz.columns, sz.lines)
            except OSError:
                pass
        sz = os.get_terminal_size() if sys.stdout.isatty() else os.terminal_size((120, 30))
        return (sz.columns, sz.lines)

    def write(self, text: str):
        if not text:
            return
        if self._file is not None:
            self._file.write(text)
            self._file.flush()
        elif self._handle is not None:
            import ctypes
            from ctypes import wintypes
            for i in range(0, len(text), 8000):
                chunk = text[i:i + 8000]
                written = wintypes.DWORD()
                self._k32.WriteConsoleW(self._handle, chunk, len(chunk.encode("utf-16-le")) // 2,
                                        ctypes.byref(written), None)
        elif self._fd is not None:
            os.write(self._fd, text.encode("utf-8", errors="replace"))

    def close(self):
        try:
            if self._file is not None:
                self._file.close()
            if self._fd is not None:
                os.close(self._fd)
            if self._handle is not None and self._k32 is not None:
                self._k32.CloseHandle.argtypes = [__import__("ctypes").c_void_p]
                self._k32.CloseHandle(self._handle)
        except Exception:
            pass


# ==============================================================================
# Config
# ==============================================================================

class ShellConfig:
    """Everything tool-specific. Keys are fzf key names (single characters are the modal shortcuts)."""

    def __init__(
        self,
        *,
        script: str,
        title: str,
        prompt: str,
        bar_items: List[Tuple[Any, ...]],               # (key label, description, rich style[, state key]) in bar order
        keys: Dict[str, str],                           # fzf key -> fzf action (may use {sel} and {py})
        resume_cmd: Callable[[str], Optional[str]],     # selected id -> shell command (None = unknown)
        copy: Callable[[str], bool],                    # text -> copied?
        preview_args: str = "--preview {1}",
        extra_fzf_args: Optional[List[str]] = None,
        launch: bool = False,                           # True: the resume key launches instead of copying
    ):
        self.launch = launch
        self.script = script
        self.title = title
        self.prompt = prompt
        self.bar_items = bar_items
        self.keys = keys
        self.resume_cmd = resume_cmd
        self.copy = copy
        self.preview_args = preview_args
        self.extra_fzf_args = extra_fzf_args or []

    @property
    def py(self) -> str:
        return f'"{sys.executable}" "{self.script}"'

    def modal_keys(self) -> List[str]:
        """Single-character keys that must stop being shortcuts while the search box is open."""
        ks = {k for k in self.keys if len(k) == 1}
        ks.update(FIXED_MODAL_KEYS)
        return sorted(ks)


# ==============================================================================
# Shared state (file, because fzf callbacks run in separate processes)
# ==============================================================================

def new_state() -> Dict[str, Any]:
    return {
        "cmds": [], "note": "", "copied": True, "mode": "normal", "show_all": False,
        "limit": 0,            # 0 = all chats; N = only the newest N
        "group": False,        # grouped by folder
        "collapsed": [],       # folder ids that are folded (grouped mode)
        "group_ids": [],       # all folder ids of the current list (for "collapse all")
        "focus": "",           # item id to put the cursor back on after a restart
    }


def assemble_items(entries: List[Tuple[str, str, str, str, str]], state: Dict[str, Any]) -> Tuple[str, int]:
    """Builds fzf input from (item_id, display, group_id, group_name, group_path) entries, newest first.

    Honors state["limit"] (newest N) and state["group"]. In grouped mode every folder gets a header row
    (`dir:<hex>`), placed AFTER its chats because fzf's default layout lists input bottom-up - so on screen the
    header sits above its chats. Returns (text, number of chats).
    """
    limit = int(state.get("limit") or 0)
    if limit > 0:
        entries = entries[:limit]

    if not state.get("group"):
        state["group_ids"] = []
        return "\n".join(f"{i}\t{d}" for i, d, *_ in entries), len(entries)

    groups: Dict[str, List[Any]] = {}       # insertion order = newest activity first
    for item_id, display, gid, gname, gpath in entries:
        g = groups.setdefault(gid, [gname, gpath, []])
        g[2].append((item_id, display))

    collapsed = set(state.get("collapsed", []))
    lines: List[str] = []
    for gid, (gname, gpath, items) in groups.items():
        is_open = gid not in collapsed
        if is_open:
            lines += [f"{i}\t  {d}" for i, d in items]
        arrow = "▼" if is_open else "▶"
        gname = gname.replace("\t", " ")
        lines.append(f"{encode_group(gid)}\t\x1b[1;33m{arrow} {gname}\x1b[0m  "
                     f"\x1b[2m({len(items)} chats)  {gpath}\x1b[0m")
    state["group_ids"] = list(groups.keys())
    return "\n".join(lines), len(entries)


def load_state(path: str) -> Dict[str, Any]:
    st = new_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            st.update(json.load(f))
    except Exception:
        pass
    return st


def save_state(path: str, st: Dict[str, Any]):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    except OSError:
        pass


# ==============================================================================
# Rendering of the bottom area
# ==============================================================================

def _ansi_lines(renderable, cols: int) -> List[str]:
    """Renders a Rich renderable to a list of ANSI strings, wrapped to `cols`."""
    buf = io.StringIO()
    c = Console(file=buf, force_terminal=True, color_system="256", width=cols, legacy_windows=False,
                highlight=False, emoji=False)
    c.print(renderable, end="")
    out = buf.getvalue().rstrip("\n")       # e.g. Rule ends in a newline; don't turn that into a phantom blank row
    return out.split("\n") if out else [""]


def bar_text(cfg: ShellConfig, mode: str, state: Optional[Dict[str, Any]] = None) -> Text:
    """The shortcut bar. A bar item may carry a 4th element - a state key that must be truthy for it to show
    (e.g. the fold/unfold shortcuts only appear in grouped mode)."""
    state = state or {}
    t = Text()
    if mode == "search":
        t.append(" [SEARCH] ", style="bold black on green")
        t.append(" type to filter ", style="bold green")
        t.append("|", style="dim")
        t.append(" [Up/Down] Move ", style="bold cyan")
        t.append("|", style="dim")
        t.append(" [Enter] Keep filter ", style="bold yellow")
        t.append("|", style="dim")
        t.append(" [Esc] Stop searching", style="bold red")
        return t
    t.append(" [LIST] ", style="bold black on yellow")
    for item in cfg.bar_items:
        key, desc, style = item[0], item[1], item[2]
        if len(item) > 3 and not state.get(item[3]):
            continue
        t.append(f" [{key}] {desc} ", style=style)
        t.append("|", style="dim")
    return t


def render_bottom(cfg: ShellConfig, state: Dict[str, Any], cols: int, nrows: int) -> List[str]:
    """Exactly `nrows` ANSI lines: the bar, then the resume-commands rule + list."""
    lines = _ansi_lines(bar_text(cfg, state.get("mode", "normal"), state), cols)
    body_rows = nrows - len(lines) - 1
    if body_rows >= 1:
        cmds = state.get("cmds", [])
        if not cmds:
            note = "nothing yet"
        elif state.get("copied", True):
            note = "newest is on your clipboard"
        else:
            note = "clipboard unavailable - select & copy"
        lines += _ansi_lines(Rule(Text(f" Resume commands ({note}) ", style="bold green"), style="green"), cols)
        if state.get("note"):
            lines.append(f"\x1b[2m{state['note']}{RESET}")
            body_rows -= 1
        if not cmds:
            lines.append(f"\x1b[2mPress Enter (or r) on a chat: its resume command is copied and listed here.{RESET}")
        else:
            shown = cmds
            if len(cmds) > body_rows:
                hidden = len(cmds) - max(0, body_rows - 1)
                shown = cmds[hidden:]
                lines.append(f"\x1b[2m... {hidden} older (all reprinted when you quit){RESET}")
            for i, c in enumerate(shown):
                newest = (i == len(shown) - 1)
                lines.append(f"\x1b[1;36m{c}{RESET}" if newest else f"\x1b[37m{c}{RESET}")
    lines = lines[:nrows]
    lines += [""] * (nrows - len(lines))
    return lines


def geometry(cols: int, rows: int) -> Tuple[int, int, int]:
    """(usable rows, fzf rows, bottom-area rows). One row stays free so nothing ever scrolls."""
    usable = max(10, rows - 1)
    main_h = max(int(usable * 0.75), min(usable - 4, 16))
    return usable, main_h, usable - main_h


def paint_bottom(cfg: ShellConfig, state: Dict[str, Any], writer: ConsoleWriter, save_restore: bool = True):
    cols, rows = writer.size()
    usable, main_h, bottom_h = geometry(cols, rows)
    lines = render_bottom(cfg, state, cols, bottom_h)
    out = ["\x1b7"] if save_restore else []
    for i, line in enumerate(lines):
        out.append(f"{CSI}{main_h + 1 + i};1H{CSI}2K{line}{RESET}")
    out.append("\x1b8" if save_restore else f"{CSI}1;1H")
    writer.write("".join(out))


# ==============================================================================
# fzf callbacks (run as separate processes: `script --fzf-key NAME --state FILE --sel ID`)
# ==============================================================================

def _leave_search(cfg: ShellConfig, state: Dict[str, Any], query: str) -> str:
    """Back to shortcut mode. The search box deliberately stays visible: it shows the active filter, and fzf
    does not reliably apply clear-query + hide-input together when they come back from a helper."""
    state["mode"] = "normal"
    return f"rebind({','.join(cfg.modal_keys())})"


def _do_resume(cfg: ShellConfig, state: Dict[str, Any], sel: str) -> str:
    """Copies the resume command and lists it (fzf keeps running). Returns an fzf action ('' normally)."""
    if is_group(sel):
        return f'become(echo toggledir "{sel}")'       # Enter/r on a folder row folds / unfolds it
    cmd = cfg.resume_cmd(sel) if sel else None
    if not cmd:
        state["note"] = "Nothing selected."
        return ""
    if cfg.launch:
        return f'become(echo launch "{sel}")'    # --launch: leave fzf; the shell launches the chat and exits
    state["note"] = ""
    state["copied"] = bool(cfg.copy(cmd))
    if cmd in state["cmds"]:
        state["cmds"].remove(cmd)       # re-copied: move to the end so it is the newest
    state["cmds"].append(cmd)
    return ""


def handle_key(cfg: ShellConfig, name: str, state_path: str, sel: str, copy_id: Optional[Callable[[str], bool]] = None) -> str:
    """Handles one fzf key callback; returns the fzf action string to run (may be empty)."""
    out = ""
    try:
        out = _handle_key(cfg, name, state_path, sel, copy_id)
        return out
    finally:
        log = os.environ.get("CHATS_UI_LOG")          # opt-in diagnostics: CHATS_UI_LOG=<file>
        if log:
            try:
                with open(log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": name, "sel": sel, "input_state": os.environ.get("FZF_INPUT_STATE"),
                                        "query": os.environ.get("FZF_QUERY"), "out": out}) + "\n")
            except OSError:
                pass


def _handle_key(cfg: ShellConfig, name: str, state_path: str, sel: str, copy_id: Optional[Callable[[str], bool]]) -> str:
    state = load_state(state_path)
    query = os.environ.get("FZF_QUERY", "")
    out = ""

    if name == "slash":
        state["mode"] = "search"
        out = f"show-input+unbind({','.join(cfg.modal_keys())})"
    elif name == "enter":
        if state.get("mode") == "search":
            out = _leave_search(cfg, state, query)
        else:
            out = _do_resume(cfg, state, sel)
    elif name == "esc":
        if state.get("mode") == "search":
            out = _leave_search(cfg, state, query)
        elif query:
            out = "clear-query"            # alone: fzf drops the clear when it is chained with other actions
        else:
            out = "abort"
    elif name == "resume":
        out = _do_resume(cfg, state, sel)
    elif name == "copyid":
        if is_group(sel):
            state["note"] = "That row is a folder - select a chat to copy its ID."
        elif sel and copy_id:
            state["note"] = f"ID copied: {sel}" if copy_id(sel) else f"ID: {sel} (clipboard unavailable)"
    elif name == "space":
        if is_group(sel):                                  # on a chat row Space does nothing
            out = f'become(echo toggledir "{sel}")'
    elif name in ("expandall", "collapseall"):
        if state.get("group"):                             # only meaningful (and only restarts fzf) when grouped
            out = f'become(echo {name} "{sel}")'
    # "repaint" just repaints

    save_state(state_path, state)
    w = ConsoleWriter()
    if w.ok:
        try:
            paint_bottom(cfg, state, w)
        finally:
            w.close()
    return out


# ==============================================================================
# The shell: launches fzf in the top rows, handles what it hands back, relaunches
# ==============================================================================

def build_fzf_cmd(cfg: ShellConfig, state_path: str, main_h: int, header: str, start_pos: int = 1) -> List[str]:
    py = cfg.py
    # --sel={1} (not "--sel {1}"): {1} is empty before the first item exists, and a bare "--sel" would then
    # make the callback fail argument parsing.
    cb = lambda key: f'{py} --fzf-key {key} --state "{state_path}" --sel={{1}}'

    binds: List[str] = [
        # fzf erases everything below its region once while starting up, so repaint our bottom area afterwards.
        f"start:execute-silent({cb('repaint')})",
        # once the list is loaded: repaint, and put the cursor back where it was before a restart
        f"load:execute-silent({cb('repaint')})" + (f"+pos({start_pos})" if start_pos > 1 else ""),
        f"enter:transform({cb('enter')})",
        f"esc:transform({cb('esc')})",
        f"/:transform({cb('slash')})",
        f"r:transform({cb('resume')})",
        f"space:transform({cb('space')})",
        f"e:transform({cb('expandall')})",
        f"c:transform({cb('collapseall')})",
        f"y:execute-silent({cb('copyid')})",
        f"g:become(echo group {{1}})",
        f"#:become(echo limit {{1}})",
        "q:abort",
        "j:down", "k:up", "J:preview-down", "K:preview-up",
        f"1:execute-silent({py} --toggle-view tools)+refresh-preview",
        f"2:execute-silent({py} --toggle-view replies)+refresh-preview",
        f"ctrl-o:execute-silent({py} --toggle-view tools)+refresh-preview",
        f"ctrl-r:execute-silent({py} --toggle-view replies)+refresh-preview",
        "ctrl-d:preview-page-down", "ctrl-u:preview-page-up",
        "pgdn:page-down", "pgup:page-up",
    ]
    for key, action in cfg.keys.items():
        binds.append(f"{key}:{action.replace('{py}', py)}")

    cmd = [
        "fzf",
        "--ansi",
        "--delimiter=\t",
        "--with-nth=2..",
        "--no-input",
        f"--height={main_h}",
        f"--preview={py} {cfg.preview_args}",
        "--preview-window=right:52%:wrap:border-rounded",
        f"--header={header}",
        f"--prompt={cfg.prompt}",
        "--border=rounded",
        "--info=inline",
    ]
    for b in binds:
        cmd.append(f"--bind={b}")
    cmd += cfg.extra_fzf_args
    return cmd


def _repaint_soon(cfg: ShellConfig, state_path: str, writer: ConsoleWriter, proc: "subprocess.Popen",
                  delays: Tuple[float, ...] = (0.25, 0.4, 0.8, 1.5)):
    """fzf erases everything below its region while starting up. The `start` callback repaints too, but it costs a
    Python spawn; repainting straight from this process shortly after launch hides that flicker."""
    import threading
    import time

    def work():
        for d in delays:
            time.sleep(d)
            if proc.poll() is not None:
                return                                   # fzf already left; the main loop owns the screen now
            try:
                paint_bottom(cfg, load_state(state_path), writer, save_restore=True)
            except Exception:
                return

    threading.Thread(target=work, daemon=True).start()


def _prompt_limit(state: Dict[str, Any]):
    cur = state.get("limit") or "all"
    try:
        val = input(f"Number of chats to show (0 = all) [current: {cur}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if val.isdigit():
        state["limit"] = int(val)


def _h_group(arg: str, st: Dict[str, Any]):
    st["group"] = not st.get("group")
    st["collapsed"] = []


def _h_limit(arg: str, st: Dict[str, Any]):
    _prompt_limit(st)


def _h_toggledir(arg: str, st: Dict[str, Any]):
    gid = decode_group(arg)
    collapsed = set(st.get("collapsed", []))
    collapsed.symmetric_difference_update({gid})
    st["collapsed"] = sorted(collapsed)


def _h_expandall(arg: str, st: Dict[str, Any]):
    st["collapsed"] = []


def _h_collapseall(arg: str, st: Dict[str, Any]):
    st["collapsed"] = list(st.get("group_ids", []))


# Handlers every shell gets (they only touch shared state); tools add their own on top.
GENERIC_HANDLERS: Dict[str, Callable[[str, Dict[str, Any]], Optional[str]]] = {
    "group": _h_group,
    "limit": _h_limit,
    "toggledir": _h_toggledir,
    "expandall": _h_expandall,
    "collapseall": _h_collapseall,
}


def _focus_pos(items: str, focus: str) -> int:
    """1-based position of the item with id `focus` in the fzf input (1 if absent)."""
    if not focus:
        return 1
    for i, line in enumerate(items.split("\n"), 1):
        if line.split("\t", 1)[0] == focus:
            return i
    return 1


def run_shell(
    cfg: ShellConfig,
    build_items: Callable[[Dict[str, Any]], Tuple[str, int]],   # state -> (tab-separated fzf input, chat count)
    header_for: Callable[[Dict[str, Any], int], str],           # (state, count) -> one-line header
    handlers: Dict[str, Callable[[str, Dict[str, Any]], Optional[str]]],   # verb -> fn(arg, state); return "quit" to exit
    show_all: bool = False,
    initial_query: str = "",
    limit: int = 0,
    group: bool = False,
) -> bool:
    """Runs the browser until you quit. Returns False if the console can't be driven (caller should fall back)."""
    writer = ConsoleWriter()
    if not writer.ok:
        return False

    all_handlers = dict(GENERIC_HANDLERS)
    all_handlers.update(handlers)

    fd, state_path = tempfile.mkstemp(prefix="chats_ui_", suffix=".json")
    os.close(fd)
    state = new_state()
    state["show_all"] = show_all
    state["limit"] = max(0, int(limit or 0))
    state["group"] = bool(group)
    save_state(state_path, state)

    try:
        while True:
            state = load_state(state_path)
            state["mode"] = "normal"
            items, count = build_items(state)           # may also set state["group_ids"]
            if not items.strip():
                writer.write(f"{CSI}2J{CSI}H")
                print("No conversations found.")
                return True
            cols, rows = writer.size()
            usable, main_h, bottom_h = geometry(cols, rows)
            save_state(state_path, state)

            writer.write(f"{CSI}2J{CSI}H")
            paint_bottom(cfg, state, writer, save_restore=False)

            cmd = build_fzf_cmd(cfg, state_path, main_h, header_for(state, count),
                                start_pos=_focus_pos(items, state.get("focus", "")))
            if initial_query:
                cmd.append(f"--query={initial_query}")
                initial_query = ""
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace")
            _repaint_soon(cfg, state_path, writer, proc)
            out, _ = proc.communicate(input=items)

            token = (out or "").strip().splitlines()
            if proc.returncode != 0 or not token:
                break                                    # Esc / q / error: leave
            verb, _, arg = token[-1].partition(" ")
            arg = arg.strip().strip("\"'")
            writer.write(f"{CSI}2J{CSI}H")               # hand the whole screen to whatever runs next
            state = load_state(state_path)
            if arg:
                state["focus"] = arg                     # come back to the same row after the restart
            fn = all_handlers.get(verb)
            if fn is not None:
                result = fn(arg, state)
                save_state(state_path, state)
                if result == "quit":
                    break
            elif verb == "quit":
                break
    finally:
        final = load_state(state_path)
        try:
            os.remove(state_path)
        except OSError:
            pass
        writer.write(f"{CSI}2J{CSI}H")
        writer.close()
        if final.get("cmds"):
            print("Resume commands from this session:")
            for c in final["cmds"]:
                sys.stdout.write(c + "\n")
            sys.stdout.flush()
    return True
