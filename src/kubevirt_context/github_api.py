"""GitHub API access using the existing GitHub CLI login."""

from contextlib import contextmanager
import subprocess

from github import Auth, Github, GithubException
from requests import RequestException

from .errors import KvctxError


def github_token() -> str:
    """Read the CLI's stored token without exposing it in diagnostics."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise KvctxError("GitHub credentials unavailable; run `gh auth login`.") from None
    token = result.stdout.strip()
    if not token:
        raise KvctxError("GitHub credentials unavailable; run `gh auth login`.")
    return token


@contextmanager
def github_client():
    """Close connections and turn API failures into safe, actionable errors."""
    try:
        with Github(auth=Auth.Token(github_token()), per_page=100, timeout=30) as client:
            yield client
    except GithubException as exc:
        hint = "check your GitHub login and organization access"
        if exc.status in (403, 429):
            hint = "check organization access or wait for the API rate limit to reset"
        raise KvctxError(f"GitHub API request failed (HTTP {exc.status}); {hint}") from None
    except RequestException:
        raise KvctxError("GitHub API request failed; check your network connection") from None
