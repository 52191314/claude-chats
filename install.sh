#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_BIN="${HOME}/.local/bin"

mkdir -p "${LOCAL_BIN}"

echo "Installing claude-chats..."

# Make wrapper executable
chmod +x "${SCRIPT_DIR}/claude-chats"
chmod +x "${SCRIPT_DIR}/claude_chats.py"

# Symlink or copy to ~/.local/bin
ln -sf "${SCRIPT_DIR}/claude-chats" "${LOCAL_BIN}/claude-chats"
ln -sf "${SCRIPT_DIR}/claude-chats" "${LOCAL_BIN}/claude-history"
ln -sf "${SCRIPT_DIR}/claude-chats" "${LOCAL_BIN}/csess"
ln -sf "${SCRIPT_DIR}/claude_chats.py" "${LOCAL_BIN}/claude_chats.py"
ln -sf "${SCRIPT_DIR}/fzf_ui.py" "${LOCAL_BIN}/fzf_ui.py"

echo "✔ Symlinked claude-chats, claude-history, and csess into ${LOCAL_BIN}"
echo "Ensure ${LOCAL_BIN} is in your PATH."
echo "Done!"
