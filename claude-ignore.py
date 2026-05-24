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


def _find_ignore_match(path: Path) -> tuple[Path, str] | None:
    """Return (ignore_file, reason) for the first .claudeignore that blocks
    `path`, or None. `reason` is `"matched <ignore_file>"` for a pattern
    hit, or `"cannot read <ignore_file>: <err>"` if the file existed but
    couldn't be parsed.

    Corrupt/unreadable .claudeignore counts as a block (fail-closed): if an
    attacker can corrupt one (e.g. via a compromised dep) we'd otherwise
    grant a partial bypass for that file's rules.

    Walks up from `path`'s parent (or `path` itself if it's a directory).
    Each .claudeignore is evaluated independently and ANY match blocks —
    additive-only across files, deliberately stricter than git.
    """
    is_dir = path.is_dir()
    start = path if is_dir else path.parent
    for ignore_file in find_claudeignore_files(start):
        base = ignore_file.parent
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            continue
        if not rel or rel == ".":
            continue

        matcher = GitignoreMatcher()
        try:
            matcher.add(ignore_file.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError) as e:
            return ignore_file, f"cannot read {ignore_file}: {e}"

        if matcher.matches(rel, is_dir=is_dir):
            return ignore_file, f"matched {ignore_file}"
    return None


def run_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    # Dispatch on hook_event_name. PreToolUse blocks reads/edits/etc.
    # before they run; PostToolUse runs after Grep and re-checks the
    # response so search results can't leak content from ignored files.
    if payload.get("hook_event_name") == "PostToolUse":
        return run_post_hook(payload)
    return run_pre_hook(payload)


def run_pre_hook(payload: dict) -> int:
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("path") or ""
    if not target:
        return 0

    target_path = Path(target)
    try:
        resolved = target_path.resolve()
    except OSError:
        return 0

    match = _find_ignore_match(resolved)
    if match:
        _, reason = match
        print(f"claude-ignore: blocked read of {target} ({reason})", file=sys.stderr)
        return 2
    return 0


# Ripgrep formats output with the path at the start of each line, followed
# by `:N:` (content), `:N` (count), `-N-` (context), or nothing (files
# mode). Extract every plausible head-of-line at a `:N` or `-N` boundary,
# plus the whole line; callers discard candidates that don't exist on disk
# and the longest existing one wins.
_GREP_PATH_SEP = re.compile(r"[:-]\d+(?:[:-]|$)")


