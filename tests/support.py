"""Temporary homes and terminal capture, shared by the regression tests."""

import io
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console

from kubevirt_context import ui


class IsolatedTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        patcher = patch.dict(os.environ, HOME=str(home), KUBEVIRT_CONTEXT_HOME=str(home / "workspace"))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(Path, "home", return_value=home)
        patcher.start()
        self.addCleanup(patcher.stop)


class Capture:
    """A stdout console and its stderr twin, both in the production theme."""

    def __init__(self, *, terminal=False, width=100):
        self.out_stream, self.err_stream = io.StringIO(), io.StringIO()
        options = dict(theme=ui.THEME, width=width, force_terminal=terminal,
                       color_system="truecolor" if terminal else None, markup=False)
        self.out = Console(file=self.out_stream, **options)
        self.err = Console(file=self.err_stream, **options)

    def console(self, *, stderr=False):
        return self.err if stderr else self.out

    @property
    def stdout(self):
        return self.out_stream.getvalue()

    @property
    def stderr(self):
        return self.err_stream.getvalue()

    def getvalue(self):
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", self.stdout + self.stderr)

    def __iter__(self):
        # Convenient unpacking for tests that only need one console and text.
        yield self.out
        yield self


def capture(*, terminal=False, width=100):
    return Capture(terminal=terminal, width=width)
