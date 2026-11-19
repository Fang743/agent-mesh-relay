"""End-to-end failover and split-brain fencing tests using a real bare Git repo."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from src.coordinator import LeaseStore
from src.git_sync import push_shadow_ref, restore_shadow_ref, snapshot_worktree
from src.ledger import ContextLedger

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SOURCE = REPO_ROOT / "hooks" / "pre-receive"


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=True,
    )
    return res.stdout.strip()


class TestFailoverE2E(unittest.TestCase):
    """Verify monotonic fencing, non-polluting snapshots, and stale push rejection."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="amr-e2e-"))
        self.db_path = self.tmp / "leases.db"
        self.store = LeaseStore(self.db_path)

        # Initialize coordination bare repository with the pre-receive fencing hook.
        self.hub = self.tmp / "hub.git"
        self.hub.mkdir()
        _git(self.hub, "init", "--bare", "-b", "main")
        _git(self.hub, "config", "agentmesh.leasedb", str(self.db_path))

        hook_dst = self.hub / "hooks" / "pre-receive"
        shutil.copy2(HOOK_SOURCE, hook_dst)
        os.chmod(hook_dst, 0o755)

        # Seed initial commit on main via a temporary seed clone.
        seed = self.tmp / "seed"
        _git(self.tmp, "clone", str(self.hub), str(seed))
        self._configure_identity(seed, "seed")
        (seed / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(seed, "add", "app.py")
        _git(seed, "commit", "-m", "initial commit")
        _git(seed, "push", "-u", "origin", "main")

        # Spawn two isolated worker checkouts simulating Node A and Node B.
        self.worker_a = self.tmp / "worker-a"
        self.worker_b = self.tmp / "worker-b"
        _git(self.tmp, "clone", str(self.hub), str(self.worker_a))
        _git(self.tmp, "clone", str(self.hub), str(self.worker_b))
        self._configure_identity(self.worker_a, "worker-a")
        self._configure_identity(self.worker_b, "worker-b")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _configure_identity(repo: Path, name: str) -> None:
        _git(repo, "config", "user.name", name)
        _git(repo, "config", "user.email", f"{name}@example.local")

    def test_lease_acquire_renew_and_monotonic_fencing(self) -> None:
        lease1 = self.store.acquire("task-1", "node-a", ttl_seconds=30.0, now=1000.0)
        self.assertIsNotNone(lease1)
        assert lease1 is not None
        self.assertEqual(lease1.fencing_token, 1)

        # Active lease blocks concurrent takeover by node-b.
        blocked = self.store.acquire("task-1", "node-b", ttl_seconds=30.0, now=1010.0)
        self.assertIsNone(blocked)

        # Owner can renew while active without bumping epoch.
        renewed = self.store.renew("task-1", "node-a", 1, ttl_seconds=30.0, now=1020.0)
        self.assertIsNotNone(renewed)
        assert renewed is not None
        self.assertEqual(renewed.fencing_token, 1)
        self.assertEqual(renewed.lease_expires_at, 1050.0)

        # Once expired, node-b reclaims with strictly higher epoch.
        lease2 = self.store.acquire("task-1", "node-b", ttl_seconds=30.0, now=1060.0)
        self.assertIsNotNone(lease2)
        assert lease2 is not None
        self.assertEqual(lease2.owner_node, "node-b")
        self.assertEqual(lease2.fencing_token, 2)

        # Stale node-a cannot renew with epoch 1.
        self.assertIsNone(
            self.store.renew("task-1", "node-a", 1, ttl_seconds=30.0, now=1065.0)
        )

    def test_snapshot_does_not_pollute_real_index(self) -> None:
        # Stage one file, leave another tracked file unstaged, add an untracked file.
        (self.worker_a / "staged.txt").write_text("staged\n", encoding="utf-8")
        _git(self.worker_a, "add", "staged.txt")
        (self.worker_a / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        (self.worker_a / "untracked.txt").write_text("wip\n", encoding="utf-8")

        before_status = _git(self.worker_a, "status", "--porcelain")
        before_cached = _git(self.worker_a, "diff", "--cached", "--name-only")

        ckpt = snapshot_worktree(self.worker_a, "task-clean", fencing_token=1)
        self.assertEqual(ckpt.ref, "refs/shadow/task-clean/epoch-1")

        after_status = _git(self.worker_a, "status", "--porcelain")
        after_cached = _git(self.worker_a, "diff", "--cached", "--name-only")
        self.assertEqual(before_status, after_status)
        self.assertEqual(before_cached, after_cached)

    def test_failover_stale_push_rejected_and_backup_resumes(self) -> None:
        task_id = "task-42"

        # 1. Worker A acquires epoch 1 and performs dirty + untracked edits.
        lease_a = self.store.acquire(task_id, "worker-a", ttl_seconds=30.0, now=2000.0)
        assert lease_a is not None
        self.assertEqual(lease_a.fencing_token, 1)

        (self.worker_a / "app.py").write_text("VALUE = 41\n", encoding="utf-8")
        snapshot_worktree(self.worker_a, task_id, lease_a.fencing_token)
        first_push = push_shadow_ref(
            self.worker_a,
            "origin",
            task_id,
            lease_a.fencing_token,
            extra_env={"LEASE_NOW_OVERRIDE": "2005.0"},
        )
        self.assertEqual(first_push.returncode, 0, msg=first_push.stderr)

        # Active Worker A can safely refresh its same-epoch shadow checkpoint.
        (self.worker_a / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
        notes_dir = self.worker_a / "notes"
        notes_dir.mkdir()
        (notes_dir / "wip.txt").write_text("untracked progress from A\n", encoding="utf-8")

        ckpt_a = snapshot_worktree(self.worker_a, task_id, lease_a.fencing_token)
        push_a = push_shadow_ref(
            self.worker_a,
            "origin",
            task_id,
            lease_a.fencing_token,
            extra_env={"LEASE_NOW_OVERRIDE": "2010.0"},
        )
        self.assertEqual(push_a.returncode, 0, msg=push_a.stderr)

        # Record handoff ledger for deterministic resume.
        ledger = ContextLedger(self.tmp / "ledger" / "transcript.jsonl")
        ledger.append(
            node_id="worker-a",
            fencing_token=lease_a.fencing_token,
            kind="checkpoint",
            summary="Updated VALUE to 42 and drafted notes/wip.txt",
            artifacts=("app.py", "notes/wip.txt"),
            ts=2010.0,
        )
        handoff_md = ledger.export_handoff_md(
            self.tmp / "ledger" / "ledger.md",
            task_id=task_id,
            shadow_ref=ckpt_a.ref,
        )
        self.assertIn("refs/shadow/task-42/epoch-1", handoff_md.read_text(encoding="utf-8"))

        # 2. Worker A drops offline; lease expires and Worker B takes epoch 2.
        lease_b = self.store.acquire(task_id, "worker-b", ttl_seconds=30.0, now=2040.0)
        assert lease_b is not None
        self.assertEqual(lease_b.fencing_token, 2)

        # 3. Stale Worker A wakes up and attempts another push under epoch 1 -> REJECTED.
        (self.worker_a / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
        snapshot_worktree(self.worker_a, task_id, lease_a.fencing_token)
        stale_push = push_shadow_ref(
            self.worker_a,
            "origin",
            task_id,
            lease_a.fencing_token,
            extra_env={"LEASE_NOW_OVERRIDE": "2045.0"},
        )
        self.assertNotEqual(stale_push.returncode, 0)
        self.assertIn("REJECTED: stale fencing token 1 != active 2", stale_push.stderr)

        # 4. Worker B restores Worker A's epoch 1 snapshot cleanly and advances under epoch 2.
        restore_shadow_ref(self.worker_b, "origin", task_id, fencing_token=1)
        self.assertEqual((self.worker_b / "app.py").read_text(encoding="utf-8"), "VALUE = 42\n")
        self.assertEqual(
            (self.worker_b / "notes" / "wip.txt").read_text(encoding="utf-8"),
            "untracked progress from A\n",
        )

        # Worker B appends its continuation and pushes cleanly with epoch 2.
        (self.worker_b / "app.py").write_text("VALUE = 43\n", encoding="utf-8")
        snapshot_worktree(self.worker_b, task_id, lease_b.fencing_token)
        push_b = push_shadow_ref(
            self.worker_b,
            "origin",
            task_id,
            lease_b.fencing_token,
            extra_env={"LEASE_NOW_OVERRIDE": "2050.0"},
        )
        self.assertEqual(push_b.returncode, 0, msg=push_b.stderr)


if __name__ == "__main__":
    unittest.main()
