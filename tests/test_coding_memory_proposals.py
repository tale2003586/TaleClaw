import json
from datetime import datetime, timezone
from pathlib import Path

from applications.coding.conclusions import ConclusionCandidate, TaskConclusionExtractor
from applications.coding.promotion import TaskMemoryPromoter
from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext
from memory.domain import MemoryOwnerScope, MemorySourceType, MemoryStatus
from memory.store import MemoryStore
from models.provider import LLMResponse
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository


def test_extraction_accepts_evidence_location_revision_and_verification() -> None:
    class Provider:
        def chat(self, **kwargs):
            return LLMResponse(content=json.dumps({
                "summary": "Verified project convention",
                "conclusions": [{
                    "category": "project",
                    "content": "Tests use pytest fixtures.",
                    "evidence_file": "tests/conftest.py",
                    "evidence_location": "fixture database",
                    "code_revision": "abc123",
                    "verified": True,
                    "confidence": 0.95,
                }],
            }))

    result = TaskConclusionExtractor(provider=Provider(), model="test").extract(
        user_request="inspect tests",
        task_summary="done",
        messages=[],
    )

    candidate = result.candidates[0]
    assert candidate.evidence_file == "tests/conftest.py"
    assert candidate.evidence_location == "fixture database"
    assert candidate.code_revision == "abc123"
    assert candidate.verified


def test_old_evidence_field_remains_compatible() -> None:
    class Provider:
        def chat(self, **kwargs):
            return LLMResponse(content=json.dumps({
                "conclusions": [{
                    "category": "fact",
                    "content": "Uploads live in storage/.",
                    "evidence": "docker-compose.yml",
                    "confidence": 0.9,
                }],
            }))

    candidate = TaskConclusionExtractor(provider=Provider(), model="test").extract(
        user_request="inspect storage",
        task_summary="done",
        messages=[],
    ).candidates[0]

    assert candidate.evidence == "docker-compose.yml"
    assert candidate.evidence_file == "docker-compose.yml"


def test_coding_promotion_creates_project_candidate_without_pending_markdown(tmp_path: Path) -> None:
    global_memory = MemoryStore(tmp_path / "global")
    task_memory = MemoryStore(tmp_path / "task")
    before = global_memory.read_pending()
    repository = InMemoryMemoryRepository()
    commands = MemoryCommandService(repository)
    context = MemoryContext(
        user_id="alice",
        session_id="task:coding-a",
        application="coding",
        workspace_id="/workspace/a",
        project_id="/workspace/a/repository",
        task_id="coding-a",
    )
    candidate = ConclusionCandidate(
        category="project",
        content="Tests use pytest fixtures.",
        evidence="tests/conftest.py",
        evidence_file="tests/conftest.py",
        evidence_location="fixture database",
        code_revision="abc123",
        verified=True,
        confidence=0.95,
    )

    result = TaskMemoryPromoter(
        global_memory,
        command_service=commands,
    ).promote(
        task_id="coding-a",
        task_memory=task_memory,
        extracted_conclusions=[candidate],
        memory_context=context,
        repository_revision="fallback-revision",
    )

    assert result.promoted == [candidate]
    assert global_memory.read_pending() == before
    item = next(iter(repository.items.values()))
    assert item.status is MemoryStatus.CANDIDATE
    assert item.owner_scope is MemoryOwnerScope.PROJECT
    assert item.owner_id == "/workspace/a/repository"
    evidence = repository.list_evidence(item.id)[0]
    assert evidence.source_type is MemorySourceType.CODING_CONCLUSION
    assert evidence.task_id == "coding-a"
    assert evidence.workspace_id == "/workspace/a"
    assert evidence.project_id == "/workspace/a/repository"
    assert evidence.metadata["code_revision"] == "abc123"
    assert evidence.metadata["verified"] is True


def test_model_cannot_override_trusted_coding_owner(tmp_path: Path) -> None:
    repository = InMemoryMemoryRepository()
    commands = MemoryCommandService(repository)
    context = MemoryContext(
        user_id="alice",
        session_id="task:coding-a",
        workspace_id="trusted-workspace",
        task_id="coding-a",
    )
    candidate = ConclusionCandidate(
        category="project",
        content="Repository uses src layout.",
        evidence="model-supplied-owner=other-workspace",
        confidence=0.9,
        verified=True,
    )

    TaskMemoryPromoter(command_service=commands).promote(
        task_id="coding-a",
        task_memory=MemoryStore(tmp_path / "task"),
        extracted_conclusions=[candidate],
        memory_context=context,
    )

    item = next(iter(repository.items.values()))
    assert item.owner_scope is MemoryOwnerScope.WORKSPACE
    assert item.owner_id == "trusted-workspace"