def _stringify_response(tool_response) -> str:
    """Flatten a tool_response into a newline-separated string suitable for
    line-based path extraction. Some hosts wrap Grep output in a dict
    (e.g. `{"output": "...", "mode": "content"}`); naively `json.dumps`ing
    that escapes newlines and breaks per-line parsing — instead, walk the
    structure and concatenate string values with real newlines."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        return "\n".join(_stringify_response(v) for v in tool_response.values())
    if isinstance(tool_response, list):
        return "\n".join(_stringify_response(v) for v in tool_response)
    return str(tool_response)


def _extract_path_candidates(text: str) -> set[str]:
    candidates: set[str] = set()
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        for m in _GREP_PATH_SEP.finditer(line):
            head = line[: m.start()]
            if head:
                candidates.add(head)
        candidates.add(line)
    return candidates


def run_post_hook(payload: dict) -> int:
    """Filter Grep responses that reference .claudeignore-protected paths.

    Grep's PreToolUse only sees the search root, not the files ripgrep
    actually matched — so secrets can leak via `content` lines. Re-check
    the response here and, if any returned path is protected, replace the
    result with a 'block' decision so Claude never sees the content.
    """
    if payload.get("tool_name") != "Grep":
        return 0

    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response")
    if tool_response is None:
        return 0

    response_text = _stringify_response(tool_response)
    if not response_text:
        return 0

    search_path = tool_input.get("path")
    if search_path:
        try:
            root = Path(search_path).resolve()
        except OSError:
            return 0
        if not root.is_dir():
            root = root.parent
    else:
        root = Path.cwd().resolve()

    blocked: list[Path] = []
    seen: set[Path] = set()
    for tok in _extract_path_candidates(response_text):
        p = Path(tok)
        if not p.is_absolute():
            p = root / p
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if not resolved.exists():
                continue
        except OSError:
            continue
        if _find_ignore_match(resolved):
            blocked.append(resolved)

    if blocked:
        sample = ", ".join(str(p) for p in sorted(blocked)[:3])
        more = f" (+{len(blocked) - 3} more)" if len(blocked) > 3 else ""
        reason = (
            f"claude-ignore: Grep response references files protected by "
            f".claudeignore: {sample}{more}. Re-run with a narrower `path` "
            f'or `glob` exclusion (e.g. "!.env") to avoid these files.'
        )
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


# ---------------------------------------------------------------------------
# Output styling — matches install.sh (TTY-aware colors, summary rows)
# ---------------------------------------------------------------------------

def _tty(stream=sys.stdout) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def _c(code: str, stream=sys.stdout) -> str:
    return f"\033[{code}m" if _tty(stream) else ""


def _bold(s: str, stream=sys.stdout) -> str:
    return f"{_c('1', stream)}{s}{_c('0', stream)}"


def _dim(s: str, stream=sys.stdout) -> str:
    return f"{_c('2', stream)}{s}{_c('0', stream)}"


def _green(s: str, stream=sys.stdout) -> str:
    return f"{_c('32', stream)}{s}{_c('0', stream)}"


def _yellow(s: str, stream=sys.stdout) -> str:
    return f"{_c('33', stream)}{s}{_c('0', stream)}"


def _red(s: str, stream=sys.stdout) -> str:
    return f"{_c('31', stream)}{s}{_c('0', stream)}"


def _tildify(path: Path) -> str:
    """Replace $HOME prefix with ~ for friendlier display."""
    home = str(Path.home())
    s = str(path)
    if s == home or s.startswith(home + os.sep):
        return "~" + s[len(home):]
    return s


def _header(action: str, status: str, color="green") -> str:
    paint = {"green": _green, "yellow": _yellow, "dim": _dim, "red": _red}[color]
    return f"{_bold('claude-ignore')} {_bold(action)}  ·  {paint(status)}"


def _row(label: str, value: str, status: str) -> str:
    return f"  {label:<8} {value:<44} {_dim(status)}"


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
    created = not target.exists()
    if created:
        target.write_text(DEFAULT_CLAUDEIGNORE, encoding="utf-8")
        print(_header("init", "created"))
        file_status = "created with starter patterns"
    else:
        print(_header("init", "unchanged", color="dim"))
        file_status = "already exists"

    print()
    print(_row("file", _tildify(target), file_status))
    print()

    settings = settings_path()
    if not settings.exists():
        print(f"{_yellow('!')} global hook not found at {_tildify(settings)}")
        print("  re-run the installer to configure it:")
        print()
        print(f"    curl -sSL {INSTALL_URL} | bash")
        print()
    elif created:
        print(f"{_bold('next:')} edit {_tildify(target)} to add patterns to block.")
        print()
    return 0


def cmd_upgrade() -> int:
    try:
        # install.sh prints its own styled summary — don't duplicate it.
        result = subprocess.run(
            ["bash", "-c", f"curl -fsSL {INSTALL_URL} | bash"],
            check=False,
        )
        return result.returncode
    except FileNotFoundError:
        print(f"{_red('✗', sys.stderr)} bash or curl not found", file=sys.stderr)
        return 1


def _is_our_hook(command: str, abs_bin: str) -> bool:
    """A hook entry belongs to us if it's the legacy bare name or the
    absolute path the installer now writes."""
    return command == HOOK_COMMAND or command == abs_bin


def cmd_uninstall() -> int:
    settings_p = settings_path()
    bin_p = bin_path()
    version_p = version_file()
    abs_bin = str(bin_p)

    print(_header("uninstall", "done"))
    print()

    # Remove hook entry from settings.json
    hook_status = "not configured"
    if settings_p.exists():
        try:
            data = json.loads(settings_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None

        if isinstance(data, dict):
            hooks = data.get("hooks", {})
            removed = 0
            for event in ("PreToolUse", "PostToolUse"):
                entries = hooks.get(event, [])
                new_entries = []
                for entry in entries:
                    inner = entry.get("hooks", []) if isinstance(entry, dict) else []
                    kept = [
                        h for h in inner
                        if not _is_our_hook(h.get("command", ""), abs_bin)
                    ]
                    if len(kept) != len(inner):
                        removed += len(inner) - len(kept)
                    if kept:
                        entry["hooks"] = kept
                        new_entries.append(entry)
                if new_entries:
                    hooks[event] = new_entries
                else:
                    hooks.pop(event, None)
            if not hooks:
                data.pop("hooks", None)
            else:
                data["hooks"] = hooks
            settings_p.write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8"
            )
            if removed == 0:
                hook_status = "no entries to remove"
            elif removed == 1:
                hook_status = "1 entry removed"
            else:
                hook_status = f"{removed} entries removed"
    print(_row("hook", _tildify(settings_p), hook_status))

    # Remove binary
    if bin_p.exists() or bin_p.is_symlink():
        bin_p.unlink()
        bin_status = "removed"
    else:
        bin_status = "not found"
    print(_row("script", _tildify(bin_p), bin_status))

    if version_p.exists():
        version_p.unlink()
        ver_status = "removed"
    else:
        ver_status = "not found"
    print(_row("version", _tildify(version_p), ver_status))
    print()
    return 0


def cmd_version() -> int:
    version_p = version_file()
    if version_p.exists():
        # Keep this minimal — `claude-ignore --version` is script-friendly.
        print(version_p.read_text(encoding="utf-8").strip())
    else:
        print("unknown")
        print(
            f"  {_dim('not installed via install.sh — run', sys.stderr)}",
            file=sys.stderr,
        )
        print(
            f"  curl -sSL {INSTALL_URL} | bash",
            file=sys.stderr,
        )
    return 0


def cmd_help() -> int:
    def cmd_row(name: str, desc: str) -> str:
        # Pad in plain text, then color — keeps columns aligned in TTYs.
        return f"  {_bold(f'{name:<10}')}  {desc}"

    print(f"{_bold('claude-ignore')} — block Claude Code reads via .claudeignore")
    print()
    print(_bold("USAGE"))
    print(f"  claude-ignore              {_dim('hook mode (reads JSON from stdin)')}")
    print(f"  claude-ignore {_bold('<command>')}")
    print()
    print(_bold("COMMANDS"))
    print(cmd_row("init",      "create a starter .claudeignore in the current dir"))
    print(cmd_row("upgrade",   "reinstall the latest version"))
    print(cmd_row("uninstall", "remove hook config and the installed script"))
    print(cmd_row("version",   "print the installed version"))
    print(cmd_row("help",      "show this message"))
    print()
    print(f"  {_dim(REPO_URL)}")
    print()
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
    print(
        f"{_red('✗', sys.stderr)} unknown command {_bold(cmd, sys.stderr)}",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    cmd_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
