#!/usr/bin/env python3
"""
claude-ignore — Claude Code PreToolUse hook that blocks reads of paths
matching patterns in hierarchical .claudeignore files (gitignore syntax).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/stefanoverna/claude-ignore"
INSTALL_URL = "https://raw.githubusercontent.com/stefanoverna/claude-ignore/main/install.sh"
HOOK_COMMAND = "claude-ignore"


def _path_env(env_var: str, default: Path) -> Path:
    """Allow tests to redirect filesystem targets without monkeypatching."""
    override = os.environ.get(env_var)
    return Path(override) if override else default


def version_file() -> Path:
    return _path_env(
        "CLAUDE_IGNORE_VERSION_FILE",
        Path.home() / ".local/share/claude-ignore/VERSION",
    )


def settings_path() -> Path:
    return _path_env(
        "CLAUDE_IGNORE_SETTINGS_PATH",
        Path.home() / ".claude/settings.json",
    )


def bin_path() -> Path:
    return _path_env(
        "CLAUDE_IGNORE_BIN_PATH",
        Path.home() / ".local/bin/claude-ignore",
    )


# ---------------------------------------------------------------------------
# gitignore-style pattern matching
# ---------------------------------------------------------------------------

class Pattern:
    __slots__ = ("regex", "negate", "dir_only")

    def __init__(self, regex: re.Pattern, negate: bool, dir_only: bool):
        self.regex = regex
        self.negate = negate
        self.dir_only = dir_only


def _translate(pattern: str) -> tuple[str, bool]:
    """Translate a gitignore pattern body into a regex string. Returns
    (regex_body, anchored). 'anchored' means the pattern must match from the
    .claudeignore's directory rather than at any depth.

    Per gitignore semantics, a pattern is anchored if it contains a `/`
    anywhere except as a trailing slash. The trailing `/` (directory marker)
    is already stripped by `_compile` before calling us, so checking
    `pattern[:-1]` for `/` is enough to exclude a sole trailing `/`."""
    anchored = "/" in pattern[:-1] or pattern.startswith("/")
    if pattern.startswith("/"):
        pattern = pattern[1:]

    i, n = 0, len(pattern)
    out = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # Handle **
                # Cases: leading "**/", trailing "/**", middle "/**/"
                if i + 2 < n and pattern[i + 2] == "/":
                    if i == 0:
                        out.append("(?:.*/)?")
                        i += 3
                        continue
                    else:
                        # Middle "/**/" — the leading "/" was already emitted
                        # by the previous iteration. Emit "(?:.*/)?" so the
                        # pattern collapses cleanly (e.g. a/**/b → a/(?:.*/)?b
                        # matches a/b, a/x/b, a/x/y/b).
                        out.append("(?:.*/)?")
                        i += 3
                        continue
                elif i + 2 == n:
                    out.append(".*")
                    i += 2
                    continue
                else:
                    # "**" not followed by "/" — treat as "*"
                    out.append("[^/]*")
                    i += 2
                    continue
            else:
                out.append("[^/]*")
                i += 1
                continue
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            # Character class — find matching ]
            j = i + 1
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
                i += 1
            else:
                cls = pattern[i + 1 : j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append("[" + cls + "]")
                i = j + 1
        elif c == "/":
            out.append("/")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1

    return "".join(out), anchored


def _compile(pattern: str) -> Pattern | None:
    pattern = pattern.rstrip()
    if not pattern or pattern.startswith("#"):
        return None
    negate = pattern.startswith("!")
    if negate:
        pattern = pattern[1:]
    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern[:-1]
    if not pattern:
        return None

    body, anchored = _translate(pattern)
    if anchored:
        regex = re.compile(r"\A" + body + r"\Z")
    else:
        # Match at any depth: either at root or after a "/"
        regex = re.compile(r"(?:\A|/)" + body + r"\Z")
    return Pattern(regex, negate, dir_only)


class GitignoreMatcher:
    def __init__(self):
        self.patterns: list[Pattern] = []

    def add(self, lines):
        for raw in lines:
            p = _compile(raw)
            if p:
                self.patterns.append(p)

    def _match_one(self, path: str, is_dir: bool) -> bool:
        ignored = False
        for p in self.patterns:
            if p.dir_only and not is_dir:
                continue
            if p.regex.search(path):
                ignored = not p.negate
        return ignored

    def matches(self, path: str, is_dir: bool = False) -> bool:
        """Return True if `path` (or any ancestor directory) is ignored.
        Path is forward-slash and relative to the project root.
        Mirrors git's behavior: once a directory is ignored, everything
        inside it is ignored too (and cannot be re-included via !pattern
        unless the directory itself is re-included)."""
        parts = path.split("/")
        # Check each ancestor directory first, then the path itself.
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i])
            if self._match_one(ancestor, is_dir=True):
                return True
        return self._match_one(path, is_dir=is_dir)


# ---------------------------------------------------------------------------
# Hook mode
# ---------------------------------------------------------------------------

def find_claudeignore_files(start: Path) -> list[Path]:
    files = []
    current = start.resolve()
    while True:
        candidate = current / ".claudeignore"
        if candidate.exists():
            files.append(candidate)
        parent = current.parent
        if parent == current:
            break
        current = parent
    files.reverse()
    return files


def run_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("path") or ""
    if not target:
        return 0

    target_path = Path(target)
    try:
        resolved = target_path.resolve()
    except OSError:
        return 0
    is_dir = target_path.is_dir()

    # Walk up from the target file's parent (not cwd) — this mirrors git's
    # gitignore lookup and lets .claudeignore files affect reads regardless
    # of where Claude was launched from.
    #
    # NOTE: each .claudeignore is evaluated independently and ANY match
    # blocks the read. This is intentionally stricter than git: a leaf
    # `!pattern` cannot re-include a file ignored by a root .claudeignore.
    # For a security-oriented tool, additive-only semantics are safer.
    start = resolved if is_dir else resolved.parent
    for ignore_file in find_claudeignore_files(start):
        base = ignore_file.parent
        try:
            rel = resolved.relative_to(base).as_posix()
        except ValueError:
            continue
        if not rel or rel == ".":
            continue

        matcher = GitignoreMatcher()
        try:
            matcher.add(ignore_file.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError) as e:
            print(f"claude-ignore: cannot read {ignore_file}: {e}", file=sys.stderr)
            continue

        if matcher.matches(rel, is_dir=is_dir):
            print(
                f"claude-ignore: blocked read of {target} "
                f"(matched {ignore_file})",
                file=sys.stderr,
            )
            return 2
    return 0


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

DEFAULT_CLAUDEIGNORE = """# Patterns follow .gitignore syntax.
# Add files Claude Code should never read.

