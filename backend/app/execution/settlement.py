"""Settlement projections never rewrite historical operation observations."""
import aiosqlite
from app.runs.models import RunRepositoryConflict
from app.execution.ports import canonical_digest


class AssistantSettlementRepository:
    async def list_effect_settlements(self, run_id):
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute("SELECT s.invocation_id,s.resolution,s.quarantined,"
                "s.termination_evidence,s.conflict,s.risk_accepted FROM assistant_effect_settlements s "
                "JOIN execution_operations o ON o.id=s.invocation_id WHERE o.run_id=?",
                (str(run_id),))).fetchall()
        return [{"invocation_id": row[0], "resolution": row[1], "quarantined": bool(row[2]),
            "termination_evidence": row[3], "conflict": bool(row[4]), "risk_accepted": bool(row[5]),
            "admission_blocked": bool(row[4] and not row[5])} for row in rows]

    async def accept_effect_conflict_risk(self, run_id, invocation_id, client_request_id=None):
        """Record explicit risk acceptance without changing conflict or termination facts."""
        now = self._aware_now()
        client_request_id = client_request_id or f"accept-conflict-risk:{invocation_id}"
        digest = canonical_digest({"invocation_id": str(invocation_id), "accept_risk": True})
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            run = await (await db.execute("SELECT session_id FROM task_runs WHERE id=?", (str(run_id),))).fetchone()
            if not run:
                raise KeyError("run_not_found")
            existing = await (await db.execute(
                "SELECT digest FROM task_actions WHERE run_id=? AND client_request_id=?",
                (str(run_id), client_request_id))).fetchone()
            if existing:
                if existing[0] != digest:
                    raise RunRepositoryConflict("action_request_conflict")
                return
            row = await (await db.execute(
                "SELECT s.resolution,s.conflict,s.risk_accepted FROM assistant_effect_settlements s "
                "JOIN execution_operations o ON o.id=s.invocation_id "
                "WHERE s.invocation_id=? AND o.run_id=?", (str(invocation_id), str(run_id)))).fetchone()
            if not row or not row[1]:
                raise RunRepositoryConflict("effect_conflict_required")
            if row[2]:
                raise RunRepositoryConflict("action_request_conflict")
            await db.execute("UPDATE assistant_effect_settlements SET risk_accepted=1 WHERE invocation_id=?",
                             (str(invocation_id),))
            await db.execute("INSERT INTO task_actions(run_id,client_request_id,digest) VALUES (?,?,?)",
                             (str(run_id), client_request_id, digest))
            await self._assistant_recovery_event(db, run_id, "tool.settled", now,
                {"resolution": row[0], "conflict": True, "risk_accepted": True,
                 "client_request_id": client_request_id, "basis": "user_accepted_conflict_risk"}, invocation_id)
            await self._commit(db)

    async def reconcile_effect_quarantine(self, run_id, invocation_id, *, termination_verifier=None):
        """Only a server-owned verifier may attest stopped/revoked write authority.

        No current built-in capability supplies this evidence. An absent verifier,
        a lease expiry, cancellation or user report must never release a lock.
        """
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT s.resolution,s.quarantined,s.conflict "
                "FROM assistant_effect_settlements s JOIN execution_operations o ON o.id=s.invocation_id "
                "WHERE s.invocation_id=? AND o.run_id=?", (str(invocation_id), str(run_id)))).fetchone()
            if not row or row[0] == "unknown" or row[2]:
                raise RunRepositoryConflict("effect_unresolved")
            if not row[1]:
                raise RunRepositoryConflict("effect_already_reconciled")
            if termination_verifier is None:
                raise RunRepositoryConflict("termination_evidence_missing")
            # Verifier is injected by trusted server integration, never selected from HTTP input.
            evidence = await termination_verifier(run_id, invocation_id)
            if not isinstance(evidence, str) or not evidence.strip():
                raise RunRepositoryConflict("termination_evidence_missing")
            await db.execute("UPDATE assistant_effect_settlements SET quarantined=0,termination_evidence=? "
                "WHERE invocation_id=?", (evidence, str(invocation_id)))
            await self._assistant_recovery_event(db, run_id, "tool.quarantine_released", now,
                {"resolution": row[0], "basis": "server_verified_termination"}, invocation_id)
            await self._commit(db)