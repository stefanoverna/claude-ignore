# claude-ignore

A [Claude Code](https://claude.com/claude-code) hook that blocks reads,
edits, and writes on files matching a `.claudeignore` (gitignore syntax).
Pure Python 3 stdlib — no Node required.

Inspired by [li-zhixin/claude-ignore](https://github.com/li-zhixin/claude-ignore).

## Requirements

- macOS (Linux should work but is untested)
- `python3` ≥ 3.8 (install Xcode CLT: `xcode-select --install`)
- `curl`

## Install

```bash
curl -sSL https://raw.githubusercontent.com/stefanoverna/claude-ignore/main/install.sh | bash
```

This installs the script to `~/.local/bin/` and registers a `PreToolUse`
hook in `~/.claude/settings.json` (backed up as `settings.json.bak` on
first run). The hook then applies to every project automatically.

If `~/.local/bin` is not on your `PATH`, the installer prints the line to
add to your shell rc.

## Usage

In any project:

```bash
claude-ignore init
```

This creates a starter `.claudeignore`. Edit it with the patterns you
want to block:

```gitignore
# Secrets
.env
.env.*
*.pem
secrets/

# Generated
dist/
node_modules/
```

When Claude tries to `Read`, `Edit`, `Write`, `Glob`, `Grep`, or
`MultiEdit` a matching path, the call is denied.

## Commands

| Command                   | Description                                            |
| ------------------------- | ------------------------------------------------------ |
| `claude-ignore init`      | Create a starter `.claudeignore` in the current dir    |
| `claude-ignore upgrade`   | Reinstall the latest version                           |
| `claude-ignore uninstall` | Remove the hook and the script                         |

Running `claude-ignore` with no args is the hook mode (reads JSON from
stdin).

## How it works

For each tool call, the hook walks up from the target file's parent to
`/`, collecting every `.claudeignore` along the way. If *any* of them
match the (symlink-resolved) path, the call is denied.

This differs from gitignore in two ways:

- **Walk-up starts from the target file**, not the cwd — rules apply
  regardless of where Claude was launched.
- **Fail-closed across files.** A leaf-level `!pattern` cannot
  re-include a file ignored higher up. Negation still works *within* a
  single `.claudeignore`.

## Manual configuration

If you'd rather not use the installer, drop `claude-ignore.py` anywhere
on your `PATH` (as `claude-ignore`, `chmod +x`) and add this to
`~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read|Edit|Write|Glob|Grep|MultiEdit",
        "hooks": [{ "type": "command", "command": "claude-ignore" }]
      }
    ]
  }
}
```

## Environment variables

Optional overrides (mainly for tests):

| Variable                       | Default                                  |
| ------------------------------ | ---------------------------------------- |
| `CLAUDE_IGNORE_SETTINGS_PATH`  | `~/.claude/settings.json`                |
| `CLAUDE_IGNORE_BIN_PATH`       | `~/.local/bin/claude-ignore`             |
| `CLAUDE_IGNORE_VERSION_FILE`   | `~/.local/share/claude-ignore/VERSION`   |

## Tests

```bash
python3 test_claude_ignore.py
```

Covers the gitignore matcher, hierarchical discovery, hook invocation
via subprocess, and every subcommand.

## License

MIT
