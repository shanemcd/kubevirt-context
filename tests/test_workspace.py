import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kubevirt_context import cli, workspace as ws
from kubevirt_context.github_api import github_token


class DiscoveryTests(unittest.TestCase):
    def test_pygithub_discovery_filters_and_saves_inventory(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.get_organization.return_value.get_repos.return_value = [
            SimpleNamespace(name="kubevirt", clone_url="https://github.com/kubevirt/kubevirt.git", archived=False, fork=False),
            SimpleNamespace(name="old", clone_url="https://github.com/kubevirt/old.git", archived=True, fork=False),
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=tmp):
            with patch.object(ws, "github_client", return_value=client), patch.object(ws, "run") as run:
                eligible, skipped = ws.discover()
                run.assert_not_called()
                client.get_organization.assert_called_once_with("kubevirt")
                client.get_organization.return_value.get_repos.assert_called_once_with(type="all")
                self.assertEqual([r.name for r in eligible], ["kubevirt"])
                self.assertEqual([r.name for r in skipped], ["old"])
                self.assertEqual(len(ws.read_inventory()[0]), 2)

    def test_auth_validation_uses_pygithub(self):
        client = MagicMock()
        client.__enter__.return_value = client
        with patch.object(ws, "github_client", return_value=client):
            self.assertTrue(ws.gh_authenticated())
            client.get_user.assert_called_once()

    def test_unavailable_credentials_have_actionable_error(self):
        with patch("kubevirt_context.github_api.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ws.KvctxError, "gh auth login"):
                github_token()


class CloneOutputTests(unittest.TestCase):
    repo = ws.Repo("kubevirt", "https://github.com/kubevirt/kubevirt.git", False, False)

    def test_clone_captures_git_output_and_reports_ok(self):
        def clone(args, **kwargs):
            self.assertTrue(kwargs["capture"])
            (Path(args[-1]) / ".git").mkdir(parents=True)
            return "git's noisy progress"

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=tmp):
            with patch.object(ws, "run", side_effect=clone), contextlib.redirect_stdout(io.StringIO()) as out:
                ws.clone_all([self.repo], update=False, workers=1)
            output = out.getvalue()
            self.assertIn("Cloning kubevirt/kubevirt ...", output)
            self.assertIn("Cloning kubevirt/kubevirt ... ok", output)
            self.assertNotIn("noisy progress", output)
            self.assertTrue((ws.clones_dir() / "kubevirt" / ".git").exists())

    def test_clone_error_prints_traceback_including_git_error(self):
        error = ws.KvctxError("git clone failed: fatal: unable to access repository")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=tmp):
            with patch.object(ws, "preflight"), patch.object(ws, "discover", return_value=([self.repo], [])):
                with patch.object(ws, "run", side_effect=error):
                    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                        rc = cli.main(["clones"])
            self.assertEqual(rc, 1)
            self.assertIn("Traceback (most recent call last)", err.getvalue())
            self.assertIn("fatal: unable to access repository", err.getvalue())
            self.assertNotIn("... ok", out.getvalue())
            self.assertFalse(list(ws.clones_dir().glob(".partial-*")))

    def test_terminal_output_uses_progress_bar_and_summary(self):
        from rich.console import Console
        from kubevirt_context import ui

        terminal = io.StringIO()
        console = Console(file=terminal, force_terminal=True, width=100, theme=ui.THEME)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KUBEVIRT_CONTEXT_HOME=tmp):
            with patch.object(ui, "Console", return_value=console), patch.object(ws, "_checkout", return_value="cloned"):
                ws.clone_all([self.repo], update=False, workers=1)
        output = terminal.getvalue()
        self.assertIn("kubevirt/kubevirt", output)
        self.assertIn("1/1", output)
        self.assertIn("Cloned 1 repository", output)


if __name__ == "__main__":
    unittest.main()
