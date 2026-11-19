# agent-mesh-relay

Distributed local multi-agent orchestrator that bypasses centralized agent aggregation platforms—preserving each agent's native execution capabilities while preventing split-brain overwrites when a local worker drops offline mid-task.

## Why This Exists

Centralized multi-agent wrappers force heterogeneous coding agents behind a single hosted proxy, which often degrades native tool use and creates account-safety risks. `agent-mesh-relay` coordinates native local agents peer-to-peer over a private mesh (e.g., Tailscale WireGuard) using three deterministic primitives:

1. **Monotonic Lease Fencing (`src/coordinator/lease_store.py`)**: SQLite WAL-backed task leases with strictly monotonic `fencing_token` epochs and TTL reclamation.
2. **Non-Polluting Git Shadow-Ref Handoff (`src/git_sync/checkpoint.py` + `hooks/pre-receive`)**: Snapshots both tracked modifications and untracked files using an ephemeral `GIT_INDEX_FILE`—never touching the developer's real staging area—and enforces fencing tokens inside a bare-repo `pre-receive` hook so a resurrected stale worker cannot overwrite newer work.
3. **Append-Only Context Ledger (`src/ledger/handoff.py`)**: Records lossless JSONL step entries alongside a compact `ledger.md` checkpoint so a backup node resumes exact state rather than guessing from a truncated summary.

## Architecture

```text
  Worker A (Node 1)                               Worker B (Node 2)
  [git worktree + local agent]                    [git worktree + local agent]
         |                                                |
         | 1. acquire(task, ttl) -> epoch=1               | 3. lease expires -> acquire -> epoch=2
         | 2. ephemeral GIT_INDEX_FILE -> shadow ref      | 4. fetch refs/shadow/<task>/epoch-1
         v                                                v
  +-------------------------------------------------------------------+
  |          Coordination Hub (Private Tailscale Mesh)                |
  |  - SQLite WAL Lease Store (task_id, owner, fencing_token, ttl)    |
  |  - Bare Git Repo + hooks/pre-receive (rejects epoch < current)    |
  |  - Append-only Context Ledger (transcript.jsonl + ledger.md)      |
  +-------------------------------------------------------------------+
```

## Quickstart & Verification

Zero third-party runtime dependencies (Python 3.10+ standard library + Git).

```bash
python3 -m unittest discover -s tests -v
```

The end-to-end failover suite (`tests/test_failover_e2e.py`) spins up a real local bare Git repository with `hooks/pre-receive` installed and verifies:
- Ephemeral `GIT_INDEX_FILE` checkpointing captures tracked + untracked files without altering `git status` or the real staging index.
- Lease expiry increments the `fencing_token` monotonically when Worker B takes over.
- A delayed push from stale Worker A (carrying the old epoch) is deterministically rejected by `hooks/pre-receive`.
- Worker B restores Worker A's shadow ref (including untracked files) and commits/pushes cleanly under the new epoch.

## Roadmap

- **M1 (`v0.1.0-alpha`, current)**: Lease fencing store (SQLite WAL), ephemeral `GIT_INDEX_FILE` shadow-ref checkpoint/restore, bare-repo `pre-receive` guard, append-only handoff ledger, and E2E failover tests.
- **M2**: Peer discovery & heartbeat daemon over Tailscale mesh + local Unix-socket / HTTP control API.
- **M3**: Native CLI agent adapter protocol (spawn, heartbeat, graceful checkpoint on `SIGTERM`/disconnect) + multi-node chaos test harness.
- **M4**: Local observability dashboard and declarative multi-agent task DAG runner.

## License

MIT
