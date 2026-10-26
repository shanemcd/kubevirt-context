"""Workspace lifecycle for kubevirt-context.

Every byte of state lives under one data directory — ``KUBEVIRT_CONTEXT_HOME``,
default ``~/.local/share/kubevirt-context`` — so the tool works from any working
directory and never writes into the user's own checkouts. The agent skills in
this repository are documentation only; this module is the implementation they
describe.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Column
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from . import ui
from .errors import CloneErrors, KvctxError
from .github_api import github_client
from .skill_storage import owned_skills, skills_destination, write_skill_manifest
from .storage import atomic_write as _atomic_write, ensure_workspace, is_workspace, validate_home

ORGANIZATION = "kubevirt"
_NAME_RE = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9_.-]*$")
_URL_PREFIX = f"https://github.com/{ORGANIZATION}/"


@dataclasses.dataclass(frozen=True)
class Repo:
    """A discovered GitHub repository."""

    name: str
    url: str
    archived: bool
    fork: bool


def _refresh(con: Console) -> None:
    """Flush our own buffered output before a child writes to the same fd.

    Without this, piped `kvctx status` shows the child's output above its own
    heading, because Python buffers prints while the subprocess writes straight
    to the file descriptor.
    """
    try:
        con.file.flush()
    except (AttributeError, ValueError):
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, ValueError):
            pass


# --- interruption ------------------------------------------------------------
# Ctrl+C must stop the whole clone: the in-flight git processes, the pending
# ones, and the partial-* staging directories.

_ACTIVE_PROCS: set[subprocess.Popen] = set()
_PROCS_LOCK = threading.Lock()
_STOPPING = threading.Event()
_DEFER_INTERRUPTS = False
_PENDING_INTERRUPT = False


@contextmanager
def signal_handlers():
    """Handle SIGTERM like Ctrl+C, then restore the caller's handlers."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(signum: int, frame: object) -> None:
        global _PENDING_INTERRUPT
        if _DEFER_INTERRUPTS:
            _PENDING_INTERRUPT = True
        else:
            raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def _defer_spawn_interrupts():
    """Defer our handler until a spawned child is registered for cleanup.

    Blocking POSIX signals instead would leak a blocked signal mask into the
    child. Worker threads already register under _PROCS_LOCK; only the main
    thread needs to defer its Python signal handler during Popen.
    """
    global _DEFER_INTERRUPTS, _PENDING_INTERRUPT
    main = threading.current_thread() is threading.main_thread()
    if main:
        _DEFER_INTERRUPTS = True
    try:
        yield
    finally:
        if main:
            _DEFER_INTERRUPTS = False
            if _PENDING_INTERRUPT:
                _PENDING_INTERRUPT = False
                raise KeyboardInterrupt


def _terminate_active() -> None:
    """Terminate every in-flight child process, then kill the stubborn ones."""
    with _PROCS_LOCK:
        procs = list(_ACTIVE_PROCS)

    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.monotonic() + 2
    for proc in procs:
        try:
            proc.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # A dead leader does not mean its children exited. Kill the group even
    # when wait() succeeded, then reap any leader that needed escalation.
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


# --- paths -------------------------------------------------------------------

def home_dir() -> Path:
    """The workspace directory. Everything this tool creates lives inside it."""
    raw = os.environ.get("KUBEVIRT_CONTEXT_HOME") or "~/.local/share/kubevirt-context"
    return Path(os.path.expanduser(raw)).resolve()


def sources_dir(home: Path | None = None) -> Path:
    return (home or home_dir()) / "sources"


def clones_dir(home: Path | None = None) -> Path:
    return sources_dir(home) / ORGANIZATION


def inventory_path(home: Path | None = None) -> Path:
    return sources_dir(home) / "repositories.tsv"


# --- subprocess helpers ------------------------------------------------------

