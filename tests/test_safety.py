"""Regression tests for ownership, configuration, cancellation, and failures.

All filesystem state and user config live in temporary directories. No GitHub,
container daemon, agent, or research checkout is used by these tests.
"""

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kubevirt_context import cli, github_api, workspace as ws
from kubevirt_context.errors import KvctxError
from kubevirt_context.storage import MARKER, atomic_write, ensure_workspace, workspace_lock
from tests.support import IsolatedTestCase, capture


class OwnershipTests(IsolatedTestCase):
    def test_clean_refuses_arbitrary_directory_without_touching_user_config(self):
        home = ws.home_dir()
        home.mkdir()
        important = home / "keep.txt"
        important.write_text("important")
        with self.assertRaisesRegex(KvctxError, "not owned"):
            ws.clean(con=capture().out)
        self.assertEqual(important.read_text(), "important")

    def test_mutations_refuse_nonempty_unowned_directory(self):
        home = ws.home_dir()
        home.mkdir()
        (home / "keep").write_text("keep")
        for operation in (ensure_workspace,):
            with self.assertRaisesRegex(KvctxError, "not a kvctx workspace"):
                operation(home)
        self.assertFalse((home / MARKER).exists())

    def test_empty_directory_gets_marker_and_can_be_cleaned(self):
        home = ws.home_dir()
        ensure_workspace(home)
        self.assertTrue((home / MARKER).is_file())
        ws.clean(con=capture().out)
        self.assertFalse(home.exists())

    def test_inventory_without_ownership_marker_is_not_adopted(self):
        home = ws.home_dir()
        inventory = home / "sources" / "repositories.tsv"
        inventory.parent.mkdir(parents=True)
        content = "organization\tkubevirt\ndiscovered_at_utc\t2026-10-26T19:04:12Z\nname\tclone_url\tarchived\tfork\n"
        inventory.write_text(content)
        with self.assertRaisesRegex(KvctxError, "not a kvctx workspace"):
            ensure_workspace(home)
        self.assertFalse((home / MARKER).exists())
        self.assertEqual(inventory.read_text(), content)

    def test_protected_directories_and_checkouts_are_rejected(self):
        for home in (Path("/"), Path.home(), Path.home().parent):
            with self.subTest(home=home), self.assertRaises(KvctxError):
                ensure_workspace(home)
        home = ws.home_dir()
        home.mkdir()
        (home / ".git").mkdir()
        for path in (home, home / "nested" / "workspace"):
            with self.assertRaisesRegex(KvctxError, "source checkout"):
                ensure_workspace(path)

    def test_symlink_cannot_redirect_clones_outside_workspace(self):
        home = ws.home_dir()
        ensure_workspace(home)
        outside = Path.home() / "outside"
        outside.mkdir()
        (home / "sources").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(KvctxError, "symlink"):
            ensure_workspace(home)
        self.assertEqual(list(outside.iterdir()), [])

    def test_workspace_source_paths_use_visible_directories(self):
        home = ws.home_dir()
        self.assertEqual(ws.sources_dir(home), home / "sources")
        self.assertEqual(ws.clones_dir(home), home / "sources" / "kubevirt")
        self.assertEqual(ws.inventory_path(home), home / "sources" / "repositories.tsv")

    def test_staging_symlink_is_rejected(self):
        home = ws.home_dir()
        ensure_workspace(home)
        (home / "sources").mkdir()
        outside = Path.home() / "outside"
        outside.mkdir()
        (home / "sources" / "staging").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(KvctxError, "symlink"):
            ensure_workspace(home)
        self.assertEqual(list(outside.iterdir()), [])

    def test_lock_prevents_a_second_process_from_mutating_workspace(self):
        home = ws.home_dir()
        script = "from pathlib import Path; from kubevirt_context.storage import workspace_lock; " \
                 "from kubevirt_context.errors import KvctxError\n" \
                 "try:\n with workspace_lock(Path(__import__('sys').argv[1])): pass\n" \
                 "except KvctxError: raise SystemExit(17)\n"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        with workspace_lock(home):
            child = subprocess.run([sys.executable, "-c", script, str(home)], env=env,
                                   capture_output=True, check=False, timeout=5)
            self.assertEqual(child.returncode, 17)
            self.assertFalse(home.exists())
        with workspace_lock(home):
            pass  # the lock is released, not left permanently held

    def test_atomic_write_preserves_permissions_and_rejects_symlink(self):
        path = Path.home() / "config.json"
        atomic_write(path, "{}")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o640)
        atomic_write(path, '{"new": true}')
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        link = Path.home() / "link"
        link.symlink_to(path)
        with self.assertRaisesRegex(KvctxError, "symlink"):
            atomic_write(link, "overwrite")
        self.assertEqual(json.loads(path.read_text()), {"new": True})
        self.assertFalse(list(path.parent.glob(".config.json-*")))

    def test_force_install_cannot_delete_the_bundled_source(self):
        root = Path.home() / "skills"
        skill = root / "kubevirt-test"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("description: test")
        with patch.object(ws, "skills_root", return_value=root):
            with self.assertRaisesRegex(KvctxError, "same skill"):
                ws.skills_install(str(root), True, con=capture().out)
        self.assertTrue((skill / "SKILL.md").is_file())


