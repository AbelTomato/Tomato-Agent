import json
from pathlib import Path

import httpx
import pytest

from app.agent.models import LLMResponse
from app.application import create_app
from app.knowledge.models import Chunk, Document
from app.settings import Settings


class RecordingWritingLLM:
    def __init__(self, *, fail_draft=False):
        self.outline_inputs = []
        self.draft_inputs = []
        self.fail_draft = fail_draft

    async def complete(self, messages, tools):
        assert tools == []
        payload = json.loads(messages[-1].content)
        evidence = payload["evidence"]
        assert len(evidence) == 1
        assert evidence[0]["chunk_id"] == "chunk-setex"
        assert evidence[0]["text"] == "SETEX key value seconds sets a value and its expiration time."
        citation_ids = [evidence[0]["citation_id"]]
        if "outline" not in payload:
            self.outline_inputs.append(payload)
            output = {
                "title": "SETEX expiration",
                "sections": [{
                    "title": "Expiration", "points": ["Explain SETEX"],
                    "citation_ids": citation_ids,
                }],
                "gaps": [],
            }
        else:
            self.draft_inputs.append(payload)
            if self.fail_draft:
                raise RuntimeError("provider unavailable")
            output = {
                "title": payload["outline"]["title"],
                "sections": [{
                    "title": "Expiration", "content": "SETEX sets a value and its expiration time.",
                    "citation_ids": citation_ids,
                }],
            }
        return LLMResponse(kind="final", content=json.dumps(output))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["success", "empty_knowledge", "draft_failure"])
async def test_default_application_writing_flow(tmp_path, monkeypatch, scenario):
    llm = RecordingWritingLLM(fail_draft=scenario == "draft_failure")
    monkeypatch.setattr("app.dependencies.create_llm_client", lambda config: llm)
    drafts = tmp_path / "drafts"
    app = create_app(Settings(
        _env_file=None,
        database_path=tmp_path / "application.db",
        docs_root=tmp_path,
        draft_directory=drafts,
        llm_api_key="test-key",
        llm_model="test-model",
        embedding_api_key="",
        writing_retrieval_mode="keyword",
        knowledge_pipeline_enabled=False,
        knowledge_query_planning_enabled=False,
        knowledge_rerank_enabled=False,
    ))
    async with app.router.lifespan_context(app):
        if scenario != "empty_knowledge":
            await app.state.knowledge_repository.replace_document(
                Document("doc-redis", "redis.md", None, "Redis", "v1"),
                [Chunk(
                    "chunk-setex", "doc-redis", "v1", "Redis > SETEX", 1, 1,
                    "SETEX key value seconds sets a value and its expiration time.", 10,
                )],
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            session = await client.post("/api/sessions", json={})
            assert session.status_code == 200
            session_id = session.json()["session_id"]
            created = await client.post(
                f"/api/sessions/{session_id}/writing-tasks", json={"topic": "SETEX expiration"},
            )
            assert created.status_code == 200
            task = created.json()
            assert task["status"] == "researching"
            assert task["session_id"] == session_id
            assert not llm.outline_inputs and not llm.draft_inputs
            url = f"/api/writing-tasks/{task['task_id']}"

            research = await client.post(f"{url}/research", json={"version": task["version"]})
            if scenario == "empty_knowledge":
                assert research.status_code == 422
                assert research.json()["detail"]["code"] == "evidence_insufficient"
                assert not llm.outline_inputs and not llm.draft_inputs
                current = await client.get(url)
                assert current.json()["status"] == "failed"
                assert not drafts.exists()
                return
            assert research.status_code == 200, research.text
            outlined = research.json()
            assert outlined["status"] == "awaiting_outline_confirmation"
            assert outlined["version"] == task["version"] + 1
            assert len(llm.outline_inputs) == 1
            assert outlined["citations"][0]["chunk_id"] == "chunk-setex"
            assert llm.outline_inputs[0]["topic"] == task["topic"]
            repeated = await client.post(f"{url}/research", json={"version": task["version"]})
            assert repeated.status_code == 200
            assert repeated.json() == outlined
            assert len(llm.outline_inputs) == 1

            premature = await client.post(f"{url}/draft", json={"version": outlined["version"]})
            assert premature.status_code == 409
            assert not llm.draft_inputs
            edited = outlined["outline"] | {"title": "User edited SETEX guide"}
            edited["sections"][0]["points"] = ["Explain expiration in seconds"]
            confirmed = await client.post(
                f"{url}/confirm-outline", json={"version": outlined["version"], "outline": edited},
            )
            assert confirmed.status_code == 200
            confirmed_task = confirmed.json()
            assert confirmed_task["status"] == "drafting"
            assert confirmed_task["version"] == outlined["version"] + 1
            premature_save = await client.post(
                f"{url}/save", json={"version": confirmed_task["version"], "idempotency_key": "flow-save"},
            )
            assert premature_save.status_code == 409
            assert not drafts.exists()

            generated = await client.post(f"{url}/draft", json={"version": confirmed_task["version"]})
            assert len(llm.draft_inputs) == 1
            assert llm.draft_inputs[0]["outline"] == edited
            assert not drafts.exists()
            if scenario == "draft_failure":
                assert generated.status_code == 502
                assert generated.json()["detail"]["code"] == "provider_failed"
                current = await client.get(url)
                assert current.json()["status"] == "failed"
                attempt = await client.get(f"{url}/draft-attempt")
                assert attempt.json()["status"] == "failed"
                assert attempt.json()["error_code"] == "provider_failed"
                denied = await client.post(
                    f"{url}/save", json={"version": current.json()["version"], "idempotency_key": "flow-save"},
                )
                assert denied.status_code == 409
                assert not drafts.exists()
                return
            assert generated.status_code == 200, generated.text
            draft_task = generated.json()
            assert draft_task["status"] == "awaiting_save_confirmation"
            assert draft_task["version"] == confirmed_task["version"] + 1
            assert "User edited SETEX guide" in draft_task["draft"]
            repeated = await client.post(f"{url}/draft", json={"version": confirmed_task["version"]})
            assert repeated.status_code == 200
            assert repeated.json() == draft_task
            assert len(llm.draft_inputs) == 1

            save_request = {"version": draft_task["version"], "idempotency_key": "flow-save"}
            saved = await client.post(f"{url}/save", json=save_request)
            assert saved.status_code == 200, saved.text
            saved_task = saved.json()
            assert saved_task["status"] == "saved"
            assert saved_task["version"] == draft_task["version"] + 1
            saved_path = Path(saved_task["saved_path"])
            assert saved_path.parent == drafts
            assert saved_path.read_text(encoding="utf-8") == draft_task["draft"]
            assert list(drafts.glob("*.md")) == [saved_path]
            repeated = await client.post(f"{url}/save", json=save_request)
            assert repeated.status_code == 200
            assert repeated.json() == saved_task
            assert list(drafts.glob("*.md")) == [saved_path]
            current = await client.get(url)
            assert current.status_code == 200
            assert current.json() == saved_task
            assert len(llm.outline_inputs) == len(llm.draft_inputs) == 1
