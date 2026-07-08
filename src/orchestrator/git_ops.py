"""Git operations — isolation, snapshot, revert.

The orchestrator NEVER commits to BASE_BRANCH. All agent work happens on
WORK_BRANCH. Before each cycle we snapshot the current HEAD; if verification
fails we revert to that snapshot. Auto-merge to base is opt-in (off by default).

All git commands run via asyncio subprocesses in the target repo.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from ..config import settings

log = logging.getLogger("solo.git")


@dataclass
class GitResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


async def _git(args: list[str]) -> GitResult:
    """Run a git command in settings.project_path."""
    cmd = ["git", "-C", str(settings.project_path), *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=60)
        out = stdout_b.decode("utf-8", errors="replace").strip()
        err = stderr_b.decode("utf-8", errors="replace").strip()
        ok = proc.returncode == 0
        if not ok:
            log.warning("git %s failed (rc=%s): %s", " ".join(args), proc.returncode, err)
        return GitResult(ok=ok, stdout=out, stderr=err, returncode=proc.returncode or 1)
    except asyncio.TimeoutError:
        log.error("git %s timed out", " ".join(args))
        return GitResult(ok=False, stderr="timeout", returncode=-1)
    except FileNotFoundError:
        log.error("git binary not found")
        return GitResult(ok=False, stderr="git not found", returncode=-1)


async def is_repo() -> bool:
    r = await _git(["rev-parse", "--is-inside-work-tree"])
    return r.ok and r.stdout.strip() == "true"


async def current_sha() -> Optional[str]:
    r = await _git(["rev-parse", "HEAD"])
    return r.stdout.strip() if r.ok and r.stdout else None


async def current_branch() -> Optional[str]:
    r = await _git(["rev-parse", "--abbrev-ref", "HEAD"])
    return r.stdout.strip() if r.ok and r.stdout else None


async def ensure_work_branch() -> GitResult:
    """Check out WORK_BRANCH, creating it from BASE_BRANCH if needed.

    Idempotent: if already on the work branch, no-op. If the work branch exists,
    just check it out. Only creates a fresh branch when it doesn't exist.
    """
    # are we already on it?
    cur = await current_branch()
    if cur == settings.work_branch:
        return GitResult(ok=True)

    # does it exist?
    exists = await _git(["show-ref", "--verify", "--quiet", f"refs/heads/{settings.work_branch}"])
    if exists.ok:
        return await _git(["checkout", settings.work_branch])

    # create from base
    log.info("creating work branch '%s' from '%s'", settings.work_branch, settings.base_branch)
    return await _git(["checkout", "-b", settings.work_branch, settings.base_branch])


async def snapshot() -> Optional[str]:
    """Return the current HEAD sha (the snapshot to revert to on failure)."""
    sha = await current_sha()
    if sha:
        log.info("snapshot: %s", sha[:10])
    return sha


async def stage_all() -> GitResult:
    """Stage all changes (including untracked) in the target repo."""
    return await _git(["add", "-A"])


async def commit_all(message: str) -> tuple[GitResult, Optional[str]]:
    """Stage + commit everything. Returns (result, new_head_sha). Aborts if nothing to commit."""
    # nothing to commit?
    diff = await _git(["diff", "--cached", "--quiet"])
    staged_clean = diff.ok  # exit 0 means no staged changes
    if staged_clean:
        await stage_all()
    status = await _git(["diff", "--cached", "--quiet"])
    if status.ok:
        log.info("nothing to commit; no changes detected")
        return GitResult(ok=True, stdout="nothing to commit"), await current_sha()

    r = await _git(["commit", "-m", message, "--no-verify"])
    new_sha = await current_sha() if r.ok else None
    return r, new_sha


async def revert_to(sha: str) -> GitResult:
    """Hard-reset the work branch to the given snapshot sha.

    Used when verification fails: discard this cycle's changes entirely.
    """
    if not sha:
        return GitResult(ok=False, stderr="no snapshot sha")
    log.warning("reverting to snapshot %s", sha[:10])
    # hard reset tracked + clean untracked
    r1 = await _git(["reset", "--hard", sha])
    r2 = await _git(["clean", "-fd"])
    return GitResult(ok=r1.ok and r2.ok, stdout=r1.stdout, stderr=f"{r1.stderr}; {r2.stderr}")


async def diff_stat(from_sha: Optional[str] = None) -> int:
    """Count lines changed (added+deleted) since from_sha, or vs HEAD~1 if None.

    Used by the no-progress / diminishing-returns detector.
    """
    if from_sha:
        r = await _git(["diff", "--numstat", from_sha, "HEAD"])
    else:
        r = await _git(["diff", "--numstat", "HEAD~1", "HEAD"])
    if not r.ok or not r.stdout:
        return 0
    total = 0
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                added = int(parts[0]) if parts[0] != "-" else 0
                deleted = int(parts[1]) if parts[1] != "-" else 0
                total += added + deleted
            except ValueError:
                continue
    return total


async def merge_to_base() -> GitResult:
    """Merge WORK_BRANCH into BASE_BRANCH. Only called when auto_merge_to_base is on."""
    log.info("merging %s into %s", settings.work_branch, settings.base_branch)
    checkout = await _git(["checkout", settings.base_branch])
    if not checkout.ok:
        return checkout
    merge = await _git(["merge", "--no-ff", settings.work_branch, "-m", "solo-agent: auto-merge cycle"])
    # switch back to work branch regardless
    await _git(["checkout", settings.work_branch])
    return merge


async def workdir_clean() -> bool:
    """True if there are no uncommitted changes in the target repo."""
    r = await _git(["status", "--porcelain"])
    return r.ok and r.stdout.strip() == ""
