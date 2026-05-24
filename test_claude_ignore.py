#!/usr/bin/env python3
"""Tests for claude-ignore. Run with: python3 test_claude_ignore.py"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent.resolve()
SCRIPT = HERE / "claude-ignore.py"

# Load claude-ignore.py as a module (filename has a hyphen, so we use importlib)
spec = importlib.util.spec_from_file_location("claude_ignore", SCRIPT)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


# ---------------------------------------------------------------------------
# Unit tests: GitignoreMatcher
# ---------------------------------------------------------------------------

class MatcherTests(unittest.TestCase):
    """Unit tests for GitignoreMatcher — pattern translation, matching, negation."""

    def m(self, *lines):
        matcher = ci.GitignoreMatcher()
        matcher.add(lines)
        return matcher

    def test_empty_matcher_matches_nothing(self):
        self.assertFalse(self.m().matches("anything"))
        self.assertFalse(self.m().matches("a/b/c.txt"))

    def test_simple_extension(self):
        m = self.m("*.log")
        self.assertTrue(m.matches("foo.log"))
        self.assertTrue(m.matches("nested/dir/bar.log"))
        self.assertFalse(m.matches("foo.txt"))

    def test_exact_filename(self):
        m = self.m(".env")
        self.assertTrue(m.matches(".env"))
        self.assertTrue(m.matches("sub/.env"))
        self.assertFalse(m.matches(".envfile"))
        self.assertFalse(m.matches("envfile.env.bak"))

    def test_directory_only(self):
        m = self.m("build/")
        # A file named "build" should NOT match a dir-only pattern.
        self.assertFalse(m.matches("build", is_dir=False))
        self.assertTrue(m.matches("build", is_dir=True))

    def test_ancestor_directory_blocks_descendants(self):
        m = self.m("build/")
        # File inside ignored dir is blocked via ancestor match.
        self.assertTrue(m.matches("build/out.js"))
        self.assertTrue(m.matches("build/sub/deep.js"))

    def test_double_star_leading(self):
        m = self.m("**/node_modules/")
        self.assertTrue(m.matches("node_modules", is_dir=True))
        self.assertTrue(m.matches("node_modules/foo.js"))
        self.assertTrue(m.matches("a/b/node_modules/foo.js"))

    def test_double_star_trailing(self):
        m = self.m("logs/**")
        self.assertTrue(m.matches("logs/foo.txt"))
        self.assertTrue(m.matches("logs/a/b/c.txt"))

    def test_double_star_middle(self):
        m = self.m("a/**/b")
        self.assertTrue(m.matches("a/b"))
        self.assertTrue(m.matches("a/x/b"))
        self.assertTrue(m.matches("a/x/y/b"))
        # a/b matches the pattern; therefore a/b/c is ignored via ancestor
        # match (git semantics: everything under an ignored dir is ignored).
        self.assertTrue(m.matches("a/b/c"))
        # But a sibling that doesn't end in `b` is not blocked
        self.assertFalse(m.matches("a/x/c"))

    def test_anchored_pattern(self):
        m = self.m("/foo")
        self.assertTrue(m.matches("foo"))
        self.assertFalse(m.matches("sub/foo"))

    def test_unanchored_pattern(self):
        m = self.m("foo")
        self.assertTrue(m.matches("foo"))
        self.assertTrue(m.matches("sub/foo"))
        self.assertTrue(m.matches("a/b/foo"))

    def test_comments_and_blank_lines(self):
        m = self.m("", "# comment", "  ", "*.log")
        self.assertEqual(len(m.patterns), 1)
        self.assertTrue(m.matches("foo.log"))

    def test_negation_for_sibling_file(self):
        m = self.m("*.log", "!keep.log")
        self.assertTrue(m.matches("foo.log"))
        self.assertFalse(m.matches("keep.log"))

    def test_negation_inside_ignored_dir_does_not_apply(self):
        # Git semantics: a file inside an ignored directory cannot be
        # re-included. We match git here.
        m = self.m("secrets/", "!secrets/public.txt")
        self.assertTrue(m.matches("secrets/public.txt"))

    def test_question_mark(self):
        m = self.m("f?o.txt")
        self.assertTrue(m.matches("foo.txt"))
        self.assertTrue(m.matches("fxo.txt"))
        self.assertFalse(m.matches("foob.txt"))
        # ? doesn't cross /
        self.assertFalse(m.matches("f/o.txt"))

    def test_character_class(self):
        m = self.m("file[12].txt")
        self.assertTrue(m.matches("file1.txt"))
        self.assertTrue(m.matches("file2.txt"))
        self.assertFalse(m.matches("file3.txt"))

    def test_dotted_files(self):
        m = self.m(".env.*")
        self.assertTrue(m.matches(".env.local"))
        self.assertTrue(m.matches(".env.production"))
        self.assertFalse(m.matches(".env"))


# ---------------------------------------------------------------------------
# Unit tests: find_claudeignore_files (hierarchical lookup)
# ---------------------------------------------------------------------------

class FindFilesTests(unittest.TestCase):
    """Walk-up discovery of .claudeignore files."""

    def test_hierarchical_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()  # resolve macOS /tmp symlink
            (root / ".claudeignore").write_text("a\n")
            sub = root / "sub" / "deep"
            sub.mkdir(parents=True)
            (sub.parent / ".claudeignore").write_text("b\n")
            (sub / ".claudeignore").write_text("c\n")

            files = ci.find_claudeignore_files(sub)
            # Root-most first, leaf-most last — strict ordering.
            expected = [
                root / ".claudeignore",
                root / "sub" / ".claudeignore",
                root / "sub" / "deep" / ".claudeignore",
            ]
            self.assertEqual(files, expected)

    def test_no_files_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = ci.find_claudeignore_files(Path(tmp))
            self.assertEqual(files, [])


# ---------------------------------------------------------------------------
# End-to-end: invoke the script as a subprocess
# ---------------------------------------------------------------------------

class HookEndToEndTests(unittest.TestCase):
    """Invoke the script as a subprocess against fixtures on disk."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Resolve symlinks (macOS /tmp -> /private/tmp) so the cwd we pass
        # to the subprocess matches what Path.resolve() returns inside it.
        self.root = Path(self.tmpdir).resolve()
        (self.root / ".claudeignore").write_text(
            "\n".join(
                [
                    ".env",
                    "*.secret",
                    "secrets/",
                    "build/",
                    "**/node_modules/",
                    "*.log",
                ]
            )
            + "\n"
        )
        # Create the files so .is_dir() etc work
        (self.root / ".env").write_text("")
        (self.root / "app.secret").write_text("")
        (self.root / "ok.txt").write_text("")
        (self.root / "foo.log").write_text("")
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "private.txt").write_text("")
        (self.root / "build").mkdir()
        (self.root / "build" / "out.js").write_text("")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "foo.js").write_text("")
        (self.root / "sub" / "node_modules").mkdir(parents=True)
        (self.root / "sub" / "node_modules" / "bar.js").write_text("")
        (self.root / "sub" / "file.txt").write_text("")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_hook(self, payload, cwd=None):
        cwd = cwd or self.root
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(cwd),
        )
        return proc.returncode, proc.stdout, proc.stderr

    def assert_blocked(self, path):
        rc, _, err = self.run_hook({"tool_input": {"file_path": str(path)}})
        self.assertEqual(rc, 2, f"expected block for {path}, stderr: {err}")

    def assert_allowed(self, path):
        rc, _, err = self.run_hook({"tool_input": {"file_path": str(path)}})
        self.assertEqual(rc, 0, f"expected allow for {path}, stderr: {err}")

    def test_blocks_matched_paths(self):
        for p in [
            ".env",
            "app.secret",
            "secrets/private.txt",
            "build/out.js",
            "node_modules/foo.js",
            "sub/node_modules/bar.js",
            "foo.log",
        ]:
            with self.subTest(path=p):
                self.assert_blocked(self.root / p)

    def test_allows_unmatched_paths(self):
        for p in ["ok.txt", "sub/file.txt"]:
            with self.subTest(path=p):
                self.assert_allowed(self.root / p)

    def test_allows_paths_with_no_claudeignore_ancestor(self):
        # /etc/hosts has no .claudeignore in any ancestor → allowed
        self.assert_allowed(Path("/etc/hosts"))

    def test_allows_when_no_file_path_in_payload(self):
        rc, _, _ = self.run_hook({"tool_input": {}})
        self.assertEqual(rc, 0)
        rc, _, _ = self.run_hook({})
        self.assertEqual(rc, 0)

    def test_allows_when_stdin_is_not_json(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="not json at all",
            text=True,
            capture_output=True,
            cwd=str(self.root),
        )
        self.assertEqual(proc.returncode, 0)

    def test_accepts_path_field_alias(self):
        # Some tool calls use `path` instead of `file_path`
        rc, _, _ = self.run_hook({"tool_input": {"path": str(self.root / ".env")}})
        self.assertEqual(rc, 2)

    def test_hierarchical_merge_picks_up_sub_claudeignore(self):
        # A .claudeignore deeper than cwd should still apply, because the
        # lookup walks up from the FILE's parent, not from cwd.
        sub = self.root / "sub"
        (sub / ".claudeignore").write_text("file.txt\n")
        # cwd is still self.root; file is sub/file.txt
        self.assert_blocked(sub / "file.txt")
        # ok.txt at root is still allowed (no matching pattern anywhere)
        self.assert_allowed(self.root / "ok.txt")

    def test_anchored_pattern_relative_to_claudeignore_dir(self):
        # Anchored pattern "/foo" in sub/.claudeignore should match only
        # sub/foo, not sub/x/foo (anchored to sub, not to the project root).
        sub = self.root / "sub"
        (sub / "anchored").mkdir()
        (sub / "anchored" / "foo").write_text("")
        (sub / ".claudeignore").write_text("/foo\n")
        (sub / "foo").write_text("")
        self.assert_blocked(sub / "foo")
        self.assert_allowed(sub / "anchored" / "foo")

    def test_symlink_inside_cwd_is_blocked(self):
        # safe.txt -> .env should still be blocked: resolve() follows the
        # symlink, so the matcher sees the real (ignored) target path.
        link = self.root / "safe.txt"
        os.symlink(self.root / ".env", link)
        self.assert_blocked(link)

    def test_relative_dotdot_path_is_resolved(self):
        # A "../foo" payload from a subdir should resolve to root/foo and
        # be matched normally — paths are resolved before lookup.
        sub = self.root / "sub"
        rc, _, err = self.run_hook(
            {"tool_input": {"file_path": "../.env"}}, cwd=sub
        )
        self.assertEqual(rc, 2, f"expected block, stderr: {err}")

    def test_invalid_utf8_in_claudeignore_does_not_crash(self):
        # Corrupt the .claudeignore — hook should log and continue (exit 0
        # rather than propagating the decode error).
        (self.root / ".claudeignore").write_bytes(b"*.log\n\xff\xfe\n")
        rc, _, err = self.run_hook(
            {"tool_input": {"file_path": str(self.root / "ok.txt")}}
        )
        self.assertEqual(rc, 0)
        self.assertIn("cannot read", err)

    def test_walk_up_works_independent_of_cwd(self):
        # Run hook from a completely unrelated cwd; should still apply
        # the .claudeignore that lives above the target file.
        with tempfile.TemporaryDirectory() as other_cwd:
            rc, _, err = self.run_hook(
                {"tool_input": {"file_path": str(self.root / ".env")}},
                cwd=Path(other_cwd),
            )
            self.assertEqual(rc, 2, f"expected block, stderr: {err}")