def run(args: list[str], *, cwd: Path | None = None, capture: bool = False) -> str:
    """Run a command without a shell. Streams output; with capture=True, returns
    stdout and folds stderr into the error message. In-flight processes are
    tracked so an interrupt can stop them."""
    # Include registration in the protected region: a SIGINT between Popen
    # returning and adding the child to _ACTIVE_PROCS must not orphan it.
    proc = None
    try:
        with _defer_spawn_interrupts(), _PROCS_LOCK:
            if _STOPPING.is_set():
                raise KvctxError("operation cancelled")
            if not capture:
                _refresh(ui.console())
            try:
                proc = subprocess.Popen(
                    args, cwd=cwd, text=True, start_new_session=True,
                    stdout=subprocess.PIPE if capture else None,
                    stderr=subprocess.PIPE if capture else None,
                )
            except FileNotFoundError:
                raise KvctxError(f"missing executable: {args[0]}") from None
            _ACTIVE_PROCS.add(proc)
        out, err = proc.communicate()
    except KeyboardInterrupt:
        # Never call communicate() again after it was interrupted: its reader
        # threads deadlock. kill() + wait() is safe and reaps the process.
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise
    finally:
        if proc is not None:
            with _PROCS_LOCK:
                _ACTIVE_PROCS.discard(proc)

    if proc.returncode != 0:
        message = f"command failed ({proc.returncode}): {' '.join(args)}"
        if capture and err and err.strip():
            message += f" — {err.strip()}"
        raise KvctxError(message)

    return out if capture else ""


def have(name: str) -> bool:
    return shutil.which(name) is not None


def require(name: str) -> None:
    if not have(name):
        raise KvctxError(f"missing `{name}` executable; run `kvctx doctor` for details")


def gh_identity() -> str | None:
    """The authenticated GitHub login, or None when there is no usable auth."""
    try:
        with github_client() as client:
            return client.get_user().login
    except (KvctxError, OSError):
        return None


def gh_authenticated() -> bool:
    return gh_identity() is not None


# --- index state -------------------------------------------------------------

def codegraph_state(home: Path | None = None) -> dict | None:
    """Parse `codegraph status --json`; None only when there is no index.

    Reading the JSON instead of echoing CodeGraph's own report is what lets
    `kvctx status` and `kvctx index` speak the same language as every other
    command.
    """
    home = home or home_dir()
    if not have("codegraph") or not (home / ".codegraph").exists():
        return None
    raw = run(["codegraph", "status", "--json", str(home)], capture=True)
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        raise KvctxError("CodeGraph returned invalid JSON; check `codegraph status --json`") from None
    if not isinstance(state, dict) or not isinstance(state.get("initialized"), bool):
        raise KvctxError("CodeGraph returned an invalid status object")
    if not state["initialized"]:
        return None
    for key in ("fileCount", "nodeCount", "edgeCount", "dbSizeBytes"):
        if type(state.get(key)) is not int or state[key] < 0:
            raise KvctxError(f"CodeGraph status has a missing or invalid {key}")
    for key in ("index", "pendingChanges"):
        if state.get(key) is None:
            state[key] = {}
        if not isinstance(state[key], dict):
            raise KvctxError(f"CodeGraph status has an invalid {key}")
    for key in ("added", "modified", "removed"):
        value = state["pendingChanges"].get(key, 0)
        if type(value) is not int or value < 0:
            raise KvctxError("CodeGraph status has invalid pending change counts")
    if state.get("lastIndexed") is not None and not isinstance(state["lastIndexed"], str):
        raise KvctxError("CodeGraph status has an invalid index timestamp")
    return state


# --- discovery ---------------------------------------------------------------

def _validate_repo(repo: Repo) -> None:
    if not _NAME_RE.fullmatch(repo.name) or repo.name in (".", ".."):
        raise KvctxError("invalid repository name in inventory")
    if repo.url != f"{_URL_PREFIX}{repo.name}.git":
        raise KvctxError(f"unexpected clone URL for repository {repo.name}")