.env
.env.*
*.pem
*.key
secrets/
"""


def cmd_init() -> int:
    cwd = Path.cwd()
    target = cwd / ".claudeignore"
    if target.exists():
        print(f".claudeignore already exists at {target}")
    else:
        target.write_text(DEFAULT_CLAUDEIGNORE, encoding="utf-8")
        print(f"created {target}")

    settings = settings_path()
    if not settings.exists():
        print(
            f"\nNote: global hook not found at {settings}.\n"
            f"Re-run the installer to configure it:\n"
            f"  curl -sSL {INSTALL_URL} | bash"
        )
    return 0


def cmd_upgrade() -> int:
    print("upgrading claude-ignore...")
    try:
        result = subprocess.run(
            ["bash", "-c", f"curl -fsSL {INSTALL_URL} | bash"],
            check=False,
        )
        return result.returncode
    except FileNotFoundError:
        print("claude-ignore: bash or curl not found", file=sys.stderr)
        return 1


def cmd_uninstall() -> int:
    settings_p = settings_path()
    bin_p = bin_path()
    version_p = version_file()

    # Remove hook entry from settings.json
    if settings_p.exists():
        try:
            data = json.loads(settings_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None

        if isinstance(data, dict):
            hooks = data.get("hooks", {})
            pretool = hooks.get("PreToolUse", [])
            new_pretool = []
            removed = 0
            for entry in pretool:
                inner = entry.get("hooks", []) if isinstance(entry, dict) else []
                kept = [h for h in inner if h.get("command") != HOOK_COMMAND]
                if len(kept) != len(inner):
                    removed += len(inner) - len(kept)
                if kept:
                    entry["hooks"] = kept
                    new_pretool.append(entry)
            if new_pretool:
                hooks["PreToolUse"] = new_pretool
            else:
                hooks.pop("PreToolUse", None)
            if not hooks:
                data.pop("hooks", None)
            else:
                data["hooks"] = hooks
            settings_p.write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8"
            )
            print(f"removed {removed} hook entry/entries from {settings_p}")

    # Remove binary
    if bin_p.exists() or bin_p.is_symlink():
        bin_p.unlink()
        print(f"removed {bin_p}")
    if version_p.exists():
        version_p.unlink()
    print("uninstall complete")
    return 0


def cmd_version() -> int:
    version_p = version_file()
    if version_p.exists():
        print(version_p.read_text(encoding="utf-8").strip())
    else:
        print("unknown (not installed via install.sh)")
    return 0


def cmd_help() -> int:
    print(
        f"""claude-ignore — block Claude Code reads via .claudeignore

Usage:
  claude-ignore              (hook mode — reads JSON from stdin)
  claude-ignore init         create a starter .claudeignore in the current dir
  claude-ignore upgrade      reinstall from {REPO_URL}
  claude-ignore uninstall    remove hook config and the installed script
  claude-ignore --version    print the installed version
  claude-ignore --help       show this message
"""
    )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        return run_hook()
    cmd = argv[0]
    if cmd == "init":
        return cmd_init()
    if cmd == "upgrade":
        return cmd_upgrade()
    if cmd == "uninstall":
        return cmd_uninstall()
    if cmd in ("--version", "-v", "version"):
        return cmd_version()
    if cmd in ("--help", "-h", "help"):
        return cmd_help()
    print(f"claude-ignore: unknown command '{cmd}'", file=sys.stderr)
    cmd_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
