"""Offline integration tests using disposable Git fixtures, never research clones."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kubevirt_context import workspace as ws
from tests.support import IsolatedTestCase, capture


@unittest.skipUnless(shutil.which("git"), "Git is required for offline integration tests")
class GitLifecycleTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.output = capture()
        patcher = patch.object(ws.ui, "console", side_effect=self.output.console)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fixture = Path.home() / "fixture"
        self.fixture.mkdir()
        self.remote = self.fixture / "remotes" / "kubevirt.git"
        self.remote.parent.mkdir()
        self.work = self.fixture / "author"
        self.work.mkdir()
        global_config = Path.home() / "gitconfig"
        global_config.write_text(
            f'[url "{self.remote.parent.as_uri()}/"]\n'
            '    insteadOf = https://github.com/kubevirt/\n'
        )
        patcher = patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(global_config),
                             GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git("init", "--bare", "--initial-branch=main", str(self.remote))
        self.git("init", "--initial-branch=main", str(self.work))
        self.git("-C", str(self.work), "config", "user.name", "Test Author")
        self.git("-C", str(self.work), "config", "user.email", "test@example.invalid")
        self.git("-C", str(self.work), "remote", "add", "origin", str(self.remote))
        self.commit("first")
        self.repo = ws.Repo("kubevirt", "https://github.com/kubevirt/kubevirt.git", False, False)

    def git(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()

    def commit(self, content):
        (self.work / "example.txt").write_text(content)
        self.git("-C", str(self.work), "add", "example.txt")
        self.git("-C", str(self.work), "commit", "-m", content)
        self.git("-C", str(self.work), "push", "origin", "main")

    def test_clone_confirm_update_and_unchanged_use_real_git(self):
        con = capture().out
        tally = ws.clone_all([self.repo], update=False, workers=1, con=con)
        self.assertEqual(tally, {"cloned": 1})
        clone = ws.clones_dir() / "kubevirt"
        self.assertEqual((clone / "example.txt").read_text(), "first")
        self.assertEqual(self.git("-C", str(clone), "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(ws.clone_all([self.repo], update=False, workers=1, con=con), {"present": 1})
        self.assertEqual(ws.clone_all([self.repo], update=True, workers=1, con=con), {"present": 1})
        self.commit("second")
        self.assertEqual(ws.clone_all([self.repo], update=True, workers=1, con=con), {"updated": 1})
        self.assertEqual((clone / "example.txt").read_text(), "second")
        self.assertFalse(list((ws.sources_dir() / "staging").iterdir()))

    def test_local_divergence_is_not_reset_or_overwritten(self):
        con = capture().out
        ws.clone_all([self.repo], update=False, workers=1, con=con)
        clone = ws.clones_dir() / "kubevirt"
        (clone / "example.txt").write_text("local work")
        self.git("-C", str(clone), "add", "example.txt")
        self.git("-C", str(clone), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-m", "local work")
        before = self.git("-C", str(clone), "rev-parse", "HEAD")
        self.commit("upstream work")
        with self.assertRaises(ws.CloneErrors):
            ws.clone_all([self.repo], update=True, workers=1, con=con)
        self.assertEqual(self.git("-C", str(clone), "rev-parse", "HEAD"), before)
        self.assertEqual((clone / "example.txt").read_text(), "local work")

    def test_valid_partial_named_repository_is_never_swept(self):
        con = capture().out
        ws.clone_all([self.repo], update=False, workers=1, con=con)
        other = ws.clones_dir() / "partial-legitimate-repo"
        other.mkdir()
        (other / "keep").write_text("keep")
        ws.clone_all([self.repo], update=False, workers=1, con=con)
        self.assertEqual((other / "keep").read_text(), "keep")

    def test_partial_clones_use_visible_staging_and_are_cleaned(self):
        observed = []
        original_run = ws.run

        def run(args, **kwargs):
            if args[:2] == ["git", "clone"]:
                staging = Path(args[-1])
                observed.append(staging)
                self.assertEqual(staging.parent, ws.home_dir() / "sources" / "staging")
                self.assertTrue(staging.name.startswith("partial-kubevirt-"))
            return original_run(args, **kwargs)

        with patch.object(ws, "run", side_effect=run):
            ws.clone_all([self.repo], update=False, workers=1, con=capture().out)
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.assertFalse((ws.home_dir() / ".sources").exists())
        self.assertFalse((ws.sources_dir() / ".staging").exists())

    def test_failed_clone_cleans_staging_and_can_be_retried(self):
        hidden = self.remote.with_name("unavailable.git")
        self.remote.rename(hidden)
        with self.assertRaises(ws.CloneErrors) as caught:
            ws.clone_all([self.repo], update=False, workers=1, con=capture().out)
        self.assertIn("kubevirt/kubevirt", str(caught.exception.errors[0]))
        self.assertFalse((ws.clones_dir() / "kubevirt").exists())
        self.assertFalse(list((ws.sources_dir() / "staging").iterdir()))
        hidden.rename(self.remote)
        self.assertEqual(ws.clone_all([self.repo], update=False, workers=1, con=capture().out), {"cloned": 1})


if __name__ == "__main__":
    unittest.main()
