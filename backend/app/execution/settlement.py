"""Settlement projections never rewrite historical operation observations."""
import aiosqlite

from app.runs.models import RunRepositoryConflict


class AssistantSettlementRepository:
    async def list_effect_settlements(self, run_id):
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute("SELECT s.invocation_id,s.resolution,s.quarantined,"
                "s.termination_evidence,s.conflict FROM assistant_effect_settlements s "
                "JOIN execution_operations o ON o.id=s.invocation_id WHERE o.run_id=?",
                (str(run_id),))).fetchall()
        return [{"invocation_id": row[0], "resolution": row[1], "quarantined": bool(row[2]),
            "termination_evidence": row[3], "conflict": bool(row[4])} for row in rows]

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