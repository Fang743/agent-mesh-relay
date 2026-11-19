"""Lossless JSONL step ledger with compact Markdown handoff exporter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import time


@dataclass(frozen=True)
class LedgerEntry:
    """Single immutable step recorded by a worker node."""

    step: int
    ts: float
    node_id: str
    fencing_token: int
    kind: str
    summary: str
    artifacts: tuple[str, ...] = ()


class ContextLedger:
    """Append-only JSONL transcript plus compact handoff summary."""

    def __init__(self, jsonl_path: str | Path) -> None:
        self.jsonl_path = Path(jsonl_path)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    def read_entries(self) -> list[LedgerEntry]:
        """Read all lossless ledger entries in append order."""
        if not self.jsonl_path.exists():
            return []
        entries: list[LedgerEntry] = []
        with self.jsonl_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                obj = json.loads(line)
                entries.append(
                    LedgerEntry(
                        step=int(obj["step"]),
                        ts=float(obj["ts"]),
                        node_id=str(obj["node_id"]),
                        fencing_token=int(obj["fencing_token"]),
                        kind=str(obj["kind"]),
                        summary=str(obj["summary"]),
                        artifacts=tuple(obj.get("artifacts", ())),
                    )
                )
        return entries

    def append(
        self,
        node_id: str,
        fencing_token: int,
        kind: str,
        summary: str,
        artifacts: tuple[str, ...] = (),
        ts: float | None = None,
    ) -> LedgerEntry:
        """Append a new step entry atomically as a single JSON line."""
        existing = self.read_entries()
        next_step = (existing[-1].step + 1) if existing else 1
        entry = LedgerEntry(
            step=next_step,
            ts=time.time() if ts is None else float(ts),
            node_id=node_id,
            fencing_token=fencing_token,
            kind=kind,
            summary=summary,
            artifacts=tuple(artifacts),
        )
        payload = asdict(entry)
        payload["artifacts"] = list(entry.artifacts)
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return entry

    def export_handoff_md(
        self,
        output_path: str | Path,
        task_id: str,
        shadow_ref: str,
        recent_limit: int = 8,
    ) -> Path:
        """Generate a compact Markdown handoff file referencing exact JSONL offsets."""
        entries = self.read_entries()
        tail = entries[-recent_limit:] if recent_limit > 0 else entries
        latest_step = entries[-1].step if entries else 0
        latest_token = entries[-1].fencing_token if entries else 0

        lines = [
            f"# Handoff Ledger — `{task_id}`",
            "",
            f"- **Latest JSONL Step**: `{latest_step}`",
            f"- **Last Recorded Fencing Token**: `{latest_token}`",
            f"- **Shadow Ref**: `{shadow_ref}`",
            f"- **Lossless Source**: `{self.jsonl_path.name}`",
            "",
            "## Recent Steps",
        ]
        if not tail:
            lines.append("- (no steps recorded yet)")
        else:
            for item in tail:
                art = f" | artifacts: {', '.join(item.artifacts)}" if item.artifacts else ""
                lines.append(
                    f"- `step#{item.step}` [epoch `{item.fencing_token}` @ `{item.node_id}`] "
                    f"({item.kind}): {item.summary}{art}"
                )

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out
