"""Exact memory reuse must not depend on which lifecycle state was touched last."""

from dataclasses import replace
from datetime import timedelta

import pytest

from memory.command_service import MemoryCommandService
from memory.domain import MemoryStatus
from memory.postgres_repository import PostgresMemoryRepository
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository
from tests.postgres_utils import temporary_postgres_schema
from tests.test_memory_verification_boundary import CONTEXT, NOW, proposal


@pytest.fixture(params=["in_memory", "postgres"])
def repository(request):
    if request.param == "in_memory":
        yield InMemoryMemoryRepository()
    else:
        with temporary_postgres_schema("memory_exact_selection") as dsn:
            yield PostgresMemoryRepository(dsn)


@pytest.mark.parametrize("shadow", ["active", "expired_candidate", "future_candidate"])
def test_reproposal_reuses_valid_candidate_despite_newer_exact_record(repository, shadow):
    commands = MemoryCommandService(repository, clock=lambda: NOW)
    candidate = commands.propose(proposal(), CONTEXT)
    newer = replace(
        candidate,
        id="newer-exact-record",
        status=MemoryStatus.ACTIVE if shadow == "active" else MemoryStatus.CANDIDATE,
        updated_at=NOW + timedelta(days=2),
        valid_from=NOW + timedelta(days=1) if shadow == "future_candidate" else NOW - timedelta(days=1),
        valid_until=NOW if shadow == "expired_candidate" else None,
    )
    repository.create(newer)

    repeated = commands.propose(proposal(), CONTEXT)

    assert repeated.id == candidate.id
    assert repeated.status is MemoryStatus.CANDIDATE
    assert len(repository.list_evidence(candidate.id)) == 1
    assert repository.get(newer.id) == newer
    assert repository.list_evidence(newer.id) == []


def test_exact_lookup_filters_before_selecting_latest(repository):
    commands = MemoryCommandService(repository, clock=lambda: NOW)
    candidate = commands.propose(proposal(), CONTEXT)
    active = replace(candidate, id="active-exact", status=MemoryStatus.ACTIVE,
                     updated_at=NOW + timedelta(days=1))
    expired = replace(candidate, id="expired-exact", valid_from=NOW - timedelta(days=1),
                      valid_until=NOW, updated_at=NOW + timedelta(days=2))
    repository.create(active)
    repository.create(expired)
    args = (candidate.owner, candidate.kind, candidate.normalized_content)

    assert repository.find_exact(*args) == expired
    assert repository.find_exact(*args, status=MemoryStatus.CANDIDATE, now=NOW) == candidate
    assert repository.find_exact(*args, status=MemoryStatus.ACTIVE, now=NOW) == active
    assert repository.find_exact(*args, now=NOW) == active
    assert repository.find_exact(*args, now=NOW - timedelta(days=2)) is None