class PrerequisiteTests(IsolatedTestCase):
    def test_clean_never_changes_external_agent_configuration(self):
        home = ws.home_dir()
        ensure_workspace(home)
        path = Path.home() / ".pi" / "agent" / "mcp.json"
        content = json.dumps({"mcpServers": {"codegraph": {"cwd": str(home), "command": "codegraph"}}})
        atomic_write(path, content)
        ws.clean(con=capture().out)
        self.assertFalse(home.exists())
        self.assertEqual(path.read_text(), content)

    def test_doctor_does_not_pass_mcp_from_local_checks(self):
        cap = capture()
        with patch.object(ws, "_prereq_report", return_value=([], ["git", "gh", "codegraph", "github auth"])):
            ws.doctor(con=cap.out)
        self.assertIn("local checks passed", cap.stdout)
        self.assertIn("MCP prerequisites", cap.stdout)
        self.assertEqual(cap.stdout.count("unverified by CLI"), 1)
        self.assertIn("CodeGraph", cap.stdout)
        self.assertIn("kubevirt-doctor", cap.stdout)

    def test_github_is_not_gated_as_an_mcp_prerequisite(self):
        cap = capture()
        with patch.object(ws, "_prereq_report", return_value=([], ["git", "gh", "codegraph", "github auth"])):
            ws.doctor(con=cap.out)
        self.assertNotIn("GitHub (read-only)", cap.stdout)
        self.assertIn("gh", cap.stdout)

    def test_both_skills_use_the_same_packaged_prerequisite_gate(self):
        root = ws.skills_root()
        doctor = root / "kubevirt-doctor"
        reference = doctor / "references" / "prerequisites.md"
        self.assertTrue(reference.is_file())
        self.assertFalse((doctor / "references" / "mcp-prerequisites.md").exists())
        setup = (root / "kubevirt-setup" / "SKILL.md").read_text()
        diagnosis = (doctor / "SKILL.md").read_text()
        self.assertIn("../kubevirt-doctor/references/prerequisites.md", setup)
        self.assertIn("references/prerequisites.md", diagnosis)
        self.assertIn("stop before cloning/indexing", " ".join(reference.read_text().split()))
        self.assertIn("Only CodeGraph is an MCP prerequisite", reference.read_text())

    def test_mcp_configuration_command_has_been_removed(self):
        cap = capture()
        with patch.object(ws.ui, "console", side_effect=cap.console):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["mcp"])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(ws.home_dir().exists())


