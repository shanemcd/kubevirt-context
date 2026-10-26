"""Tests for the shared presentation layer and each command's output.

These assert on what a reader actually sees: the presence of the facts, the
glyph set on a terminal, and the ASCII fallback in a pipe. They do not pin
column widths.
"""

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kubevirt_context import cli, ui, workspace as ws
from kubevirt_context.storage import ensure_workspace


from tests.support import IsolatedTestCase, capture


class FormattingTests(IsolatedTestCase):
    def test_durations(self):
        self.assertEqual(ui.duration(0.84), "840ms")
        self.assertEqual(ui.duration(12.44), "12.4s")
        self.assertEqual(ui.duration(192), "3m12s")
        self.assertEqual(ui.duration(3840), "1h04m")

    def test_sizes(self):
        self.assertEqual(ui.size(512), "512 B")
        self.assertEqual(ui.size(2048), "2 KB")
        self.assertEqual(ui.size(5 * 1024 ** 2), "5.0 MB")
        self.assertEqual(ui.size(1177903104), "1.10 GB")

    def test_counts_are_thousands_separated(self):
        self.assertEqual(ui.count(15768), "15,768")

    def test_relative_and_local_times(self):
        self.assertEqual(ui.ago(None), "unknown")
        self.assertEqual(ui.ago("not-a-date"), "not-a-date")
        self.assertEqual(ui.ago("2999-01-01T00:00:00Z"), "just now")
        self.assertRegex(ui.local_time("2026-10-26T19:05:16.003Z"), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_home_directory_is_shortened(self):
        self.assertEqual(ui.shorten_path(Path.home()), "~")
        self.assertTrue(ui.shorten_path(Path.home() / ".pi" / "mcp.json").startswith("~/"))

    def test_long_paths_keep_both_ends(self):
        long = Path("/a" * 60 + "/workspace")
        shortened = ui.shorten_path(long, limit=30)
        self.assertLessEqual(len(shortened), 30)
        self.assertIn("...", shortened)


class GlyphTests(IsolatedTestCase):
    def test_terminal_gets_glyphs_and_pipes_get_words(self):
        tty, tty_out = capture(terminal=True)
        pipe, pipe_out = capture()

        ui.step(tty, "Cloning")
        ui.done(tty, "Cloned 3 repositories")
        ui.step(pipe, "Cloning")
        ui.done(pipe, "Cloned 3 repositories")

        self.assertIn("▸ Cloning", tty_out.getvalue())
        self.assertIn("✓ Cloned 3 repositories", tty_out.getvalue())
        self.assertIn("* Cloning", pipe_out.getvalue())
        self.assertIn("ok: Cloned 3 repositories", pipe_out.getvalue())
        self.assertNotIn("▸", pipe_out.getvalue())

    def test_piped_output_never_wraps_long_paths(self):
        pipe, out = capture(width=40)
        path = "/very/" + "long/" * 20 + "workspace"
        ui.info(pipe, path)
        self.assertIn(path, out.getvalue())

    def test_warnings_and_failures_go_to_stderr(self):
        cap = capture()
        with patch.object(ui, "console", side_effect=cap.console):
            ui.warn(cap.out, "archived or forked")
            ui.fail(cap.out, "no runtime")
        self.assertIn("warn: archived or forked", cap.stderr)
        self.assertIn("error: no runtime", cap.stderr)
        self.assertNotIn("archived", cap.stdout)


class HelpTests(IsolatedTestCase):
    def run_cli(self, argv):
        console, stream = capture()
        with patch.object(ui, "console", return_value=console):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(argv)
        return code, stream.getvalue()

    def test_no_arguments_prints_grouped_help(self):
        code, out = self.run_cli([])
        self.assertEqual(code, 0)
        for heading in ("Usage", "Setup and maintenance", "Inspect", "Configure", "Housekeeping", "Options"):
            self.assertIn(heading, out)
        for command in ("setup", "sync", "clones", "index", "doctor", "status", "path", "skills", "clean"):
            self.assertIn(command, out)
        self.assertNotIn("<<", out)  # no stray metavar artifacts
        self.assertIn("Examples", out)

    def test_subcommand_help_shows_real_flags(self):
        code, out = self.run_cli(["setup", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("kvctx setup [--only A,B,C] [--workers N]", out)
        self.assertIn("--only", out)
        self.assertIn("--workers", out)

    def test_skills_help_shows_positionals(self):
        _code, out = self.run_cli(["skills", "-h"])
        self.assertIn("kvctx skills [list|install|uninstall] [DIR] [--force]", out)
        self.assertIn("<action>", out)
        self.assertIn("--force", out)


class DoctorOutputTests(IsolatedTestCase):
    def test_all_checks_pass_is_one_summary_line(self):
        console, stream = capture()
        with patch.object(ui, "console", return_value=console), \
             patch.object(ws, "_prereq_report", return_value=([], ["git", "gh"])):
            ws.doctor(con=console)
        self.assertIn("all 2 local checks passed", stream.getvalue())
        self.assertIn("unverified by CLI", stream.getvalue())

    def test_missing_check_is_marked_and_gets_a_fix_line(self):
        tty, stream = capture(terminal=True)
        with patch.object(ui, "console", return_value=tty), \
             patch.object(ws, "have", side_effect=lambda name: name != "codegraph"), \
             patch.object(ws.shutil, "which", side_effect=lambda name: None if name == "codegraph" else f"/usr/bin/{name}"), \
             patch.object(ws, "gh_identity", return_value="octocat"):
            with self.assertRaises(ws.KvctxError) as caught:
                ws.doctor(con=tty)
        output = stream.getvalue()
        self.assertIn("✗ codegraph", output)
        self.assertIn("npm install -g @colbymchenry/codegraph", output)
        self.assertIn("✓ github auth", output)
        self.assertIn("octocat on github.com", output)
        self.assertIn("missing or unavailable: codegraph", str(caught.exception))

    def test_preflight_stays_quiet_on_success(self):
        console, stream = capture()
        with patch.object(ws, "_prereq_report", return_value=([], ["git"])):
            ws.preflight(full=False, con=console)
        self.assertIn("prerequisites ok (1 check)", stream.getvalue())


class StatusOutputTests(IsolatedTestCase):
    state = {
        "initialized": True, "version": "1.6.2", "fileCount": 15768, "nodeCount": 227517,
        "edgeCount": 739954, "dbSizeBytes": 1177903104, "lastIndexed": "2026-10-26T19:05:16.003Z",
        "index": {"state": "complete", "reindexRecommended": False},
        "pendingChanges": {"added": 2, "modified": 1, "removed": 0},
    }

    def test_status_reports_repositories_index_and_unverified_mcp(self):
        console, stream = capture()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=tmp):
            home = Path(tmp)
            repos = [
                ws.Repo("kubevirt", "https://github.com/kubevirt/kubevirt.git", False, False),
                ws.Repo("cdi", "https://github.com/kubevirt/cdi.git", False, False),
                ws.Repo("old", "https://github.com/kubevirt/old.git", True, False),
            ]
            (ws.clones_dir(home) / "kubevirt" / ".git").mkdir(parents=True)
            with patch.object(ws, "read_inventory", return_value=(repos, "2026-10-26T19:04:12Z")), \
                 patch.object(ws, "codegraph_state", return_value=self.state), \
                 patch.object(ui, "console", side_effect=stream.console):
                ws.status(con=console)
        output = stream.getvalue()
        self.assertIn("Workspace", output)
        self.assertIn("1 of 2 cloned", output)
        self.assertIn("1 archived or forked", output)
        self.assertIn("15,768 files", output)
        self.assertIn("1.10 GB", output)
        self.assertIn("MCP prerequisites", output)
        self.assertIn("unverified by CLI", output)
        self.assertIn("1 active repository not cloned: cdi", output)
        self.assertIn("3 files changed since the last index", output)

    def test_status_without_workspace_points_at_setup(self):
        console, stream = capture()
        missing = str(Path(tempfile.gettempdir()) / "kvctx-does-not-exist")
        with patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=missing), \
             patch.object(ws, "codegraph_state", return_value=None), \
             patch.object(ui, "console", side_effect=stream.console):
            ws.status(con=console)
        output = stream.getvalue()
        self.assertIn("does not exist yet", output)
        self.assertIn("run `kvctx setup`", output)

    def test_stale_index_warns(self):
        console, stream = capture()
        stale = dict(self.state, pendingChanges={"added": 0, "modified": 0, "removed": 0},
                     index={"state": "complete", "reindexRecommended": True})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=tmp):
            with patch.object(ws, "read_inventory", return_value=([], None)), \
                 patch.object(ws, "codegraph_state", return_value=stale), \
                 patch.object(ui, "console", side_effect=stream.console):
                ws.status(con=console)
        self.assertIn("built by an older codegraph", stream.getvalue())


