import importlib.util
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("project_verify", ROOT / "scripts" / "verify.py")
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)


def test_runner_records_nonzero_exit_and_artifact_paths_without_environment(tmp_path):
    runner = verify.Runner("external", 10, tmp_path)
    secret = "must-not-appear-in-report"
    assert not runner.run(
        "external-provider",
        [sys.executable, "-c", "import os,sys; print('failure'); sys.exit(7)"],
        tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "PROVIDER_SECRET": secret},
    )

    assert runner.finish() == 1
    result_path = runner.directory / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    step = result["steps"][0]
    assert step["status"] == "failed"
    assert step["exit_code"] == 7
    assert step["log"] == str(runner.directory / "01-external-provider.log")
    assert result["artifacts"]["directory"] == str(runner.directory)
    assert secret not in result_path.read_text(encoding="utf-8")


def test_runner_timeout_is_reported_without_retry_and_keeps_log_index(tmp_path):
    runner = verify.Runner("external", 10, tmp_path)
    assert not runner.run(
        "external-provider",
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(5)"],
        tmp_path,
        timeout=0.1,
    )

    runner.finish()
    result = json.loads((runner.directory / "result.json").read_text(encoding="utf-8"))
    step = result["steps"][0]
    assert step["status"] == "timed_out"
    assert step["exit_code"] == 124
    assert step["termination_reason"] == "timeout"
    assert step["log"] == str(runner.directory / "01-external-provider.log")
    assert "started" in Path(step["log"]).read_text(encoding="utf-8")