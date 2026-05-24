#!/usr/bin/env bash
# claude-ignore installer — idempotent. Re-run to upgrade.
#
#   curl -sSL https://raw.githubusercontent.com/stefanoverna/claude-ignore/main/install.sh | bash

set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/stefanoverna/claude-ignore/main"
REPO_API="https://api.github.com/repos/stefanoverna/claude-ignore"
# SHA256 of claude-ignore.py expected on disk after download. Updated in
# lockstep with claude-ignore.py — a mismatch aborts the install. Set
# CLAUDE_IGNORE_SKIP_SHA=1 to bypass (only useful when developing against a
# fork — never recommended for end users).
EXPECTED_SHA256="f36c886e5e73b3d6e7c6e76459db5ec66732735ff5bec34ad3a3c0063390f2fc"
BIN_DIR="$HOME/.local/bin"
BIN_PATH="$BIN_DIR/claude-ignore"
SHARE_DIR="$HOME/.local/share/claude-ignore"
VERSION_FILE="$SHARE_DIR/VERSION"
SETTINGS_DIR="$HOME/.claude"
SETTINGS_PATH="$SETTINGS_DIR/settings.json"
# Hook command is the absolute path resolved at install time. Using a bare
# name like "claude-ignore" would let any earlier-PATH binary hijack it.
HOOK_COMMAND="$BIN_PATH"

if [ -t 1 ]; then
  c_bold=$'\033[1m'; c_dim=$'\033[2m'; c_green=$'\033[32m'
  c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_reset=$'\033[0m'
else
  c_bold=""; c_dim=""; c_green=""; c_yellow=""; c_red=""; c_reset=""
fi

step() { printf '%s· %s%s\n' "$c_dim" "$*" "$c_reset"; }
warn() { printf '%s! %s%s\n' "$c_yellow" "$*" "$c_reset"; }
die()  { printf '%s✗ %s%s\n' "$c_red" "$*" "$c_reset" >&2; exit 1; }

# Replace $HOME prefix with ~ in displayed paths
tildify() { printf '%s' "${1/#$HOME/~}"; }

# --- preflight -------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Xcode Command Line Tools: xcode-select --install"
command -v curl    >/dev/null 2>&1 || die "curl not found"

PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PYMAJ=$(echo "$PYVER" | cut -d. -f1)
PYMIN=$(echo "$PYVER" | cut -d. -f2)
if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 8 ]; }; then
  die "python3 >= 3.8 required (found $PYVER)"
fi

# Capture state BEFORE installing so we can report install vs upgrade.
BEFORE_VERSION=$(cat "$VERSION_FILE" 2>/dev/null || true)

# --- download script -------------------------------------------------------
step "downloading claude-ignore.py"
mkdir -p "$BIN_DIR" "$SHARE_DIR" "$SETTINGS_DIR"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
curl -fsSL "$REPO_RAW/claude-ignore.py" -o "$TMP"
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$TMP" \
  || die "downloaded file failed syntax check"

# Verify the download matches the SHA256 baked into this installer. If they
# disagree, either the installer is stale (mid-release) or the download was
# tampered with — refuse to install either way.
if [ "${CLAUDE_IGNORE_SKIP_SHA:-}" = "1" ]; then
  warn "skipping SHA256 verification (CLAUDE_IGNORE_SKIP_SHA=1)"
elif [ "$EXPECTED_SHA256" = "__CLAUDE_IGNORE_PY_SHA256__" ]; then
  die "installer is missing its embedded SHA256 (build error). Refusing to install."
else
  ACTUAL_SHA256=$(shasum -a 256 "$TMP" | awk '{print $1}')
  if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
    die "SHA256 mismatch for claude-ignore.py
  expected: $EXPECTED_SHA256
  actual:   $ACTUAL_SHA256
This installer may be stale, or the download was tampered with. Aborting."
  fi
fi

mv "$TMP" "$BIN_PATH"
chmod +x "$BIN_PATH"

