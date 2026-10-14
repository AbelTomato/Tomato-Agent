"""Server-owned Docker execution identities and conservative termination evidence."""
import asyncio
import contextlib
import json
import os
import re
from uuid import uuid4

import aiosqlite

from app.agent.sandbox import SandboxResult


_DAEMON_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,255}$")


DOCKER_EXECUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS docker_tool_executions (
    step_id TEXT PRIMARY KEY REFERENCES execution_steps(id),
    run_id TEXT NOT NULL REFERENCES task_runs(id),
    invocation_id TEXT NOT NULL REFERENCES execution_operations(id),
    container_name TEXT NOT NULL UNIQUE,
    daemon_id TEXT NOT NULL,
    launch_closed INTEGER NOT NULL DEFAULT 0,
    termination_evidence TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'reserved',
    revocation_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_docker_tool_executions_lifecycle
    ON docker_tool_executions(lifecycle_state);
"""


async def docker_query(binary, *arguments):
    """Bounded argv-only queries with no inherited Docker endpoint overrides."""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(binary, "--host", "unix:///var/run/docker.sock", *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "")})
        async with asyncio.timeout(5):
            output = await process.stdout.read(65537)
            await process.wait()
        if process.returncode != 0 or len(output) > 65536:
            return None
        return output.decode("utf-8", errors="strict").strip()
    except (OSError, TimeoutError, UnicodeError):
        return None
    finally:
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(process.wait(), 5)


class DockerTerminationVerifier:
    def __init__(self, repository, docker_binary="docker", *, persist=True):
        self.repository, self.docker_binary = repository, docker_binary
        self.persist = persist

    async def __call__(self, run_id, invocation_id):
        async with aiosqlite.connect(self.repository.path) as db:
            rows = await db.execute_fetchall("SELECT step_id,container_name,daemon_id,launch_closed "
                "FROM docker_tool_executions WHERE run_id=? AND invocation_id=?",
                (str(run_id), str(invocation_id)))
            steps = await db.execute_fetchall("SELECT s.id FROM execution_steps s JOIN execution_operations o "
                "ON o.id=s.operation_id WHERE o.run_id=? AND o.id=?", (str(run_id), str(invocation_id)))
            operation = await (await db.execute(
                "SELECT tool_name,recovery_class FROM execution_operations WHERE id=? AND run_id=?",
                (str(invocation_id), str(run_id)))).fetchone()
        if (operation is None or operation != ("run_tests", "sandbox") or not rows
                or {r[0] for r in rows} != {s[0] for s in steps} or any(not r[3] for r in rows)):
            return None
        daemon = await docker_query(self.docker_binary, "info", "--format", "{{.ID}}")
        if not daemon or not _DAEMON_ID.fullmatch(daemon) or any(row[2] != daemon for row in rows):
            return None
        objects = []
        for step, name, _, _ in rows:
            # Successful all-container listing is authoritative absence; inspect errors are not.
            present = await docker_query(self.docker_binary, "ps", "-a", "--filter", f"name=^/{name}$",
                                         "--format", "{{.Names}}")
            if present is None or present:
                return None
            objects.append({"step_id": step, "container_name": name, "state": "absent"})
        if await docker_query(self.docker_binary, "info", "--format", "{{.ID}}") != daemon:
            return None
        evidence = {"schema_version": 1, "source": "docker_daemon", "run_id": str(run_id),
                    "invocation_id": str(invocation_id), "daemon_id": daemon, "objects": objects}
        if self.persist:
            async with aiosqlite.connect(self.repository.path) as db:
                existing = await (await db.execute(
                    "SELECT termination_evidence FROM docker_tool_executions "
                    "WHERE run_id=? AND invocation_id=? LIMIT 1",
                    (str(run_id), str(invocation_id)))).fetchone()
                if existing and existing[0]:
                    return json.loads(existing[0])
                await db.execute("UPDATE docker_tool_executions SET termination_evidence=?, "
                    "lifecycle_state='terminated', last_error=NULL "
                    "WHERE run_id=? AND invocation_id=?", (json.dumps(evidence), str(run_id), str(invocation_id)))
                await db.commit()
        return evidence


class TrackedDockerExecutor:
    def __init__(self, executor, repository, context):
        self.executor, self.repository, self.context = executor, repository, context

    async def execute(self, request, profile):
        context = self.context
        if not context.invocation_id or not context.step_id or context.lease is None:
            return SandboxResult(status="rejected", error_code="execution_identity_missing")
        daemon = await docker_query(self.executor.docker_binary, "info", "--format", "{{.ID}}")
        if not daemon:
            return SandboxResult(status="rejected", error_code="backend_unavailable")
        name = f"tomato-agent-{uuid4().hex}"
        async with aiosqlite.connect(self.repository.path) as db:
            await self.repository._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            await self.repository._validate_lease(db, context.lease, self.repository._aware_now())
            row = await (await db.execute("SELECT 1 FROM execution_steps s JOIN execution_operations o "
                "ON o.id=s.operation_id WHERE s.id=? AND o.id=? AND o.run_id=? AND s.attempt_id=? "
                "AND s.status='running' AND o.tool_name='run_tests'",
                (context.step_id, context.invocation_id, context.run_id, str(context.lease.attempt_id)))).fetchone()
            if row is None:
                return SandboxResult(status="rejected", error_code="execution_identity_missing")
            # Unique Step prevents duplicate launches, including after repository restart.
            await db.execute("INSERT INTO docker_tool_executions "
                "(step_id,run_id,invocation_id,container_name,daemon_id,lifecycle_state) "
                "VALUES (?,?,?,?,?,'running')",
                (context.step_id, context.run_id, context.invocation_id, name, daemon))
            await db.commit()
        try:
            result = await self.executor.execute(request, profile, container_name=name)
        except BaseException as exc:
            async with aiosqlite.connect(self.repository.path) as db:
                await db.execute("UPDATE docker_tool_executions SET lifecycle_state='unknown', "
                    "last_error=? WHERE step_id=? AND lifecycle_state='running'",
                    (type(exc).__name__, context.step_id))
                await db.commit()
            raise
        # Only a fully returned docker run seals launch. Timeout/cancel/crash remain conservative.
        if result.status == "completed":
            async with aiosqlite.connect(self.repository.path) as db:
                await db.execute("UPDATE docker_tool_executions SET launch_closed=1 "
                    "WHERE step_id=? AND lifecycle_state='running'", (context.step_id,))
                await db.commit()
            verifier = DockerTerminationVerifier(self.repository, self.executor.docker_binary)
            if await verifier(context.run_id, context.invocation_id) is None:
                async with aiosqlite.connect(self.repository.path) as db:
                    await db.execute("UPDATE docker_tool_executions SET lifecycle_state='unknown', "
                        "last_error='termination_unknown' WHERE step_id=?", (context.step_id,))
                    await db.commit()
                return SandboxResult(status="failed", error_code="termination_unknown")
        elif result.status in {"timed_out", "cancelled"}:
            async with aiosqlite.connect(self.repository.path) as db:
                await db.execute("UPDATE docker_tool_executions SET lifecycle_state='unknown', "
                    "last_error=? WHERE step_id=? AND lifecycle_state='running'",
                    (result.status, context.step_id))
                await db.commit()
        else:
            async with aiosqlite.connect(self.repository.path) as db:
                await db.execute("UPDATE docker_tool_executions SET lifecycle_state='unknown', "
                    "last_error=? WHERE step_id=? AND lifecycle_state='running'",
                    (result.error_code or "executor_failed", context.step_id))
                await db.commit()
            return result.model_copy(update={"error_code": "termination_unknown"})
        return result