# ---------------------------------------------------------------------------
# End-to-end: subcommands
# ---------------------------------------------------------------------------

class SubcommandTests(unittest.TestCase):
    """init / upgrade / uninstall / --version / --help via subprocess."""

    def test_help(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("claude-ignore", proc.stdout)
        self.assertIn("init", proc.stdout)
        self.assertIn("upgrade", proc.stdout)
        self.assertIn("uninstall", proc.stdout)

    def test_unknown_command_exits_nonzero(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "bogus"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown command", proc.stderr)

    def test_init_creates_claudeignore_with_starter_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "init"],
                capture_output=True, text=True, cwd=tmp,
            )
            self.assertEqual(proc.returncode, 0)
            created = Path(tmp) / ".claudeignore"
            self.assertTrue(created.exists())
            content = created.read_text()
            for expected in (".env", ".env.*", "*.pem", "*.key", "secrets/"):
                self.assertIn(expected, content)

    def test_init_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / ".claudeignore"
            existing.write_text("my-custom-pattern\n")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "init"],
                capture_output=True, text=True, cwd=tmp,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(existing.read_text(), "my-custom-pattern\n")
            self.assertIn("already exists", proc.stdout)

    def _env_with_overrides(self, tmp: Path) -> dict:
        env = dict(os.environ)
        env["CLAUDE_IGNORE_SETTINGS_PATH"] = str(tmp / "settings.json")
        env["CLAUDE_IGNORE_VERSION_FILE"] = str(tmp / "VERSION")
        env["CLAUDE_IGNORE_BIN_PATH"] = str(tmp / "claude-ignore")
        return env

    def test_version_reads_version_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            (tmp_p / "VERSION").write_text("abc1234\n")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--version"],
                capture_output=True, text=True,
                env=self._env_with_overrides(tmp_p),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "abc1234")

    def test_version_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--version"],
                capture_output=True, text=True,
                env=self._env_with_overrides(Path(tmp)),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("unknown", proc.stdout)

    def test_uninstall_removes_hook_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            settings = {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read|Edit|Write",
                            "hooks": [
                                {"type": "command", "command": "claude-ignore"},
                                {"type": "command", "command": "other-hook"},
                            ],
                        }
                    ]
                },
                "otherKey": {"keep": "me"},
            }
            (tmp_p / "settings.json").write_text(json.dumps(settings))
            (tmp_p / "VERSION").write_text("v1\n")
            (tmp_p / "claude-ignore").write_text("#!/bin/sh\n")

            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "uninstall"],
                capture_output=True, text=True,
                env=self._env_with_overrides(tmp_p),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            # Other hook preserved, claude-ignore entry gone
            new_settings = json.loads((tmp_p / "settings.json").read_text())
            entries = new_settings["hooks"]["PreToolUse"][0]["hooks"]
            commands = [h["command"] for h in entries]
            self.assertNotIn("claude-ignore", commands)
            self.assertIn("other-hook", commands)
            self.assertEqual(new_settings["otherKey"], {"keep": "me"})

            # Binary + VERSION gone
            self.assertFalse((tmp_p / "claude-ignore").exists())
            self.assertFalse((tmp_p / "VERSION").exists())

    def test_uninstall_clears_empty_hooks_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            settings = {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [
                                {"type": "command", "command": "claude-ignore"}
                            ],
                        }
                    ]
                }
            }
            (tmp_p / "settings.json").write_text(json.dumps(settings))

            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "uninstall"],
                capture_output=True, text=True,
                env=self._env_with_overrides(tmp_p),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            new_settings = json.loads((tmp_p / "settings.json").read_text())
            # hooks key removed entirely when nothing remains
            self.assertNotIn("hooks", new_settings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
