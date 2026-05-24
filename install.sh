#!/usr/bin/env bash
# claude-ignore installer — idempotent. Re-run to upgrade.
#
#   curl -sSL https://raw.githubusercontent.com/stefanoverna/claude-ignore/main/install.sh | bash

set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/stefanoverna/claude-ignore/main"
REPO_API="https://api.github.com/repos/stefanoverna/claude-ignore"
BIN_DIR="$HOME/.local/bin"
BIN_PATH="$BIN_DIR/claude-ignore"
SHARE_DIR="$HOME/.local/share/claude-ignore"
VERSION_FILE="$SHARE_DIR/VERSION"
SETTINGS_DIR="$HOME/.claude"
SETTINGS_PATH="$SETTINGS_DIR/settings.json"
HOOK_COMMAND="claude-ignore"

c_blue=$'\033[34m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_reset=$'\033[0m'
info()  { printf '%s==>%s %s\n' "$c_blue" "$c_reset" "$*"; }
ok()    { printf '%s✓%s %s\n'   "$c_green" "$c_reset" "$*"; }
warn()  { printf '%s!%s %s\n'   "$c_yellow" "$c_reset" "$*"; }
die()   { printf '%s✗%s %s\n'   "$c_red" "$c_reset" "$*" >&2; exit 1; }

# --- preflight ---
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Xcode Command Line Tools: xcode-select --install"
command -v curl    >/dev/null 2>&1 || die "curl not found"

PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PYMAJ=$(echo "$PYVER" | cut -d. -f1)
PYMIN=$(echo "$PYVER" | cut -d. -f2)
if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 8 ]; }; then
  die "python3 >= 3.8 required (found $PYVER)"
fi

# --- download script ---
info "Downloading claude-ignore..."
mkdir -p "$BIN_DIR" "$SHARE_DIR" "$SETTINGS_DIR"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
curl -fsSL "$REPO_RAW/claude-ignore.py" -o "$TMP"
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$TMP" || die "downloaded file failed syntax check"
mv "$TMP" "$BIN_PATH"
chmod +x "$BIN_PATH"
ok "Installed $BIN_PATH"

# --- record version (latest commit sha, short) ---
VERSION=$(curl -fsSL "$REPO_API/commits/main" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"][:7])' 2>/dev/null || echo "unknown")
printf '%s\n' "$VERSION" > "$VERSION_FILE"
ok "Version: $VERSION"

# --- merge hook into ~/.claude/settings.json ---
info "Configuring global hook in $SETTINGS_PATH..."
python3 - "$SETTINGS_PATH" "$HOOK_COMMAND" <<'PY'
import json, sys, os
from pathlib import Path

path = Path(sys.argv[1])
cmd  = sys.argv[2]

if path.exists() and path.stat().st_size > 0:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  settings.json is not valid JSON ({e}). Aborting merge.", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print("  settings.json root is not an object. Aborting merge.", file=sys.stderr)
        sys.exit(1)
else:
    data = {}

hooks    = data.setdefault("hooks", {})
pretool  = hooks.setdefault("PreToolUse", [])

# Find or create an entry whose matcher matches Read|Edit|Write|Glob|Grep
target_matcher = "Read|Edit|Write|Glob|Grep|MultiEdit"
entry = None
for e in pretool:
    if isinstance(e, dict) and e.get("matcher") == target_matcher:
        entry = e
        break

if entry is None:
    entry = {"matcher": target_matcher, "hooks": []}
    pretool.append(entry)

inner = entry.setdefault("hooks", [])
already = any(isinstance(h, dict) and h.get("command") == cmd for h in inner)
if already:
    print("  hook already configured — no change")
else:
    inner.append({"type": "command", "command": cmd})
    print("  added hook entry")

# Backup once
if path.exists():
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  backup written to {backup}")

path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(f"  wrote {path}")
PY
ok "Hook configured"

# --- PATH check ---
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    warn "$BIN_DIR is not in your PATH."
    warn "Add this to ~/.zshrc (or ~/.bashrc) and restart your shell:"
    printf '\n  export PATH="%s:$PATH"\n\n' "$BIN_DIR"
    ;;
esac

# --- done ---
cat <<EOF

${c_green}claude-ignore installed.${c_reset}

Next steps:
  1. ${c_blue}cd${c_reset} into a project
  2. ${c_blue}claude-ignore init${c_reset}   (creates a starter .claudeignore)
  3. Edit ${c_blue}.claudeignore${c_reset} with patterns (gitignore syntax)

Commands:
  claude-ignore init        starter .claudeignore in current dir
  claude-ignore upgrade     reinstall the latest version
  claude-ignore uninstall   remove the hook and script
  claude-ignore --version   print installed version

EOF
