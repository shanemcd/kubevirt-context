"""Skill ownership and uninstall tests, confined to temporary directories."""

import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kubevirt_context import cli, workspace as ws
from kubevirt_context.errors import KvctxError
from kubevirt_context.skill_storage import SKILL_MANIFEST
from tests.support import IsolatedTestCase, capture


class SkillsUninstallTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.source = Path.home() / "bundled"
        for name in ("kubevirt-a", "kubevirt-b"):
            directory = self.source / name
            (directory / "references").mkdir(parents=True)
            (directory / "SKILL.md").write_text("description: test\n")
            (directory / "references" / "guide.md").write_text("guidance\n")
        self.destination = Path.home() / "installed"
        patcher = patch.object(ws, "skills_root", return_value=self.source)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, force=False, dest=None):
        ws.skills_install(str(dest or self.destination), force, con=capture().out)

    def uninstall(self, force=False, dest=None):
        ws.skills_uninstall(str(dest or self.destination), force, con=capture().out)

    def test_install_records_ownership_and_file_hashes(self):
        self.install()
        directory = self.destination / "kubevirt-a"
        manifest = json.loads((directory / SKILL_MANIFEST).read_text())
        self.assertEqual(manifest["application"], "kubevirt-context")
        self.assertEqual(manifest["skill"], directory.name)
        self.assertEqual(len(manifest["entries"]["SKILL.md"]), 64)
        self.assertIsNone(manifest["entries"]["references"])
        self.assertIn("references/guide.md", manifest["entries"])
        self.assertFalse((self.source / "kubevirt-a" / SKILL_MANIFEST).exists())

    def test_uninstall_preserves_unrelated_and_unmanaged_skills(self):
        self.install()
        for name in ("other-tool", "kubevirt-user-skill"):
            directory = self.destination / name
            directory.mkdir()
            (directory / "SKILL.md").write_text("user content")
        unrelated = self.destination / "user-config.json"
        unrelated.write_text("{}")
        self.uninstall()
        self.assertEqual(sorted(p.name for p in self.destination.iterdir()),
                         ["kubevirt-user-skill", "other-tool", "user-config.json"])
        self.assertEqual(unrelated.read_text(), "{}")
        self.assertFalse(ws.home_dir().exists())

    def test_uninstall_without_install_is_idempotent_and_creates_nothing(self):
        self.uninstall()
        self.assertFalse(self.destination.exists())
        self.install()
        self.uninstall()
        self.uninstall()
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_default_destination_round_trip_through_cli(self):
        cap = capture()
        with patch.object(ws.ui, "console", side_effect=cap.console):
            self.assertEqual(cli.main(["skills", "install"]), 0)
            self.assertEqual(cli.main(["skills", "uninstall"]), 0)
        self.assertIn("2 skill(s) uninstalled", cap.stdout)
        self.assertEqual(list((Path.home() / ".agents" / "skills").iterdir()), [])

    def test_explicit_destination_and_force_through_cli(self):
        self.install()
        (self.destination / "kubevirt-a" / "SKILL.md").write_text("edited")
        cap = capture()
        with patch.object(ws.ui, "console", side_effect=cap.console):
            self.assertEqual(cli.main(["skills", "uninstall", str(self.destination)]), 1)
            self.assertEqual(cli.main(["skills", "uninstall", str(self.destination), "--force"]), 0)
        self.assertIn("locally modified", cap.getvalue())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_modified_skill_blocks_all_removal_until_forced(self):
        self.install()
        edited = self.destination / "kubevirt-b" / "SKILL.md"
        edited.write_text("edited")
        with self.assertRaisesRegex(KvctxError, "nothing removed"):
            self.uninstall()
        self.assertTrue((self.destination / "kubevirt-a" / "SKILL.md").exists())
        self.assertEqual(edited.read_text(), "edited")
        self.uninstall(force=True)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_added_files_empty_directories_and_missing_files_are_modifications(self):
        for change in ("extra-file", "empty-directory", "missing-reference"):
            with self.subTest(change=change):
                self.install(force=True)
                directory = self.destination / "kubevirt-a"
                if change == "extra-file":
                    (directory / "notes.txt").write_text("user notes")
                elif change == "empty-directory":
                    (directory / "user-directory").mkdir()
                else:
                    (directory / "references" / "guide.md").unlink()
                with self.assertRaisesRegex(KvctxError, "locally modified"):
                    self.uninstall()
                self.uninstall(force=True)

    def test_uninstall_uses_installed_manifest_not_current_bundle(self):
        self.install()
        with patch.object(ws, "skills_root", side_effect=AssertionError("do not read current bundle")):
            self.uninstall()
        self.assertTrue((self.source / "kubevirt-a" / "SKILL.md").exists())

    def test_existing_unmanaged_skill_is_not_adopted_even_when_identical(self):
        self.destination.mkdir()
        existing = self.destination / "kubevirt-a"
        existing.mkdir()
        (existing / "SKILL.md").write_text("description: test\n")
        self.install()
        self.assertFalse((existing / SKILL_MANIFEST).exists())
        self.uninstall(force=True)
        self.assertTrue((existing / "SKILL.md").exists())

    def test_force_reinstall_updates_manifest_after_bundle_change(self):
        self.install()
        (self.source / "kubevirt-a" / "SKILL.md").write_text("new release")
        self.install(force=True)
        self.uninstall()
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_manifest_write_failure_preserves_previous_installation(self):
        self.install()
        previous = (self.destination / "kubevirt-a" / SKILL_MANIFEST).read_bytes()
        with patch.object(ws, "write_skill_manifest", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.install(force=True)
        self.assertEqual((self.destination / "kubevirt-a" / SKILL_MANIFEST).read_bytes(), previous)
        self.assertFalse(list(self.destination.glob(".kvctx-skill-*")))
        self.uninstall()

    def test_invalid_manifest_is_never_used_even_with_force(self):
        for invalid in ("broken json", "[]", "{}", json.dumps({
            "application": "kubevirt-context", "version": 1,
            "skill": "../outside", "entries": {"SKILL.md": "hash"},
        })):
            with self.subTest(invalid=invalid):
                self.install(force=True)
                (self.destination / "kubevirt-b" / SKILL_MANIFEST).write_text(invalid)
                with self.assertRaisesRegex(KvctxError, "invalid skill ownership"):
                    self.uninstall(force=True)
                self.assertTrue((self.destination / "kubevirt-a").exists())

    def test_manifest_symlink_is_rejected_even_with_force(self):
        self.install()
        marker = self.destination / "kubevirt-a" / SKILL_MANIFEST
        outside = Path.home() / "outside.json"
        marker.rename(outside)
        marker.symlink_to(outside)
        with self.assertRaisesRegex(KvctxError, "invalid skill ownership"):
            self.uninstall(force=True)
        self.assertTrue(outside.exists())

    def test_skill_directory_symlink_is_never_followed(self):
        self.install()
        directory = self.destination / "kubevirt-a"
        outside = Path.home() / "outside"
        directory.rename(outside)
        directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(KvctxError, "symlink"):
            self.uninstall(force=True)
        self.assertTrue((outside / "SKILL.md").exists())

    def test_nested_symlink_requires_force_and_never_deletes_its_target(self):
        self.install()
        outside = Path.home() / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        (self.destination / "kubevirt-a" / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(KvctxError, "locally modified"):
            self.uninstall()
        self.uninstall(force=True)
        self.assertEqual((outside / "keep").read_text(), "keep")

    def test_protected_directories_are_rejected_with_force(self):
        for directory in (Path.home(), Path.home().parent, Path("/")):
            with self.subTest(directory=directory), self.assertRaisesRegex(KvctxError, "protected directory"):
                self.uninstall(force=True, dest=directory)

    def test_file_is_not_accepted_as_destination(self):
        self.destination.write_text("keep")
        with self.assertRaisesRegex(KvctxError, "not a skills directory"):
            self.uninstall(force=True)
        self.assertEqual(self.destination.read_text(), "keep")

    def test_uninstall_does_not_change_workspace_or_agent_config(self):
        self.install()
        workspace = ws.home_dir()
        workspace.mkdir()
        (workspace / "keep").write_text("workspace")
        config = Path.home() / ".agent-config.json"
        config.write_text('{"keep": true}')
        self.uninstall()
        self.assertEqual((workspace / "keep").read_text(), "workspace")
        self.assertEqual(config.read_text(), '{"keep": true}')
