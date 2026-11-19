"""Ephemeral GIT_INDEX_FILE shadow-ref checkpointing and failover restore."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class ShadowCheckpoint:
    """Metadata for a locally committed shadow snapshot."""

    ref: str
    commit_sha: str
    tree_sha: str
    task_id: str
    fencing_token: int


def shadow_ref_name(task_id: str, fencing_token: int) -> str:
    """Build a canonical shadow ref name for a task and fencing epoch."""
    if not _SAFE_TASK_ID_RE.match(task_id):
        raise ValueError(f"unsafe task_id for git ref: {task_id!r}")
    if fencing_token < 1:
        raise ValueError("fencing_token must be >= 1")
    return f"refs/shadow/{task_id}/epoch-{fencing_token}"


def _run_git(
    worktree_path: str | Path,
    args: list[str],
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=str(worktree_path),
        env=merged_env,
        text=True,
        capture_output=True,
        check=check,
    )


def snapshot_worktree(
    worktree_path: str | Path,
    task_id: str,
    fencing_token: int,
    message: str | None = None,
) -> ShadowCheckpoint:
    """Capture tracked changes and untracked files without polluting git index."""
    wt = Path(worktree_path)
    ref = shadow_ref_name(task_id, fencing_token)

    real_index_raw = _run_git(wt, ["rev-parse", "--git-path", "index"]).stdout.strip()
    real_index = Path(real_index_raw)
    if not real_index.is_absolute():
        real_index = wt / real_index

    head_sha = _run_git(wt, ["rev-parse", "HEAD"]).stdout.strip()
    commit_msg = message or f"shadow checkpoint task={task_id} epoch={fencing_token}"

    with tempfile.TemporaryDirectory(prefix="amr-index-") as tmp_dir:
        tmp_index = Path(tmp_dir) / "index"
        if real_index.exists():
            shutil.copy2(real_index, tmp_index)

        ephemeral_env = {"GIT_INDEX_FILE": str(tmp_index)}
        _run_git(wt, ["add", "-A"], env=ephemeral_env)
        tree_sha = _run_git(wt, ["write-tree"], env=ephemeral_env).stdout.strip()
        commit_sha = _run_git(
            wt,
            ["commit-tree", tree_sha, "-p", head_sha, "-m", commit_msg],
            env=ephemeral_env,
        ).stdout.strip()

    _run_git(wt, ["update-ref", ref, commit_sha])
    return ShadowCheckpoint(
        ref=ref,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        task_id=task_id,
        fencing_token=fencing_token,
    )


def push_shadow_ref(
    worktree_path: str | Path,
    remote: str,
    task_id: str,
    fencing_token: int,
    extra_env: dict[str, str] | None = None,
    force: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Push a local shadow ref to the coordination bare repository.

    Shadow refs represent mutable per-epoch checkpoints; force-updating the
    epoch ref delegates authority to the server-side `pre-receive` fencing hook.
    """
    ref = shadow_ref_name(task_id, fencing_token)
    refspec = f"+{ref}:{ref}" if force else f"{ref}:{ref}"
    return _run_git(
        worktree_path,
        ["push", remote, refspec],
        env=extra_env,
        check=False,
    )


def restore_shadow_ref(
    worktree_path: str | Path,
    remote: str,
    task_id: str,
    fencing_token: int,
) -> str:
    """Fetch a shadow ref and hydrate tracked + untracked files as dirty state."""
    wt = Path(worktree_path)
    ref = shadow_ref_name(task_id, fencing_token)
    _run_git(wt, ["fetch", remote, ref])
    fetched_sha = _run_git(wt, ["rev-parse", "FETCH_HEAD"]).stdout.strip()
    _run_git(wt, ["read-tree", "--reset", "-u", "FETCH_HEAD"])
    _run_git(wt, ["reset", "--mixed", "HEAD"])
    return fetched_sha
