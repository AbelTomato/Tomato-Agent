"""Settlement projections never rewrite historical operation observations."""
import aiosqlite
import json
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

        Only structured evidence matching every persisted Docker execution may
        release a lock. A lease expiry, cancellation or user report is insufficient.
        """
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT s.resolution,s.quarantined,s.conflict,o.tool_name,o.recovery_class "
                "FROM assistant_effect_settlements s JOIN execution_operations o ON o.id=s.invocation_id "
                "WHERE s.invocation_id=? AND o.run_id=?", (str(invocation_id), str(run_id)))).fetchone()
            if not row or row[0] == "unknown" or row[2]:
                raise RunRepositoryConflict("effect_unresolved")
            if row[3] != "run_tests" or row[4] != "sandbox":
                raise RunRepositoryConflict("termination_evidence_missing")
            if not row[1]:
                raise RunRepositoryConflict("effect_already_reconciled")
            if termination_verifier is None:
                raise RunRepositoryConflict("termination_evidence_missing")
            # Verifier is injected by trusted server integration, never selected from HTTP input.
            evidence = await termination_verifier(run_id, invocation_id)
            try:
                if isinstance(evidence, str):
                    evidence = json.loads(evidence)
            except (TypeError, ValueError):
                evidence = None
            if not isinstance(evidence, dict):
                raise RunRepositoryConflict("termination_evidence_missing")
            docker_rows = await db.execute_fetchall(
                "SELECT step_id,container_name,daemon_id,termination_evidence FROM docker_tool_executions "
                "WHERE run_id=? AND invocation_id=? ORDER BY step_id", (str(run_id), str(invocation_id)))
            step_rows = await db.execute_fetchall(
                "SELECT id FROM execution_steps WHERE operation_id=? ORDER BY id", (str(invocation_id),))
            expected_objects = [{"step_id": item[0], "container_name": item[1], "state": "absent"}
                                for item in docker_rows]
            actual_objects = evidence.get("objects")
            existing_evidence_matches = True
            for item in docker_rows:
                if item[3] is None:
                    continue
                try:
                    existing_evidence_matches = existing_evidence_matches and json.loads(item[3]) == evidence
                except (TypeError, ValueError):
                    existing_evidence_matches = False
            valid = (
                evidence.get("schema_version") == 1
                and evidence.get("source") == "docker_daemon"
                and evidence.get("run_id") == str(run_id)
                and evidence.get("invocation_id") == str(invocation_id)
                and isinstance(evidence.get("daemon_id"), str)
                and bool(evidence["daemon_id"].strip())
                and bool(docker_rows)
                and {item[0] for item in docker_rows} == {item[0] for item in step_rows}
                and all(item[2] == evidence["daemon_id"] for item in docker_rows)
                and existing_evidence_matches
                and isinstance(actual_objects, list)
                and all(isinstance(item, dict) for item in actual_objects)
                and sorted(actual_objects, key=lambda item: item.get("step_id", "")) == expected_objects
            )
            if not valid:
                raise RunRepositoryConflict("termination_evidence_missing")
            serialized_evidence = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
            await db.execute("UPDATE docker_tool_executions SET termination_evidence=?, "
                "lifecycle_state='terminated',last_error=NULL WHERE run_id=? AND invocation_id=? "
                "AND daemon_id=? AND termination_evidence IS NULL",
                (serialized_evidence, str(run_id), str(invocation_id), evidence["daemon_id"]))
            persisted = await db.execute_fetchall("SELECT step_id,termination_evidence FROM docker_tool_executions "
                "WHERE run_id=? AND invocation_id=? ORDER BY step_id", (str(run_id), str(invocation_id)))
            if ({item[0] for item in persisted} != {item[0] for item in docker_rows}
                    or any(not item[1] or json.loads(item[1]) != evidence for item in persisted)):
                raise RunRepositoryConflict("termination_evidence_missing")
            await db.execute("UPDATE assistant_effect_settlements SET quarantined=0,termination_evidence=? "
                "WHERE invocation_id=? AND quarantined=1", (serialized_evidence, str(invocation_id)))
            await self._assistant_recovery_event(db, run_id, "tool.quarantine_released", now,
                {"resolution": row[0], "basis": "server_verified_termination"}, invocation_id)
            await self._commit(db)