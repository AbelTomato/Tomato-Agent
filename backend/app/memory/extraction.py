"""Bounded extraction of user-authored evidence into candidate memory facts."""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.interfaces import LLMClient
from app.agent.models import Message


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["preference", "habit", "background", "episodic"]
    topic_key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=1000)
    evidence_message_ids: list[str] = Field(min_length=1, max_length=8)
    confidence: float = Field(ge=0, le=1)
    self_reported: bool
    durable: bool


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    facts: list[ExtractedFact] = Field(max_length=12)


_SENSITIVE = re.compile(
    r"(?i)(api[_ -]?key|secret|password|passwd|token|私钥|密钥|密码|验证码|令牌)|"
    r"\b(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,})"
)
_TEMPORARY = re.compile(r"(?i)(这次|本次|今天|刚才|暂时|临时|for this time|just today|temporarily)")
_NON_ASSERTIVE = re.compile(
    r"(?i)(如果|假如|假设|假定|设想|引用|引述|有人说|朋友说|同事说|别人说|"
    r"他说|她说|说过|曾说|hypothetical|quoted|quote:|if someone)"
)
_DURABLE_CUES = re.compile(
    r"(?i)(记住|以后|长期|一直|通常|习惯|喜欢|偏好|更倾向|我是|我的职业|"
    r"i prefer|i usually|i always|my preference|remember that|i am a)"
)


class MemoryExtractor:
    def __init__(self, llm: LLMClient, *, timeout_seconds: float = 45.0):
        self.llm = llm
        self.timeout_seconds = timeout_seconds

    async def extract(self, messages: list[dict]) -> ExtractionResult:
        evidence = [
            {"message_id": item["message_id"], "text": item["content"]}
            for item in messages if item.get("role") == "user" and item.get("content", "").strip()
        ]
        if not evidence:
            return ExtractionResult(facts=[])
        prompt = (
            "Extract only durable personal facts explicitly stated by the user in the supplied messages. "
            "Do not infer identity, preferences, or facts from assistant text. Ignore requests limited to one task, "
            "quotes, hypotheticals, other people, secrets, credentials, and ambiguous statements. Return JSON only "
            "with shape {\"facts\":[{\"kind\":\"preference|habit|background|episodic\","
            "\"topic_key\":string,\"content\":string,\"evidence_message_ids\":[string],"
            "\"confidence\":number,\"self_reported\":boolean,\"durable\":boolean}]}. "
            "Use evidence message IDs exactly as supplied. Empty facts are valid.\nMESSAGES:\n"
            + json.dumps(evidence, ensure_ascii=False)
        )
        import asyncio

        try:
            response = await asyncio.wait_for(
                self.llm.complete([Message(role="user", content=prompt)], []),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError("memory_extraction_provider_failed") from exc
        if response.kind != "final" or not response.content:
            raise ValueError("memory_extraction_invalid_response")
        try:
            return ExtractionResult.model_validate_json(response.content)
        except (ValidationError, ValueError) as exc:
            raise ValueError("memory_extraction_invalid_schema") from exc

    @staticmethod
    def auto_eligible(fact: ExtractedFact, evidence_text: str) -> bool:
        return (
            fact.confidence >= 0.9
            and fact.self_reported
            and fact.durable
            and bool(_DURABLE_CUES.search(evidence_text))
            and not _TEMPORARY.search(evidence_text)
            and not _NON_ASSERTIVE.search(evidence_text)
            and not _SENSITIVE.search(evidence_text)
        )
