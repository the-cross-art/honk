"""The ledger: every proposal, decision, approval, execution and check, append-only.

SQLite, so `honk` installs with nothing else to run. Every table rejects UPDATE
and DELETE with a trigger: a record of what agents did is only worth having if
it cannot be quietly edited afterwards. State that changes over time (an
action's outcome) is a new row per checkpoint, never an overwrite.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from honk.actions import CATALOG, ActionType
from honk.gate import TrackRecord
from honk.outcomes import OFFSETS, Check, Checkpoint, Result, Status, checkpoints_for, fold

TABLES: Final[tuple[str, ...]] = ("proposals", "approvals", "executions", "checks", "events")

SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    client TEXT NOT NULL,
    action TEXT NOT NULL,
    kind TEXT NOT NULL,
    namespace TEXT,
    name TEXT NOT NULL,
    target_uid TEXT,
    subject_kind TEXT,
    subject_name TEXT,
    params TEXT NOT NULL,
    reason TEXT NOT NULL,
    sig_version TEXT,
    sig_digest TEXT,
    sig_label TEXT,
    sig_fields TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('refuse', 'ask', 'auto')),
    decision_reason TEXT NOT NULL,
    decision_detail TEXT NOT NULL,
    record_shown TEXT,
    request TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
    ts TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('approved', 'denied')),
    approver TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS executions (
    proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
    ts TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('auto', 'approved')),
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    detail TEXT NOT NULL,
    generation INTEGER
);
CREATE TABLE IF NOT EXISTS checks (
    proposal_id TEXT NOT NULL REFERENCES executions(proposal_id),
    checkpoint TEXT NOT NULL CHECK (checkpoint IN ('t3m', 't1h', 't24h')),
    ts TEXT NOT NULL,
    result TEXT NOT NULL
        CHECK (result IN ('healthy', 'failing', 'unclear', 'held', 'recurred', 'missed')),
    detail TEXT NOT NULL,
    PRIMARY KEY (proposal_id, checkpoint)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('breaker_reset')),
    actor TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS proposals_cell ON proposals(sig_digest, action);
CREATE INDEX IF NOT EXISTS proposals_target ON proposals(target_uid, action);
""" + "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
""" for t in TABLES
)

BREAKER_FAILURES: Final[int] = 3
BREAKER_WINDOW: Final[timedelta] = timedelta(hours=1)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


@dataclass(frozen=True, slots=True)
class ProposalRow:
    id: str
    ts: datetime
    client: str
    action: ActionType
    kind: str
    namespace: str | None
    name: str
    target_uid: str | None
    subject_kind: str | None
    subject_name: str | None
    params: Mapping[str, Any]
    reason: str
    sig_digest: str | None
    sig_label: str | None
    mode: str
    decision_detail: str
    request: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class DueCheck:
    proposal: ProposalRow
    executed_at: datetime
    generation: int | None
    checkpoint: Checkpoint


class Ledger:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # --- writes ---------------------------------------------------------------

    def record_proposal(
        self, *, now: datetime, client: str, action: ActionType, kind: str,
        namespace: str | None, name: str, target_uid: str | None, subject_kind: str | None,
        subject_name: str | None, params: Mapping[str, Any],
        reason: str, signature: Mapping[str, Any] | None, sig_digest: str | None,
        sig_label: str | None, mode: str, decision_reason: str, decision_detail: str,
        record_shown: Mapping[str, Any] | None, request: Mapping[str, Any] | None,
    ) -> str:
        pid = uuid.uuid4().hex[:12]
        with self._tx() as c:
            c.execute(
                "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, _iso(now), client, action.value, kind, namespace, name, target_uid,
                 subject_kind, subject_name, json.dumps(dict(params), sort_keys=True), reason,
                 signature.get("version") if signature else None, sig_digest, sig_label,
                 json.dumps(dict(signature), sort_keys=True) if signature else None,
                 mode, decision_reason, decision_detail,
                 json.dumps(dict(record_shown)) if record_shown is not None else None,
                 json.dumps(dict(request)) if request is not None else None),
            )
        return pid

    def record_approval(self, pid: str, *, now: datetime, verdict: str, approver: str,
                        note: str = "") -> None:
        with self._tx() as c:
            c.execute("INSERT INTO approvals VALUES (?,?,?,?,?)",
                      (pid, _iso(now), verdict, approver, note))

    def record_execution(self, pid: str, *, now: datetime, mode: str, ok: bool, detail: str,
                         generation: int | None) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO executions VALUES (?,?,?,?,?,?)",
                      (pid, _iso(now), mode, int(ok), detail, generation))

    def record_check(self, pid: str, *, now: datetime, checkpoint: Checkpoint, result: Result,
                     detail: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO checks VALUES (?,?,?,?,?)",
                      (pid, checkpoint.value, _iso(now), result.value, detail))

    def reset_breaker(self, *, now: datetime, actor: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO events (ts, kind, actor) VALUES (?, 'breaker_reset', ?)",
                      (_iso(now), actor))

    # --- reads ----------------------------------------------------------------

    @staticmethod
    def _proposal(row: sqlite3.Row) -> ProposalRow:
        return ProposalRow(
            id=row["id"], ts=_dt(row["ts"]), client=row["client"],
            action=ActionType(row["action"]), kind=row["kind"], namespace=row["namespace"],
            name=row["name"], target_uid=row["target_uid"], subject_kind=row["subject_kind"],
            subject_name=row["subject_name"], params=json.loads(row["params"]),
            reason=row["reason"], sig_digest=row["sig_digest"], sig_label=row["sig_label"],
            mode=row["mode"], decision_detail=row["decision_detail"],
            request=json.loads(row["request"]) if row["request"] else None,
        )

    def proposal(self, pid: str) -> ProposalRow | None:
        row = self._conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        return self._proposal(row) if row else None

    def approval(self, pid: str) -> str | None:
        row = self._conn.execute(
            "SELECT verdict FROM approvals WHERE proposal_id = ?", (pid,)).fetchone()
        return str(row["verdict"]) if row else None

    def executed(self, pid: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM executions WHERE proposal_id = ?", (pid,)).fetchone() is not None

    def pending_approvals(self) -> list[ProposalRow]:
        rows = self._conn.execute(
            "SELECT p.* FROM proposals p WHERE p.mode = 'ask' AND p.request IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.proposal_id = p.id)"
            " AND NOT EXISTS (SELECT 1 FROM executions e WHERE e.proposal_id = p.id)"
            " ORDER BY p.ts").fetchall()
        return [self._proposal(r) for r in rows]

    def checks(self, pid: str) -> list[Check]:
        rows = self._conn.execute(
            "SELECT checkpoint, result, detail FROM checks WHERE proposal_id = ?", (pid,))
        return [Check(Checkpoint(r["checkpoint"]), Result(r["result"]), r["detail"])
                for r in rows]

    def status(self, pid: str) -> Status | None:
        p = self.proposal(pid)
        row = self._conn.execute(
            "SELECT ok FROM executions WHERE proposal_id = ?", (pid,)).fetchone()
        if p is None or row is None or not row["ok"]:
            return None
        return fold(self.checks(pid), verifiable=CATALOG[p.action].verifiable)

    def track_record(self, sig_digest: str, action: ActionType, *, now: datetime) -> TrackRecord:
        rows = self._conn.execute(
            "SELECT p.id, e.ts FROM proposals p JOIN executions e ON e.proposal_id = p.id"
            " WHERE p.sig_digest = ? AND p.action = ? AND e.ok = 1",
            (sig_digest, action.value)).fetchall()
        counts = dict.fromkeys(Status, 0)
        verifiable = CATALOG[action].verifiable
        for r in rows:
            counts[fold(self.checks(r["id"]), verifiable=verifiable)] += 1
        denied = self._conn.execute(
            "SELECT count(*) FROM proposals p JOIN approvals a ON a.proposal_id = p.id"
            " WHERE p.sig_digest = ? AND p.action = ? AND a.verdict = 'denied'",
            (sig_digest, action.value)).fetchone()[0]
        times = [_dt(r["ts"]) for r in rows]
        return TrackRecord(
            fixed=counts[Status.FIXED], provisional=counts[Status.PROVISIONAL],
            retracted=counts[Status.RETRACTED], failed=counts[Status.FAILED],
            unclear=counts[Status.UNCLEAR], pending=counts[Status.PENDING], denied=int(denied),
            age_days=(now - min(times)).total_seconds() / 86400 if times else 0.0,
            distinct_days=len({t.date() for t in times}),
        )

    def recent_executions(self, target_uid: str, action: ActionType, *, since: datetime) -> int:
        return int(self._conn.execute(
            "SELECT count(*) FROM proposals p JOIN executions e ON e.proposal_id = p.id"
            " WHERE p.target_uid = ? AND p.action = ? AND e.ok = 1 AND e.ts >= ?",
            (target_uid, action.value, _iso(since))).fetchone()[0])

    def breaker_open(self, *, now: datetime) -> bool:
        """Open when BREAKER_FAILURES actions failed their 3-minute check within the window."""
        reset = self._conn.execute(
            "SELECT max(ts) FROM events WHERE kind = 'breaker_reset'").fetchone()[0]
        since = max(_iso(now - BREAKER_WINDOW), reset or "")
        failures = self._conn.execute(
            "SELECT count(*) FROM checks WHERE checkpoint = 't3m' AND result = 'failing'"
            " AND ts >= ?", (since,)).fetchone()[0]
        return int(failures) >= BREAKER_FAILURES

    def due_checks(self, *, now: datetime) -> list[DueCheck]:
        rows = self._conn.execute(
            "SELECT p.*, e.ts AS executed_ts, e.generation FROM proposals p"
            " JOIN executions e ON e.proposal_id = p.id WHERE e.ok = 1").fetchall()
        due: list[DueCheck] = []
        for r in rows:
            p = self._proposal(r)
            executed_at = _dt(r["executed_ts"])
            done = {c.checkpoint for c in self.checks(p.id)}
            for cp in checkpoints_for(CATALOG[p.action].verifiable):
                if cp in done or executed_at + OFFSETS[cp] > now:
                    continue
                if cp is not Checkpoint.T3M and Checkpoint.T3M not in done:
                    continue
                due.append(DueCheck(p, executed_at, r["generation"], cp))
        return due

    def history(self, *, limit: int = 50) -> list[Mapping[str, Any]]:
        rows = self._conn.execute(
            "SELECT p.id, p.ts, p.client, p.action, p.kind, p.namespace, p.name, p.sig_label,"
            " p.mode, p.decision_detail, a.verdict, a.approver, e.ok, e.mode AS exec_mode"
            " FROM proposals p LEFT JOIN approvals a ON a.proposal_id = p.id"
            " LEFT JOIN executions e ON e.proposal_id = p.id ORDER BY p.ts DESC LIMIT ?",
            (limit,)).fetchall()
        out: list[Mapping[str, Any]] = []
        for r in rows:
            status = self.status(r["id"])
            out.append({**dict(r), "status": status.value if status else ""})
        return out

    def cells(self, *, now: datetime) -> Sequence[tuple[str, str, ActionType, TrackRecord]]:
        rows = self._conn.execute(
            "SELECT DISTINCT p.sig_digest, p.sig_label, p.action FROM proposals p"
            " WHERE p.sig_digest IS NOT NULL").fetchall()
        return [(r["sig_digest"], r["sig_label"], ActionType(r["action"]),
                 self.track_record(r["sig_digest"], ActionType(r["action"]), now=now))
                for r in rows]
