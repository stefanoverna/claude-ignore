# claude-ignore

A [Claude Code](https://claude.com/claude-code) `PreToolUse` hook that
prevents Claude from reading, editing, or writing files matching patterns in
`.claudeignore` files (gitignore syntax).

Inspired by [li-zhixin/claude-ignore](https://github.com/li-zhixin/claude-ignore)
and rewritten to remove the Node.js dependency.

- **No Node required.** Pure Python 3 (stdlib only). `python3` ships with
  Xcode Command Line Tools on macOS — nothing else to install.
- **One-liner install.** Idempotent installer doubles as the upgrade path.
- **Global hook.** Registered once in `~/.claude/settings.json`, applies to
  every project automatically.
- **Hierarchical lookup.** For every read, the hook walks up from the
  target file's parent and applies every `.claudeignore` it finds along
  the way.

## Requirements

- macOS (Linux should work but is untested)
- `python3` ≥ 3.8 (install Xcode CLT: `xcode-select --install`)
- `curl`

## Install

```bash
curl -sSL https://raw.githubusercontent.com/stefanoverna/claude-ignore/main/install.sh | bash
```

The installer:

1. Downloads `claude-ignore` to `~/.local/bin/`
2. Registers a `PreToolUse` hook in `~/.claude/settings.json`
   (backing up your existing file as `settings.json.bak` on first run)
3. Records the installed version in `~/.local/share/claude-ignore/VERSION`

If `~/.local/bin` is not on your `PATH`, the installer prints the line to
add to your shell rc.

## Upgrade

Either re-run the installer (it's idempotent) or:

```bash
claude-ignore upgrade
```

Both download the latest `claude-ignore.py` from `main` and refresh the
recorded version.

## Usage

In any project:

```bash
claude-ignore init
```

This creates a starter `.claudeignore` in the current directory. Edit it
with the patterns you want to block. Example:

```gitignore
# Secrets
.env
.env.*
*.pem
*.key
secrets/

# Generated
dist/
build/
node_modules/

# Personal notes
TODO.local.md
```

When Claude tries to `Read`, `Edit`, `Write`, `Glob`, `Grep`, or
`MultiEdit` a matching path, the hook exits with code `2` and Claude is
told the file is off-limits.

## Commands

| Command                   | Description                                            |
| ------------------------- | ------------------------------------------------------ |
| `claude-ignore`           | Hook mode — reads tool-call JSON from stdin            |
| `claude-ignore init`      | Create a starter `.claudeignore` in the current dir    |
| `claude-ignore upgrade`   | Reinstall the latest version                           |
| `claude-ignore uninstall` | Remove the hook from `settings.json` and the script    |
| `claude-ignore --version` | Print the installed version (commit sha)               |
| `claude-ignore --help`    | Show usage                                             |

## How it works

On every read/edit/write/glob/grep tool call, Claude Code pipes a JSON
payload to the hook's stdin:

```json
{ "tool_input": { "file_path": "/abs/path/to/file" } }
```

The hook:

1. Resolves the target path (following symlinks).
2. Walks up from the file's **parent directory** to the filesystem root,
   collecting every `.claudeignore` it finds.
3. For each `.claudeignore`, computes the file path relative to that
   `.claudeignore`'s directory and tests the patterns. This means an
   anchored pattern like `/foo` in `sub/.claudeignore` matches only
   `sub/foo`, not `sub/x/foo`.
4. Exits `2` as soon as any `.claudeignore` matches; otherwise exits `0`.

### Differences from gitignore

- **Walk-up starts from the target file**, not the cwd. So `.claudeignore`
  rules apply no matter where Claude was launched from.
- **Additive-only across files.** Each `.claudeignore` is evaluated
  independently — if *any* file in the hierarchy blocks the path, the read
  is denied. A leaf-level `!pattern` cannot re-include a file ignored by a
  root-level `.claudeignore`. This is deliberately stricter than git: for
  a security tool, "fail closed" is the right default. Negation
  (`!pattern`) still works *within a single `.claudeignore`* — but not
  across the boundary between files in the same directory tree.
- **Symlinks are resolved** before matching. A symlink `safe.txt → .env`
  is blocked because the resolved target (`.env`) is ignored.

## Manual configuration

If you'd rather not use the installer, drop `claude-ignore.py` anywhere on
your `PATH` (as `claude-ignore`, `chmod +x`) and add this to
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

The script reads three optional env vars (mainly for tests and unusual
setups). When unset, the defaults shown below are used.

| Variable                       | Default                                  |
| ------------------------------ | ---------------------------------------- |
| `CLAUDE_IGNORE_SETTINGS_PATH`  | `~/.claude/settings.json`                |
| `CLAUDE_IGNORE_BIN_PATH`       | `~/.local/bin/claude-ignore`             |
| `CLAUDE_IGNORE_VERSION_FILE`   | `~/.local/share/claude-ignore/VERSION`   |

## Tests

```bash
python3 test_claude_ignore.py
```

38 tests covering: the gitignore matcher (translation, negation, `**`,
character classes, anchored vs unanchored), hierarchical discovery, hook
invocation via subprocess (symlinks, `../` paths, invalid UTF-8, walk-up
across the cwd boundary), and every subcommand (`init`, `--version`,
`uninstall`, `--help`).

## Uninstall

```bash
claude-ignore uninstall
```

Removes the hook entry from `~/.claude/settings.json`, deletes the
installed script, and removes the recorded VERSION. Other entries in
`settings.json` are preserved.

## License

MIT
