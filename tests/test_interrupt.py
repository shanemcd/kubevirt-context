"""Deterministic interrupt integration tests; no network or GitHub access."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"
RUNNER = r'''
import os
from pathlib import Path
import sys
from kubevirt_context import cli, workspace as ws

root = ws.home_dir()
root.mkdir(parents=True, exist_ok=True)
child = ''' + repr('''
import os
from pathlib import Path
import subprocess
import sys
import time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(sys.argv[1]).write_text(f"{os.getpid()} {p.pid}")
time.sleep(60)
''') + r'''

def checkout(repo, update, clones):
    staging = clones / ("partial-" + repo.name)
    staging.mkdir()
    try:
        ws.run([sys.executable, "-c", child, str(root / (repo.name + ".ready"))], capture=True)
    finally:
        import shutil
        shutil.rmtree(staging)

ws._checkout = checkout
repos = [ws.Repo(f"repo{i}", "unused", False, False) for i in range(20)]
ws.setup = lambda **kwargs: ws.clone_all(repos, update=False, workers=2)
sys.exit(cli.main(["setup"]))
'''


class InterruptTests(unittest.TestCase):
    def check_interrupt(self, sig, group=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = dict(os.environ, PYTHONPATH=str(SOURCE), KUBEVIRT_CONTEXT_HOME=tmp)
            proc = subprocess.Popen([sys.executable, "-c", RUNNER], env=env,
                                    start_new_session=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            child_groups = []
            try:
                deadline = time.monotonic() + 8
                while len(list(root.glob("*.ready"))) < 2:
                    if proc.poll() is not None or time.monotonic() > deadline:
                        self.fail("test workers failed to become ready")
                    time.sleep(0.02)
                child_groups = [int(p.read_text().split()[0]) for p in root.glob("*.ready")]
                started = time.monotonic()
                if group:
                    os.killpg(proc.pid, sig)
                else:
                    proc.send_signal(sig)
                out, err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 130, (out, err))
                self.assertLess(time.monotonic() - started, 4)
                self.assertNotIn("Traceback", err)
                self.assertIn("Interrupted", err)
                self.assertEqual(len(list(root.glob("*.ready"))), 2, "queued work started")
                self.assertFalse(list(root.rglob("partial-*")))
                for pgid in child_groups:
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(pgid, 0)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                for pgid in child_groups:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.stdout.close()
                proc.stderr.close()

    def test_sigint(self):
        self.check_interrupt(signal.SIGINT)

    def test_terminal_ctrl_c(self):
        self.check_interrupt(signal.SIGINT, group=True)

    def test_sigterm(self):
        self.check_interrupt(signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
