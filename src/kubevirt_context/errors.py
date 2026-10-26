"""User-facing errors shared by the CLI and its service modules."""


class KvctxError(Exception):
    """An expected failure that does not need a Python traceback."""


class CloneErrors(KvctxError):
    """Collect failed workers without losing their original exceptions."""

    def __init__(self, errors: list[Exception]):
        super().__init__(f"{len(errors)} repositories failed")
        self.errors = errors