# --- resolve and record version --------------------------------------------
step "resolving latest version"
VERSION=$(curl -fsSL "$REPO_API/commits/main" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"][:7])' 2>/dev/null \
  || echo "unknown")
printf '%s\n' "$VERSION" > "$VERSION_FILE"

# --- merge hook into settings.json -----------------------------------------
step "configuring hook"

# The heredoc emits a single structured line ("HOOK:added" or "HOOK:unchanged"),
# optionally followed by "BACKUP:<path>". We capture and parse it.
PY_OUT=$(python3 - "$SETTINGS_PATH" "$HOOK_COMMAND" <<'PY'
import json, os, sys, time
from pathlib import Path

path = Path(sys.argv[1])
cmd  = sys.argv[2]

# Treat both the absolute path and the legacy bare name as "ours" so we can
# migrate older installs (which wrote "claude-ignore") to the absolute form.
LEGACY_CMDS = {"claude-ignore"}
def is_ours(c):
    return c == cmd or c in LEGACY_CMDS

original_text = path.read_text(encoding="utf-8") if path.exists() else ""

if path.exists() and path.stat().st_size > 0:
    try:
        data = json.loads(original_text)
    except json.JSONDecodeError as e:
        print(f"ERR:settings.json is not valid JSON ({e})", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print("ERR:settings.json root is not an object", file=sys.stderr)
        sys.exit(1)
else:
    data = {}

hooks = data.setdefault("hooks", {})

# Two hooks register together:
#   PreToolUse — block direct reads/edits/etc. of ignored paths
#   PostToolUse(Grep) — filter Grep responses that reference ignored
#     paths (Grep's PreToolUse only sees the search root, so ripgrep can
#     leak content from ignored files via match lines; this catches that).
TARGETS = [
    ("PreToolUse",  "Read|Edit|Write|Glob|Grep|MultiEdit"),
    ("PostToolUse", "Grep"),
]

had_legacy = False
had_current_all = True
for event, matcher in TARGETS:
    entries = hooks.setdefault(event, [])
    entry = next(
        (e for e in entries if isinstance(e, dict) and e.get("matcher") == matcher),
        None,
    )
    if entry is None:
        entry = {"matcher": matcher, "hooks": []}
        entries.append(entry)
    inner = entry.setdefault("hooks", [])
    if any(isinstance(h, dict) and h.get("command") in LEGACY_CMDS for h in inner):
        had_legacy = True
    if not any(isinstance(h, dict) and h.get("command") == cmd for h in inner):
        had_current_all = False
    # Drop any prior entries that point to "us" (legacy or current), then
    # append a single canonical entry with the absolute path.
    kept = [h for h in inner if not (isinstance(h, dict) and is_ours(h.get("command")))]
    kept.append({"type": "command", "command": cmd})
    entry["hooks"] = kept

new_text = json.dumps(data, indent=2) + "\n"

if had_legacy:
    print("HOOK:migrated")
elif had_current_all and new_text == original_text:
    print("HOOK:unchanged")
else:
    print("HOOK:added")

# Always back up when we're about to change the file. Timestamped so we
# don't clobber previous backups on subsequent upgrades.
if path.exists() and new_text != original_text:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{stamp}")
    backup.write_text(original_text, encoding="utf-8")
    print(f"BACKUP:{backup}")

if new_text != original_text:
    path.write_text(new_text, encoding="utf-8")
PY
)

HOOK_STATE=$(printf '%s\n' "$PY_OUT" | sed -n 's/^HOOK://p')
BACKUP_PATH=$(printf '%s\n' "$PY_OUT" | sed -n 's/^BACKUP://p')

# --- compute change summary ------------------------------------------------
if [ -z "$BEFORE_VERSION" ]; then
  TITLE_STATE="installed"
  SCRIPT_STATE="installed"
elif [ "$BEFORE_VERSION" = "$VERSION" ] && [ "$VERSION" != "unknown" ]; then
  TITLE_STATE="already up to date"
  SCRIPT_STATE="unchanged"
else
  TITLE_STATE="upgraded"
  SCRIPT_STATE="updated"
fi

case "$HOOK_STATE" in
  added)     HOOK_LABEL="added" ;;
  migrated)  HOOK_LABEL="migrated to absolute path" ;;
  *)         HOOK_LABEL="already configured" ;;
esac

# --- summary ---------------------------------------------------------------
printf '\n'
if [ -n "$BEFORE_VERSION" ] && [ "$BEFORE_VERSION" != "$VERSION" ] && [ "$VERSION" != "unknown" ]; then
  printf '%sclaude-ignore%s  %s%s%s → %s%s%s  ·  %s%s%s\n' \
    "$c_bold" "$c_reset" \
    "$c_dim" "$BEFORE_VERSION" "$c_reset" \
    "$c_bold" "$VERSION" "$c_reset" \
    "$c_green" "$TITLE_STATE" "$c_reset"
else
  printf '%sclaude-ignore%s %s%s%s  ·  %s%s%s\n' \
    "$c_bold" "$c_reset" \
    "$c_bold" "$VERSION" "$c_reset" \
    "$c_green" "$TITLE_STATE" "$c_reset"
fi

printf '\n'
printf '  script   %-44s %s%s%s\n' "$(tildify "$BIN_PATH")"      "$c_dim" "$SCRIPT_STATE"   "$c_reset"
printf '  hook     %-44s %s%s%s\n' "$(tildify "$SETTINGS_PATH")" "$c_dim" "$HOOK_LABEL"     "$c_reset"
if [ -n "$BACKUP_PATH" ]; then
  printf '  backup   %-44s %s%s%s\n' "$(tildify "$BACKUP_PATH")" "$c_dim" "created" "$c_reset"
fi
printf '\n'

# --- PATH check ------------------------------------------------------------
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    warn "$(tildify "$BIN_DIR") is not on your PATH."
    printf '  add to %s~/.zshrc%s (or %s~/.bashrc%s):\n\n    export PATH="%s:$PATH"\n\n' \
      "$c_bold" "$c_reset" "$c_bold" "$c_reset" "$(tildify "$BIN_DIR")"
    ;;
esac

# --- next steps (only on first install) ------------------------------------
if [ -z "$BEFORE_VERSION" ]; then
  printf '%snext:%s run %sclaude-ignore init%s in any project.\n\n' \
    "$c_bold" "$c_reset" "$c_bold" "$c_reset"
fi