def discover() -> tuple[list[Repo], list[Repo]]:
    """Fetch every visible org repository into the TSV inventory.

    Returns (clone_eligible, skipped) where skipped are archived/forked
    repositories — recorded, never cloned.
    """
    ensure_workspace(home_dir())
    with github_client() as client:
        discovered = [
            Repo(repo.name, repo.clone_url, repo.archived, repo.fork)
            for repo in client.get_organization(ORGANIZATION).get_repos(type="all")
        ]

    repos: list[Repo] = []
    seen: set[str] = set()

    for repo in discovered:
        name = repo.name

        _validate_repo(repo)
        if name in seen:
            raise KvctxError(f"duplicate repository name in the GitHub API response: {name!r}")

        seen.add(name)
        repos.append(repo)

    if not repos:
        raise KvctxError("repository inventory is empty; the GitHub API returned no repositories")

    repos.sort(key=lambda r: r.name.lower())
    _save_inventory(repos)

    skipped = [r for r in repos if r.archived or r.fork]
    eligible = [r for r in repos if not (r.archived or r.fork)]
    return eligible, skipped


def _save_inventory(repos: list[Repo]) -> None:
    ensure_workspace(home_dir())
    path = inventory_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"organization\t{ORGANIZATION}",
        f"discovered_at_utc\t{stamp}",
        "name\tclone_url\tarchived\tfork",
        *(f"{r.name}\t{r.url}\t{str(r.archived).lower()}\t{str(r.fork).lower()}" for r in repos),
    ]
    _atomic_write(path, "\n".join(lines) + "\n")


def read_inventory() -> tuple[list[Repo], str | None]:
    """Parse the TSV inventory. Returns (repos, discovered_at_utc)."""
    path = inventory_path()

    if not path.exists():
        return [], None

    repos: list[Repo] = []
    stamp: str | None = None

    lines = path.read_text(encoding="utf-8").splitlines()
    if (len(lines) < 3 or lines[0] != f"organization\t{ORGANIZATION}"
            or not lines[1].startswith("discovered_at_utc\t")
            or lines[2] != "name\tclone_url\tarchived\tfork"):
        raise KvctxError(f"invalid inventory header: {path}")
    stamp = lines[1].split("\t", 1)[1]
    seen: set[str] = set()
    for line in lines[3:]:
        parts = line.split("\t")
        if len(parts) != 4 or any(flag not in ("true", "false") for flag in parts[2:]):
            raise KvctxError(f"invalid inventory record: {path}")
        repo = Repo(parts[0], parts[1], parts[2] == "true", parts[3] == "true")
        _validate_repo(repo)
        if repo.name in seen:
            raise KvctxError(f"duplicate repository in inventory: {repo.name}")
        seen.add(repo.name)
        repos.append(repo)
    return repos, stamp


def _subset(eligible: list[Repo], only: str | None, con: Console) -> list[Repo]:
    """Reduce the clone list to the repositories named in --only."""
    if only is None:
        return eligible

    wanted = sorted({n.strip() for n in only.split(",") if n.strip()})
    if not wanted:
        raise KvctxError("--only needs a comma-separated repository list")

    known, _ = read_inventory()
    known_names = {r.name for r in known}

    unknown = [n for n in wanted if n not in known_names]
    if unknown:
        raise KvctxError(f"not repositories in github.com/{ORGANIZATION}: {', '.join(unknown)}")

    eligible_names = {r.name for r in eligible}
    ineligible = [n for n in wanted if n not in eligible_names]
    if ineligible:
        ui.warn(con, f"archived or forked, never cloned: {', '.join(ineligible)}")

    chosen = [r for r in eligible if r.name in set(wanted)]
    if not chosen:
        raise KvctxError("--only selected no active, non-fork repositories")
    ui.note(con, f"--only selected {len(chosen)} of {len(eligible)} active repositories")
    return chosen


# --- cloning -----------------------------------------------------------------

def _remove_partial_clones(staging_root: Path) -> None:
    # Staging has its own directory. A valid GitHub repository may itself be
    # named partial-foo; never sweep names out of the repository directory.
    for stale in staging_root.glob("partial-*"):
        shutil.rmtree(stale, ignore_errors=True)


