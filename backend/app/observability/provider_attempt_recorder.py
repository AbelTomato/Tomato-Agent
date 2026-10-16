import json
from pathlib import Path
from typing import Any


class ProviderAttemptRecorder:
    def __init__(self, path: Path, attempt: int, run_id: str | None):
        self.path = path
        self.record: dict[str, Any] = {
            "attempt": attempt,
            "run_id": run_id,
            "run_association": "execution_context" if run_id else "unknown",
            "request_payload": None,
            "raw_response_body": None,
            "http_status": None,
        }

    def observe(self, payload: dict[str, Any], response) -> None:
        self.record["request_payload"] = payload
        if response is not None:
            self.record["http_status"] = response.status_code
            self.record["raw_response_body"] = response.content.decode("utf-8", errors="replace")
            self.record["raw_response_body_encoding"] = "utf-8-replace"

    def write(self) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.record, ensure_ascii=False) + "\n")