class FailureTests(IsolatedTestCase):
    def test_codegraph_errors_are_not_silently_reported_as_missing(self):
        home = ws.home_dir()
        (home / ".codegraph").mkdir(parents=True)
        for value in ('not JSON', '{}', '{"initialized": true}',
                      '{"initialized": true, "fileCount": -1}'):
            with self.subTest(value=value), patch.object(ws, "have", return_value=True), \
                 patch.object(ws, "run", return_value=value):
                with self.assertRaises(KvctxError):
                    ws.codegraph_state(home)
        with patch.object(ws, "have", return_value=True), \
             patch.object(ws, "run", side_effect=KvctxError("database unreadable")):
            with self.assertRaisesRegex(KvctxError, "unreadable"):
                ws.codegraph_state(home)

    def test_status_keeps_mcp_prerequisites_unverified_when_index_is_broken(self):
        cap = capture()
        with patch.object(ws.ui, "console", side_effect=cap.console), \
             patch.object(ws, "codegraph_state", side_effect=KvctxError("database unreadable")):
            ws.status(con=cap.out)
        self.assertIn("database unreadable", cap.stderr)
        self.assertIn("MCP prerequisites", cap.stdout)
        self.assertNotIn("not built yet", cap.stdout)

    def test_local_prerequisites_do_not_probe_containers_or_agent_clis(self):
        with patch.object(ws.shutil, "which", return_value="/bin/tool") as which, \
             patch.object(ws, "gh_identity", return_value="test-user"), \
             patch.object(ws.subprocess, "run") as probe:
            ws.preflight(full=True, con=capture().out)
        self.assertEqual({call.args[0] for call in which.call_args_list}, {"git", "gh", "codegraph"})
        probe.assert_not_called()

    def test_credentials_timeout_has_actionable_sanitized_error(self):
        with patch.object(github_api.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 10)):
            with self.assertRaisesRegex(KvctxError, "gh auth login"):
                github_api.github_token()

    def test_api_errors_are_reported_without_dumping_the_response(self):
        client = MagicMock()
        client.__enter__.return_value = client
        with patch.object(github_api, "github_token", return_value="test-placeholder"), \
             patch.object(github_api, "Github", return_value=client):
            with self.assertRaisesRegex(KvctxError, "HTTP 403") as caught:
                with github_api.github_client():
                    raise github_api.GithubException(403, {"private_detail": "do not include API response"})
        self.assertNotIn("private_detail", str(caught.exception))
        client.__exit__.assert_called_once()

    def test_cli_rejects_invalid_clone_options_before_network_or_mutation(self):
        for argv in (["clones", "--only", ""], ["setup", "--only", "a,,b"],
                     ["sync", "--workers", "0"], ["clones", "--workers", "-2"],
                     ["skills", "list", "/tmp/ignored"], ["skills", "list", "--force"]):
            with self.subTest(argv=argv), patch.object(ws, "discover") as discover:
                cap = capture()
                with patch.object(ws.ui, "console", side_effect=cap.console):
                    with self.assertRaises(SystemExit) as caught:
                        cli.main(argv)
                self.assertEqual(caught.exception.code, 2)
                discover.assert_not_called()
                self.assertFalse(ws.home_dir().exists())

    def test_cli_restores_signal_handlers_on_success_failure_and_help(self):
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        cap = capture()
        with patch.object(ws.ui, "console", side_effect=cap.console), \
             patch.object(ws, "doctor", side_effect=KvctxError("failure")):
            self.assertEqual(cli.main(["doctor"]), 1)
            self.assertEqual(cli.main(["--help"]), 0)
            with patch("sys.stdout", io.StringIO()):
                self.assertEqual(cli.main(["path"]), 0)
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_interrupt_during_spawn_cannot_orphan_the_process(self):
        proc = MagicMock(pid=12345)

        def spawn(*args, **kwargs):
            signal.raise_signal(signal.SIGINT)
            return proc

        with ws.signal_handlers(), patch.object(ws.subprocess, "Popen", side_effect=spawn), \
             patch.object(ws.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                ws.run(["unused"], capture=True)
        kill.assert_called_once_with(12345, signal.SIGKILL)
        proc.wait.assert_called_once()
        self.assertNotIn(proc, ws._ACTIVE_PROCS)

    def test_force_install_preserves_previous_skill_if_copy_fails(self):
        source = Path.home() / "bundled" / "kubevirt-test"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("new version")
        target = Path.home() / "installed" / "kubevirt-test"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("previous version")
        with patch.object(ws, "skills_root", return_value=source.parent), \
             patch.object(ws.shutil, "copytree", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                ws.skills_install(str(target.parent), True, con=capture().out)
        self.assertEqual((target / "SKILL.md").read_text(), "previous version")
        self.assertFalse(list(target.parent.glob(".kvctx-skill-*")))

    def test_forced_color_does_not_enable_animation_or_ansi_in_a_pipe(self):
        output = io.StringIO()
        with patch.dict(os.environ, FORCE_COLOR="1"), patch("sys.stdout", output):
            console = ws.ui.console()
            self.assertFalse(console.is_terminal)
            ws.ui.done(console, "done")
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("ok: done", output.getvalue())

    def test_inventory_rejects_path_traversal_and_invalid_flags(self):
        home = ws.home_dir()
        ensure_workspace(home)
        inventory = ws.inventory_path()
        inventory.parent.mkdir()
        header = "organization\tkubevirt\ndiscovered_at_utc\t2026-10-26T19:04:12Z\nname\tclone_url\tarchived\tfork\n"
        for row in ("../escape\thttps://github.com/kubevirt/escape.git\tfalse\tfalse\n",
                    "repo\thttps://github.com/kubevirt/repo.git\tmaybe\tfalse\n"):
            inventory.write_text(header + row)
            with self.assertRaises(KvctxError):
                ws.read_inventory()


if __name__ == "__main__":
    unittest.main()