def _checkout(repo: Repo, update: bool, clones: Path) -> str:
    _validate_repo(repo)
    dest = clones / repo.name
    if dest.is_symlink() or (dest / ".git").is_symlink():
        raise KvctxError(f"refusing to use a symlinked clone: {dest}")

    if dest.exists():
        if not (dest / ".git").is_dir():
            raise KvctxError(f"refusing to overwrite a non-Git directory: {dest}")
        if update:
            # Fetch since the existing shallow boundary. Fetching --depth 1
            # again disconnects the new tip from HEAD, so even a normal update
            # can look divergent and fail --ff-only. Never reset local changes.
            before = run(["git", "rev-parse", "HEAD"], cwd=dest, capture=True).strip()
            run(["git", "pull", "--ff-only"], cwd=dest, capture=True)
            after = run(["git", "rev-parse", "HEAD"], cwd=dest, capture=True).strip()
            return "updated" if before != after else "present"
        return "present"

    # Clone into a staging directory and rename on success, so an interrupted
    # run never leaves a half-cloned repository that later looks "present".
    staging_root = clones.parent / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"partial-{repo.name}-", dir=staging_root))
    try:
        run(["git", "clone", "--depth", "1", "--no-tags", repo.url, str(staging)], capture=True)
        staging.rename(dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return "cloned"


def _clone_summary(tally: dict[str, int], *, update: bool, elapsed: float) -> str:
    """`✓ Cloned 74 repositories in 2m31s`, or `✓ 12 updated · 62 unchanged in 41s`."""
    past = "Updated" if update else "Cloned"
    changed = tally.get("cloned", 0) + tally.get("updated", 0)
    if not tally.get("present"):
        noun = "repository" if changed == 1 else "repositories"
        return f"{past} {ui.count(changed)} {noun} in {ui.duration(elapsed)}"
    parts = [f"{ui.count(changed)} {past.lower()}"] if changed else []
    parts.append(f"{ui.count(tally['present'])} unchanged")
    return f"{' · '.join(parts)} in {ui.duration(elapsed)}"


def clone_all(repos: list[Repo], *, update: bool, workers: int, con: Console | None = None) -> dict[str, int]:
    con = con or ui.console()
    require("git")

    ensure_workspace(home_dir())
    clones = clones_dir()
    clones.mkdir(parents=True, exist_ok=True)
    staging_root = clones.parent / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    _remove_partial_clones(staging_root)  # leftovers from a previous interrupted run
    workers = max(1, workers)
    total = len(repos)

    verb = "Updating" if update else "Cloning"
    if not total:
        ui.warn(con, "no repositories to clone")
        return {}

    noun = "repository" if total == 1 else "repositories"
    plurality = "worker" if workers == 1 else "workers"
    ui.step(con, f"{verb} {ui.count(total)} {noun} with {workers} {plurality}")

    started = time.monotonic()
    failures: list[Exception] = []
    tally: dict[str, int] = {}
    interactive = con.is_terminal and not con.is_dumb_terminal

    progress: Progress | None = None
    task = None
    if interactive:
        progress = Progress(
            SpinnerColumn(style="kv.step"),
            TextColumn(
                "{task.fields[repo]}", style="kv.dim",
                table_column=Column(no_wrap=True, overflow="ellipsis", min_width=22),
            ),
            BarColumn(bar_width=None, complete_style="kv.step", finished_style="kv.ok"),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=con, expand=True, transient=True,
        )
        task = progress.add_task("", total=total, repo=f"{ORGANIZATION}/...")
        progress.start()

    def checkout(repo: Repo) -> str:
        exists = (clones / repo.name).exists()
        action = "Updating" if exists and update else "Checking" if exists else "Cloning"
        label = f"{ORGANIZATION}/{repo.name}"
        if interactive:
            progress.update(task, repo=label)
        else:
            ui.info(con, f"{action} {label} ...")
        try:
            result = _checkout(repo, update, clones)
            if not interactive:
                ui.info(con, f"{action} {label} ... ok")
            return result
        except Exception as exc:
            raise KvctxError(f"{label}: {exc}") from exc
        finally:
            if interactive:
                progress.update(task, advance=1)

    try:
        pool = ThreadPoolExecutor(max_workers=workers)
        futures = {}
        try:
            for repo in repos:
                futures[pool.submit(checkout, repo)] = repo
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    failures.append(exc)
                else:
                    tally[result] = tally.get(result, 0) + 1
        except KeyboardInterrupt:
            _interrupt_clones(pool, futures, clones, con)
            raise
        else:
            pool.shutdown(wait=True)
    finally:
        if progress is not None:
            progress.stop()

    elapsed = time.monotonic() - started
    if failures:
        ui.fail(con, f"{len(failures)} of {ui.count(total)} {noun} failed after {ui.duration(elapsed)}")
        raise CloneErrors(failures)

    ui.done(con, _clone_summary(tally, update=update, elapsed=elapsed))
    return tally


def _interrupt_clones(pool: ThreadPoolExecutor, futures: dict, clones: Path, con: Console) -> None:
    """Prevent new children before terminating running process groups."""
    # A second Ctrl+C must not interrupt staging cleanup or executor shutdown.
    previous = {sig: signal.signal(sig, signal.SIG_IGN)
                for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with _PROCS_LOCK:
            _STOPPING.set()
        pool.shutdown(wait=False, cancel_futures=True)
        _terminate_active()
        # Children are dead; workers now finish their own staging-directory
        # finally blocks. Never wait for workers before stopping their children.
        pool.shutdown(wait=True)
        ui.warn(con, "Interrupted; in-flight clones stopped and partial clones removed. Re-run to continue.")
    finally:
        _STOPPING.clear()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# --- index -------------------------------------------------------------------

def index(home: Path | None = None, con: Console | None = None) -> None:
    con = con or ui.console()
    require("codegraph")
    home = home or home_dir()
    ensure_workspace(home)

    clones = clones_dir(home)
    if not clones.is_dir() or not any((d / ".git").is_dir() for d in clones.iterdir() if d.is_dir()):
        raise KvctxError("no cloned repositories found; run `kvctx clones` first")

    repositories = sum(1 for d in clones.iterdir() if (d / ".git").is_dir())
    refreshing = (home / ".codegraph").exists()
    verb = "Refreshing" if refreshing else "Building"
    noun = "repository" if repositories == 1 else "repositories"
    ui.step(con, f"{verb} the CodeGraph index over {ui.count(repositories)} {noun}")

    started = time.monotonic()
    _refresh(con)
    if refreshing:
        run(["codegraph", "sync", str(home)])
    else:
        run(["codegraph", "init", str(home)])
    elapsed = time.monotonic() - started

    state = codegraph_state(home)
    if state:
        ui.done(con,
                f"Indexed {ui.count(state['fileCount'])} files · "
                f"{ui.count(state['nodeCount'])} nodes · {ui.size(state['dbSizeBytes'])} "
                f"in {ui.duration(elapsed)}")
    else:
        raise KvctxError("CodeGraph completed without a readable index; run `codegraph status --json`")


# --- doctor ------------------------------------------------------------------

# What to run when a check fails; doctor itself only ever reads.
FIXES = {
    "git": "install git",
    "gh": "install the GitHub CLI, then run `gh auth login`",
    "codegraph": "npm install -g @colbymchenry/codegraph",
    "gh-auth": "run `gh auth login`",
}


def _prereq_report(*, full: bool, con: Console, verbose: bool = True) -> tuple[list[str], list[str]]:
    """Report the state of every prerequisite.

    Returns (missing labels, labels that passed). With verbose=False the
    per-check lines are replaced by the returned summary, which is what long
    commands want: a line of diagnostics, not a table.
    With full=False (clone/sync preflight) only git/gh/auth are required.
    With full=True (doctor, setup) codegraph is checked too. Live MCP access
    is checked by the setup/doctor skills in the parent agent, not inferred
    from local executables. Nothing is ever installed here.
    """
    system = con
    m = ui.marks(system)
    missing: list[str] = []
    passed: list[str] = []

    def record(label: str, detail: str, ok: bool, fix: str | None = None) -> None:
        (passed if ok else missing).append(label)
        if not verbose:
            return
        line = Text()
        line.append(f"{m['ok'] if ok else m['err']} ", style="kv.ok" if ok else "kv.err")
        line.append(f"{label:<20}", style="" if ok else "kv.err")
        if detail:
            line.append(detail, style="kv.dim")
        system.print(line)
        if not ok and fix:
            system.print(Text(f"{'':<22}fix: {fix}", style="kv.dim"))

    tools = ("git", "gh", "codegraph") if full else ("git", "gh")
    for tool in tools:
        path = shutil.which(tool)
        record(tool, str(path) if path else "not on PATH", bool(path), FIXES.get(tool))

    if have("gh"):
        login = gh_identity()
        record("github auth", f"{login} on github.com" if login else "not authenticated",
               bool(login), FIXES["gh-auth"])

    return missing, passed


def doctor(con: Console | None = None) -> None:
    """Read-only prerequisite check. Installs nothing, changes nothing."""
    con = con or ui.console()
    ui.info(con, "Checking prerequisites (read-only; nothing is installed)")
    con.print()
    missing, passed = _prereq_report(full=True, con=con)
    con.print()

    if missing:
        raise KvctxError(
            f"{len(missing)} missing or unavailable: {', '.join(missing)} "
            "(fixes listed above; load the kubevirt-doctor skill for detail)"
        )

    ui.done(con, f"all {len(passed)} local checks passed")
    codegraph_prerequisite_notice(con, skill="kubevirt-doctor")


def codegraph_prerequisite_notice(con: Console, *, skill: str) -> None:
    ui.block(con, "MCP prerequisites", [
        ("CodeGraph", "unverified by CLI"),
    ])
    ui.hint(con, f"the {skill} skill must verify that connection in your running agent; "
                 "GitHub history uses authenticated gh by default")


def preflight(*, full: bool, con: Console | None = None) -> None:
    con = con or ui.console()
    missing, passed = _prereq_report(full=full, con=con, verbose=False)
    if missing:
        raise KvctxError(f"missing/unavailable prerequisites: {', '.join(missing)}; run `kvctx doctor`")
    noun = "check" if len(passed) == 1 else "checks"
    ui.note(con, f"prerequisites ok ({len(passed)} {noun})")


# --- commands ----------------------------------------------------------------


def _discover_step(con: Console) -> list[Repo]:
    """Discovery is a step in every command that needs the inventory."""
    ui.step(con, f"Discovering repositories in github.com/{ORGANIZATION}")
    eligible, skipped = discover()
    ui.note(con, f"{ui.count(len(eligible))} active · {ui.count(len(skipped))} archived or forked (never cloned)")
    return eligible


def setup(only: str | None, workers: int) -> None:
    con = ui.console()
    started = time.monotonic()
    ui.info(con, f"Setting up the KubeVirt workspace at {ui.shorten_path(home_dir())}")
    preflight(full=True, con=con)
    codegraph_prerequisite_notice(con, skill="kubevirt-setup")
    eligible = _discover_step(con)
    clone_all(_subset(eligible, only, con), update=False, workers=workers, con=con)
    index(con=con)
    ui.done(con, f"Workspace data ready in {ui.duration(time.monotonic() - started)}")
    ui.hint(con, "complete the setup skill's live MCP/index checks before reporting research readiness")


def clones(only: str | None, workers: int) -> None:
    con = ui.console()
    preflight(full=False, con=con)
    eligible = _discover_step(con)
    clone_all(_subset(eligible, only, con), update=False, workers=workers, con=con)
    ui.hint(con, "next: kvctx index")


def sync(only: str | None, workers: int) -> None:
    con = ui.console()
    preflight(full=False, con=con)
    eligible = _discover_step(con)
    clone_all(_subset(eligible, only, con), update=True, workers=workers, con=con)
    index(con=con)


def status(con: Console | None = None) -> None:
    """One screen: where the workspace is, what is in it, and who can read it."""
    con = con or ui.console()
    home = home_dir()
    exists = home.exists()

    ui.block(con, "Workspace", [
        ("path", str(home)),
    ])
    if not exists:
        ui.warn(con, "the workspace does not exist yet · run `kvctx setup`")
    con.print()

    # Repositories
    repos, stamp = read_inventory()
    if repos:
        clones = clones_dir(home)
        active = [r for r in repos if not (r.archived or r.fork)]
        cloned = [r for r in active if (clones / r.name / ".git").is_dir()]
        missing = [r.name for r in active if not (clones / r.name / ".git").is_dir()]
        rows = [
            ("active", f"{ui.count(len(cloned))} of {ui.count(len(active))} cloned"),
            ("skipped", f"{ui.count(len(repos) - len(active))} archived or forked"),
            ("inventory", f"{ui.local_time(stamp)} ({ui.ago(stamp)})"),
        ]
        ui.block(con, "Repositories", rows)
        if missing:
            shown = ", ".join(missing[:5]) + (f" +{len(missing) - 5} more" if len(missing) > 5 else "")
            noun = "repository" if len(missing) == 1 else "repositories"
            ui.warn(con, f"{len(missing)} active {noun} not cloned: {shown}")
    else:
        ui.block(con, "Repositories", [("inventory", "none yet · run `kvctx clones`")])
    con.print()

    # Keep reporting repositories and MCP config if the index itself is broken.
    index_error = None
    try:
        state = codegraph_state(home)
    except KvctxError as exc:
        state = None
        index_error = str(exc)
    if state:
        rows = [
            ("index", f"{ui.count(state['fileCount'])} files · {ui.count(state['nodeCount'])} nodes · "
                      f"{ui.count(state['edgeCount'])} edges · {ui.size(state['dbSizeBytes'])}"),
            ("built", f"{ui.local_time(state.get('lastIndexed'))} ({ui.ago(state.get('lastIndexed'))}) · "
                      f"codegraph {state.get('version', '?')} · {state.get('index', {}).get('state', '?')}"),
        ]
        ui.block(con, "CodeGraph", rows)
        pending = state.get("pendingChanges") or {}
        dirty = sum(int(pending.get(key) or 0) for key in ("added", "modified", "removed"))
        if dirty:
            ui.warn(con, f"{ui.count(dirty)} files changed since the last index · run `kvctx index`")
        if (state.get("index") or {}).get("reindexRecommended"):
            ui.warn(con, "this index was built by an older codegraph · run `kvctx index`")
        if state.get("worktreeMismatch"):
            ui.warn(con, f"index was built from a different worktree: {state['worktreeMismatch']}")
    elif index_error:
        ui.block(con, "CodeGraph", [("index", "unavailable (not the same as an absent index)")])
        ui.warn(con, index_error)
    elif not have("codegraph"):
        ui.block(con, "CodeGraph", [("index", "codegraph is not on PATH · run `kvctx doctor`")])
    else:
        ui.block(con, "CodeGraph", [("index", "not built yet · run `kvctx index`")])
    con.print()

    codegraph_prerequisite_notice(con, skill="kubevirt-doctor")

    if not exists or not repos:
        con.print()
        ui.hint(con, "next: kvctx setup")
    elif not state:
        con.print()
        ui.hint(con, "next: kvctx index" if not index_error else "inspect the CodeGraph error above")


def _tree_size(path: Path) -> int:
    """Bytes under a directory, for the "freed" line in clean."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def clean(con: Console | None = None) -> None:
    con = con or ui.console()
    home = home_dir()

    validate_home(home)
    if home.exists() and not is_workspace(home):
        raise KvctxError(f"refusing to delete a directory not owned by kvctx: {home}")

    if not home.exists():
        ui.done(con, f"nothing to remove · {ui.shorten_path(home)} does not exist")
        return

    freed = _tree_size(home)
    ui.step(con, f"Deleting {ui.shorten_path(home)} ({ui.size(freed)})")
    shutil.rmtree(home)
    ui.done(con, f"removed the workspace · {ui.size(freed)} freed")
    ui.hint(con, "kvctx setup builds a new workspace")


# --- bundled skills ----------------------------------------------------------

def skills_root() -> Path:
    """The bundled skills: the wheel's copy, else the repository layout."""
    packaged = Path(__file__).resolve().parent / "_skills"
    if (packaged / "kubevirt-workspace").is_dir():
        return packaged

    checkout = Path(__file__).resolve().parents[2] / "skills"
    if (checkout / "kubevirt-workspace").is_dir():
        return checkout

    raise KvctxError("cannot locate the bundled skills; reinstall with `uv tool install`")


def _skill_description(skill_md: Path) -> str:
    for line in skill_md.read_text().splitlines():
        if line.startswith("description:"):
            return line[len("description:"):].strip()
    return ""


def skills_list(con: Console | None = None) -> None:
    con = con or ui.console()
    root = skills_root()
    installed_root = (Path.home() / ".agents" / "skills").resolve()
    m = ui.marks(con)

    skills = [d for d in sorted(root.glob("kubevirt-*")) if (d / "SKILL.md").is_file()]
    installed = [d for d in skills if (installed_root / d.name / "SKILL.md").is_file()]

    ui.block(con, "Bundled skills", [
        ("source", ui.shorten_path(root, limit=80)),
        ("installed", f"{len(installed)} of {len(skills)} in {ui.shorten_path(installed_root)}"),
    ])
    con.print()

    for directory in skills:
        line = Text()
        line.append(f"{m['ok'] if directory in installed else m['sub']} ",
                    style="kv.ok" if directory in installed else "kv.dim")
        line.append(directory.name, style="kv.step")
        con.print(line)
        ui.paragraph(con, _skill_description(directory / "SKILL.md"))
        con.print()

    ui.hint(con, "kvctx skills install [DIR] copies them anywhere (~/.agents/skills by default); --force overwrites")


def skills_install(dest: str | None, force: bool, con: Console | None = None) -> None:
    con = con or ui.console()
    root = skills_root()
    target_root = skills_destination(dest)

    directories = [d for d in sorted(root.glob("kubevirt-*")) if (d / "SKILL.md").is_file()]
    for directory in directories:
        target = target_root / directory.name
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise KvctxError(f"refusing to replace a symlink or non-directory: {target}")
        if target.resolve() == directory.resolve():
            raise KvctxError(f"source and destination are the same skill: {target}")

    target_root.mkdir(parents=True, exist_ok=True)
    installed = kept = 0
    m = ui.marks(con)
    rows: list[tuple[str, str]] = []
    ui.step(con, f"Installing {len(directories)} skills into {ui.shorten_path(target_root)}")
    for directory in directories:
        target = target_root / directory.name
        if target.exists() and not force:
            rows.append((f"{m['sub']} {directory.name}", "present, left alone (--force overwrites)"))
            kept += 1
            continue

        existed = target.exists()
        # Copy first and keep a rollback copy until replacement succeeds. A
        # failed copy or Ctrl+C must not destroy the previously installed skill.
        with tempfile.TemporaryDirectory(prefix=".kvctx-skill-", dir=target_root) as temporary:
            staged, backup = Path(temporary) / "new", Path(temporary) / "old"
            shutil.copytree(directory, staged)
            write_skill_manifest(staged, directory.name)
            try:
                if existed:
                    target.rename(backup)
                staged.rename(target)
            finally:
                if backup.exists() and not target.exists():
                    backup.rename(target)
        rows.append((f"{m['ok']} {directory.name}", "updated" if existed else "installed"))
        installed += 1

    ui.columns(con, rows, styles=("kv.step", "kv.dim"), no_wrap=(True, False))
    con.print()
    if installed:
        ui.done(con, f"{installed} skill(s) installed · {kept} left alone")
    else:
        ui.warn(con, f"nothing installed · all {kept} already present, use --force to overwrite")
    if installed:
        ui.hint(con, "reload your agent to pick up the new skills")


def skills_uninstall(dest: str | None, force: bool, con: Console | None = None) -> None:
    """Remove only manifest-owned installations; preflight modifications first."""
    con = con or ui.console()
    target_root = skills_destination(dest)
    directories = owned_skills(target_root, force)
    if not directories:
        ui.note(con, f"no kvctx-managed skills to uninstall in {ui.shorten_path(target_root)}")
        return

    ui.step(con, f"Uninstalling {len(directories)} skills from {ui.shorten_path(target_root)}")
    for directory in directories:
        shutil.rmtree(directory)
        ui.note(con, f"removed {directory.name}")
    ui.done(con, f"{len(directories)} skill(s) uninstalled")
    ui.hint(con, "reload your agent to stop using the removed skills")