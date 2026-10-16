#!/usr/bin/env python3
"""Single entry point for layered project verification."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
PYTHON = BACKEND / ".venv" / "bin" / "python"
MODULES = {
    "assistant": ["tests/test_assistant_*.py", "tests/test_provider_attempt_recorder.py"],
    "execution": ["tests/test_execution_*.py", "tests/test_code_execution.py", "tests/test_code_task*.py"],
    "rag": ["tests/test_knowledge_*.py", "tests/test_embeddings.py", "tests/test_retrieval.py"],
    "writing": ["tests/test_writing_*.py", "tests/test_application_writing_flow.py"],
    "frontend": [],
}
INTEGRATION_TESTS = [
    "tests/test_application_lifecycle.py",
    "tests/test_execution_worker.py",
    "tests/test_assistant_integration.py",
    "tests/test_unified_mvp_integration.py",
    "tests/test_unified_tasks_api.py",
]


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def version() -> dict[str, object]:
    def git(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
        return result.stdout.strip() if result.returncode == 0 else "unknown"

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


class Runner:
    def __init__(self, level: str, timeout: int, artifact_root: Path):
        self.level = level
        self.timeout = timeout
        self.started = timestamp()
        run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.directory = artifact_root / run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.steps: list[dict[str, object]] = []
        self.skipped: list[str] = []
        self.environment = "fake"

    def run(self, name: str, command: list[str], cwd: Path, *, env=None, timeout=None) -> bool:
        log_path = self.directory / f"{len(self.steps) + 1:02d}-{name}.log"
        start = timestamp()
        status = "passed"
        exit_code = None
        try:
            with log_path.open("w", encoding="utf-8") as log:
                result = subprocess.run(command, cwd=cwd, env=env, stdout=log,
                                        stderr=subprocess.STDOUT, timeout=timeout or self.timeout)
            exit_code = result.returncode
            if exit_code:
                status = "failed"
        except subprocess.TimeoutExpired:
            status = "timed_out"
        except OSError as exc:
            status = "failed"
            log_path.write_text(str(exc), encoding="utf-8")
        self.steps.append({"name": name, "command": command, "cwd": str(cwd), "started_at": start,
                           "status": status, "exit_code": exit_code, "log": str(log_path)})
        print(f"[{status}] {name}: {log_path}")
        return status == "passed"

    def finish(self) -> int:
        failed = any(step["status"] != "passed" for step in self.steps)
        result_path = self.directory / "result.json"
        result = {
            "schema_version": 1,
            "level": self.level,
            "code_version": version(),
            "started_at": self.started,
            "finished_at": timestamp(),
            "status": "failed" if failed else "passed",
            "environment": self.environment,
            "steps": self.steps,
            "skipped": self.skipped,
            "artifacts": {"directory": str(self.directory), "result": str(result_path)},
        }
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Result: {result_path} ({result['status']})")
        return 1 if failed else 0


def pytest_command(paths: list[str]) -> list[str]:
    return [str(require_python()), "-m", "pytest", "-q", *paths]


def require_python() -> Path:
    return PYTHON


def execute(args: argparse.Namespace) -> int:
    runner = Runner(args.level, args.timeout, Path(args.artifact_root).resolve())
    ok = True
    if args.level == "quick":
        selected = bool(args.static or args.backend_test or args.frontend_test)
        if not selected:
            print("select at least one check: --static, --backend-test, --frontend-test", file=sys.stderr)
            return 2
        if args.static:
            ok &= runner.run("backend-compile", [str(require_python()), "-m", "compileall", "-q", "app", "tests"], BACKEND)
            ok &= runner.run("frontend-build", ["pnpm", "build"], FRONTEND)
        if args.backend_test:
            ok &= runner.run("backend-target", pytest_command(args.backend_test), BACKEND)
        if args.frontend_test:
            ok &= runner.run("frontend-target", ["node", "--test", *args.frontend_test], FRONTEND)
    elif args.level == "module":
        for module in args.modules:
            if module == "frontend":
                tests = [str(Path(path).relative_to(FRONTEND)) for path in sorted(glob.glob(str(FRONTEND / "tests" / "*.test.mjs")))]
                ok &= runner.run("module-frontend-tests", ["node", "--test", *tests], FRONTEND)
                ok &= runner.run("module-frontend-build", ["pnpm", "build"], FRONTEND)
            else:
                paths = sorted({str(path.relative_to(BACKEND)) for pattern in MODULES[module]
                                for path in BACKEND.glob(pattern)})
                if paths:
                    ok &= runner.run(f"module-{module}", pytest_command(paths), BACKEND)
                else:
                    runner.skipped.append(f"module {module}: no matching test files")
    elif args.level == "integration":
        env = os.environ.copy()
        env["TMPDIR"] = str(runner.directory)
        env["PYTEST_ADDOPTS"] = (env.get("PYTEST_ADDOPTS", "") +
                                 f" --basetemp={runner.directory / 'pytest-temp'}").strip()
        ok &= runner.run("integration-backend", pytest_command(INTEGRATION_TESTS), BACKEND, env=env)
        frontend_tests = [str(path.relative_to(FRONTEND))
                          for path in sorted((FRONTEND / "tests").glob("*.test.mjs"))]
        ok &= runner.run("integration-frontend", ["node", "--test", *frontend_tests], FRONTEND, env=env)
        ok &= runner.run("integration-ui-api", pytest_command(["tests/test_assistant_api.py"]), BACKEND, env=env)
    elif args.level == "external":
        if args.external_timeout < 1 or args.external_timeout > 600:
            print("external timeout must be between 1 and 600 seconds", file=sys.stderr)
            return 2
        runner.environment = "real"
        env = os.environ.copy()
        if args.target == "docker":
            env["RUN_DOCKER_ACCEPTANCE"] = "1"
            command = pytest_command(["tests/test_sandbox_docker_acceptance.py"])
        elif args.target == "openshell":
            env["RUN_OPENSHELL_ACCEPTANCE"] = "1"
            command = pytest_command(["tests/test_sandbox_openshell_acceptance.py"])
        else:
            if args.provider_max_runs is None or args.provider_max_requests is None:
                print("provider requires --provider-max-runs and --provider-max-requests", file=sys.stderr)
                return 2
            if not 1 <= args.provider_max_runs <= 4 or not 1 <= args.provider_max_requests <= 16:
                print("provider budget must be between 1-4 runs and 1-16 requests", file=sys.stderr)
                return 2
            command = [str(require_python()), "tests/fixtures/runtime_provider_acceptance.py", "--run-real-provider"]
            command.extend(["--max-runs", str(args.provider_max_runs), "--max-requests", str(args.provider_max_requests)])
        ok &= runner.run(f"external-{args.target}", command, BACKEND, env=env, timeout=args.external_timeout)
    code = runner.finish()
    return code if ok else max(1, code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="level", required=True)
    quick = subparsers.add_parser("quick")
    quick.add_argument("--static", action="store_true")
    quick.add_argument("--backend-test", action="append", default=[])
    quick.add_argument("--frontend-test", action="append", default=[])
    module = subparsers.add_parser("module")
    module.add_argument("modules", nargs="+", choices=sorted(MODULES))
    subparsers.add_parser("integration")
    external = subparsers.add_parser("external")
    external.add_argument("target", choices=["provider", "docker", "openshell"])
    external.add_argument("--confirm-real-dependencies", action="store_true", required=True)
    external.add_argument("--external-timeout", type=int, default=600)
    external.add_argument("--provider-max-runs", type=int)
    external.add_argument("--provider-max-requests", type=int)
    for command in subparsers.choices.values():
        command.add_argument("--timeout", type=int, default=300)
        command.add_argument("--artifact-root", default=str(ROOT / "verification-artifacts"))
    return parser


if __name__ == "__main__":
    sys.exit(execute(build_parser().parse_args()))