class SkillsOutputTests(IsolatedTestCase):
    def test_cli_installs_complete_skill_bundle_and_shared_references(self):
        cap = capture()
        destination = Path.home() / "installed-skills"
        source = ws.skills_root()
        with patch.object(ui, "console", side_effect=cap.console):
            self.assertEqual(cli.main(["skills", "install", str(destination)]), 0)
        skills = sorted(destination.glob("kubevirt-*/SKILL.md"))
        self.assertEqual(len(skills), 6)
        for skill in skills:
            self.assertEqual(skill.read_bytes(), (source / skill.parent.name / "SKILL.md").read_bytes())
        reference = destination / "kubevirt-doctor" / "references" / "prerequisites.md"
        self.assertEqual(reference.read_bytes(), (source / "kubevirt-doctor" / "references" / reference.name).read_bytes())
        self.assertFalse(ws.home_dir().exists())

    def test_skills_list_marks_what_is_installed(self):
        console, stream = capture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            for name in ("kubevirt-context", "kubevirt-zzz"):
                (root / name).mkdir(parents=True)
                (root / name / "SKILL.md").write_text(f"---\ndescription: does {name} things\n---\n")
            installed = Path(tmp) / "installed"
            (installed / "kubevirt-context").mkdir(parents=True)
            (installed / "kubevirt-context" / "SKILL.md").write_text("---\ndescription: x\n---\n")
            with patch.object(ws, "skills_root", return_value=root), \
                 patch.object(ws.Path, "home", return_value=Path(tmp) / "home"):
                # install root is ~/.agents/skills under the patched home
                (Path(tmp) / "home" / ".agents" / "skills" / "kubevirt-context").mkdir(parents=True)
                (Path(tmp) / "home" / ".agents" / "skills" / "kubevirt-context" / "SKILL.md").write_text("x")
                ws.skills_list(con=console)
        output = stream.getvalue()
        self.assertIn("1 of 2 in", output)
        self.assertIn("kubevirt-context", output)
        self.assertIn("does kubevirt-context things", output)

    def test_skills_install_reports_kept_and_installed(self):
        console, stream = capture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            for name in ("kubevirt-a", "kubevirt-b"):
                (root / name).mkdir(parents=True)
                (root / name / "SKILL.md").write_text("---\ndescription: d\n---\n")
            target = Path(tmp) / "out"
            (target / "kubevirt-a").mkdir(parents=True)
            with patch.object(ws, "skills_root", return_value=root):
                ws.skills_install(str(target), False, con=console)
        output = stream.getvalue()
        self.assertIn("kubevirt-a", output)
        self.assertIn("left alone", output)
        self.assertIn("kubevirt-b", output)
        self.assertIn("1 skill(s) installed", output)


