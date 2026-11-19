"""Non-polluting Git shadow-ref checkpointing and restoration."""

from .checkpoint import (
    ShadowCheckpoint,
    push_shadow_ref,
    restore_shadow_ref,
    shadow_ref_name,
    snapshot_worktree,
)

__all__ = [
    "ShadowCheckpoint",
    "push_shadow_ref",
    "restore_shadow_ref",
    "shadow_ref_name",
    "snapshot_worktree",
]
