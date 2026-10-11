"""Read completed session history as untrusted data, once per public turn."""

from uuid import UUID

from app.agent.harness_models import HarnessState
from app.agent.models import ContextState


async def build_assistant_context(session_id, message, workspace_id, repositories):
    session_id = UUID(str(session_id))
    sessions, runs = repositories["sessions"], repositories["runs"]
    if not await sessions.session_exists(session_id):
        raise KeyError("session_not_found")
    unified = []
    offset = 0
    while True:
        page = await runs.list_for_session(session_id, limit=50, offset=offset)
        unified.extend(page)
        if len(page) < 50:
            break
        offset += len(page)
    linked = {item.domain_run_id for item in unified if item.domain_run_id}
    turns = []
    for event in await sessions.list_completed_turn_events(session_id):
        if str(event.run_id) not in linked:
            turns.append((event.created_at, str(event.run_id), event.sequence,
                {"role": "user" if event.event_type == "user_message" else "assistant",
                 "content": event.payload.get("content", event.payload.get("answer", ""))}))
    references = []
    artifacts = []
    for item in unified:
        if item.session_id != session_id:
            raise ValueError("history_session_mismatch")
        if item.status != "completed":
            continue
        result = item.result or {}
        if item.domain_run_id:
            domain = await sessions.get_run(UUID(item.domain_run_id)) if item.task_type in {"chat", "knowledge"} else None
            if domain and domain.session_id != session_id:
                raise ValueError("domain_session_mismatch")
        turns.append((item.created_at, str(item.id), 0,
            {"role": "user", "content": item.request.get("message", "")}))
        turns.append((item.created_at, str(item.id), 1,
            {"role": "assistant", "content": result.get("answer", result.get("output", ""))}))
        references.extend(result.get("citations", []))
        artifacts.extend(result.get("artifacts", []))
    history = [item[3] for item in sorted(turns, key=lambda item: item[:3])]
    return HarnessState(context_state=ContextState(), structured_state={"task": message,
        "history": history, "citations": references, "artifacts": artifacts,
        "workspace_id": workspace_id})