class CleanOutputTests(IsolatedTestCase):
    def test_clean_reports_freed_space(self):
        console, stream = capture()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "ws"
            ensure_workspace(home)
            (home / "blob").write_bytes(b"x" * 4096)
            with patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=str(home)), \
                 patch.object(ws, "have", return_value=False):
                ws.clean(con=console)
            self.assertFalse(home.exists())
        output = stream.getvalue()
        self.assertIn("4 KB freed", output)
        self.assertIn("kvctx setup builds a new workspace", output)

    def test_clean_on_missing_workspace_is_not_an_error(self):
        console, stream = capture()
        missing = str(Path(tempfile.gettempdir()) / "kvctx-nothing-here")
        with patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=missing), \
             patch.object(ws, "have", return_value=False):
            ws.clean(con=console)
        self.assertIn("nothing to remove", stream.getvalue())


class IndexOutputTests(IsolatedTestCase):
    def test_index_reports_counts_from_codegraph_json(self):
        console, stream = capture()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=tmp):
            ensure_workspace(Path(tmp))
            (ws.clones_dir() / "kubevirt" / ".git").mkdir(parents=True)
            with patch.object(ws, "run", return_value=""), \
                 patch.object(ws, "require"), \
                 patch.object(ws, "codegraph_state", return_value=StatusOutputTests.state):
                ws.index(con=console)
        output = stream.getvalue()
        self.assertIn("Building the CodeGraph index over 1 repository", output)
        self.assertIn("15,768 files", output)
        self.assertIn("227,517 nodes", output)


class CommandFlowTests(IsolatedTestCase):
    def test_setup_and_sync_report_each_phase(self):
        repo = ws.Repo("kubevirt", "https://github.com/kubevirt/kubevirt.git", False, False)
        for command in ("setup", "sync", "clones"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                cap = capture()
                with patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=tmp), \
                     patch.object(ui, "console", side_effect=cap.console), \
                     patch.object(ws, "preflight"), \
                     patch.object(ws, "discover", return_value=([repo], [])), \
                     patch.object(ws, "_checkout", return_value="cloned"), \
                     patch.object(ws, "index") as index:
                    getattr(ws, command)(only=None, workers=1)
                self.assertIn("Discovering repositories", cap.stdout)
                self.assertIn("1 active", cap.stdout)
                self.assertIn("1 repository", cap.stdout)
                self.assertNotIn("\x1b", cap.stdout)
                self.assertEqual(index.called, command != "clones")
                self.assertFalse((Path(tmp) / ".mcp.json").exists())
                self.assertFalse((Path(tmp) / ".pi").exists())
                if command == "setup":
                    self.assertIn("unverified by CLI", cap.stdout)

    def test_path_stays_plain_and_scriptable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=tmp):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(cli.main(["path"]), 0)
            self.assertEqual(stdout.getvalue(), f"{Path(tmp).resolve()}\n")

    def test_expected_error_has_no_traceback(self):
        cap = capture()
        with patch.object(ui, "console", side_effect=cap.console), \
             patch.object(ws, "index", side_effect=ws.KvctxError("run `kvctx clones` first")):
            self.assertEqual(cli.main(["index"]), 1)
        self.assertIn("error: run `kvctx clones` first", cap.stderr)
        self.assertNotIn("Traceback", cap.stderr)

    def test_invalid_argument_has_usage_hint(self):
        cap = capture()
        with patch.object(ui, "console", side_effect=cap.console):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["setup", "--bad-option"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("error:", cap.stderr)
        self.assertIn("--help", cap.stderr)


if __name__ == "__main__":
    unittest.main()
