"""Git ops tests — branch isolation, snapshot, commit, revert."""

import pytest

from src.config import settings


@pytest.fixture(autouse=True)
def point_at_tmp_repo(tmp_target_repo, monkeypatch):
    monkeypatch.setattr(settings, "project_path", tmp_target_repo)
    monkeypatch.setattr(settings, "base_branch", "main")
    monkeypatch.setattr(settings, "work_branch", "solo-agent/test")


@pytest.mark.asyncio
async def test_is_repo_true():
    from src.orchestrator import git_ops
    assert await git_ops.is_repo() is True


@pytest.mark.asyncio
async def test_ensure_work_branch_creates_and_checks_out():
    from src.orchestrator import git_ops
    r = await git_ops.ensure_work_branch()
    assert r.ok
    assert await git_ops.current_branch() == "solo-agent/test"


@pytest.mark.asyncio
async def test_snapshot_returns_sha():
    from src.orchestrator import git_ops
    sha = await git_ops.snapshot()
    assert sha is not None
    assert len(sha) == 40


@pytest.mark.asyncio
async def test_commit_and_revert_roundtrip():
    """Commit changes, then revert to the pre-commit snapshot."""
    from src.orchestrator import git_ops

    await git_ops.ensure_work_branch()
    before = await git_ops.snapshot()

    # make a change
    repo = settings.project_path
    (repo / "new_file.txt").write_text("hello")

    r, after = await git_ops.commit_all("test commit")
    assert r.ok
    assert after != before
    assert (repo / "new_file.txt").exists()

    # revert
    rv = await git_ops.revert_to(before)
    assert rv.ok
    assert not (repo / "new_file.txt").exists()  # cleaned
    assert await git_ops.current_sha() == before


@pytest.mark.asyncio
async def test_diff_stat_counts_lines():
    from src.orchestrator import git_ops
    await git_ops.ensure_work_branch()
    before = await git_ops.snapshot()
    (settings.project_path / "README.md").write_text("# target\n\nnew line here\nmore lines\n")
    await git_ops.commit_all("add lines")
    lines = await git_ops.diff_stat(before)
    assert lines >= 2


@pytest.mark.asyncio
async def test_commit_nothing_to_commit_is_ok():
    from src.orchestrator import git_ops
    await git_ops.ensure_work_branch()
    r, sha = await git_ops.commit_all("nothing")
    assert r.ok
    assert sha is not None
