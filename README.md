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
hook in `~/.claude/settings.json` (backed up as
`settings.json.bak.<timestamp>` whenever it's modified). The hook then
applies to every project automatically.

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
`MultiEdit` a matching path, the call is denied. `Grep` is double-gated:
a `PostToolUse` hook also inspects the response and blocks it if ripgrep
ended up matching content inside a protected file.

## Commands

| Command                   | Description                                            |
| ------------------------- | ------------------------------------------------------ |
| `claude-ignore init`      | Create a starter `.claudeignore` in the current dir    |
| `claude-ignore upgrade`   | Reinstall the latest version                           |
| `claude-ignore uninstall` | Remove the hook and the script                         |

Running `claude-ignore` with no args is the hook mode (reads JSON from
stdin).

## How it works

Two hooks register together:

- **`PreToolUse`** on `Read|Edit|Write|Glob|Grep|MultiEdit`. Walks up
  from the target file's parent to `/`, collecting every `.claudeignore`
  along the way. If *any* of them match the (symlink-resolved) path, the
  call is denied.
- **`PostToolUse`** on `Grep`. Re-inspects the response after ripgrep
  runs. `Grep`'s `PreToolUse` only sees the search root, so a search
  rooted at the project would otherwise return match lines from
  protected files. This hook extracts each path mentioned in the
  response, checks it against the same `.claudeignore` chain, and
  replaces the result with a block decision (the original content is
  never shown to Claude) telling it to retry with a narrower `path` or
  `glob` exclusion.

This differs from gitignore in two ways:

- **Walk-up starts from the target file**, not the cwd — rules apply
  regardless of where Claude was launched.
- **Fail-closed across files.** A leaf-level `!pattern` cannot
  re-include a file ignored higher up. Negation still works *within* a
  single `.claudeignore`. Unreadable/corrupt `.claudeignore` files also
  fail closed (block the read) rather than silently dropping rules.

### Limitations

- **`Bash` is not hooked.** Shell commands (`cat .env`, `grep -r SECRET .`)
  bypass `.claudeignore` entirely. For true secret protection, combine
  with `permissions.deny` rules in `~/.claude/settings.json` that
  restrict `Bash` access to the same paths.
- **`Glob` results aren't filtered.** Claude can still learn that a
  protected file *exists* (e.g. `Glob("**/.env")` returns paths). Only
  `Grep` is post-filtered, since it can leak file *contents*.

## Manual configuration

If you'd rather not use the installer, drop `claude-ignore.py` anywhere
on your `PATH` (as `claude-ignore`, `chmod +x`) and add this to
`~/.claude/settings.json` — use the **absolute path** to the script, not
the bare name, so an earlier-`PATH` binary can't hijack the hook:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read|Edit|Write|Glob|Grep|MultiEdit",
        "hooks": [{ "type": "command", "command": "/absolute/path/to/claude-ignore" }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Grep",
        "hooks": [{ "type": "command", "command": "/absolute/path/to/claude-ignore" }]
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

## Development

After cloning, run this once to wire the tracked git hooks:

```bash
./scripts/setup
```

This sets `core.hooksPath` to `.githooks/`, which contains a pre-commit
hook that keeps `install.sh`'s embedded SHA256 in lockstep with
`claude-ignore.py`. Without it, commits that touch `claude-ignore.py`
would leave the installer pointing at a stale hash and the next install
would refuse to run.

## License

MIT
