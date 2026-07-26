import unittest

from memory.vector_index import MemoryHit, MemoryRecord
from memory.vector_runtime import history_vector_scope_for_session
from runtime.context.retrieval import ContextRetrievalService
from runtime.sessions.session import Session


class ScopeRecordingIndex:
    def __init__(self) -> None:
        self.records: list[MemoryRecord] = []
        self.searches: list[dict] = []

    def upsert(self, record: MemoryRecord) -> None:
        self.records.append(record)

    def search(self, **kwargs) -> list[MemoryHit]:
        self.searches.append(dict(kwargs))
        return [
            MemoryHit(
                id=record.id,
                text=record.text,
                score=0.9,
                scope=record.scope,
                source_type=record.source_type,
                source_ref=record.source_ref,
                metadata=record.metadata,
            )
            for record in self.records
            if record.scope == kwargs["scope"]
        ]


class EpisodicHistoryScopeBaselineTests(unittest.TestCase):
    def test_legacy_user_scope_reproduces_cross_session_turn_leak(self) -> None:
        """Phase 0 reproduction; Phase 4 changes the expected result to no hit."""
        session_a = Session(
            id="web:alice:a",
            metadata={"user_id": "alice"},
        )
        session_b = Session(
            id="web:alice:b",
            metadata={"user_id": "alice"},
        )
        session_c = Session(
            id="web:bob:c",
            metadata={"user_id": "bob"},
        )
        index = ScopeRecordingIndex()
        index.upsert(MemoryRecord(
            id="session-a-turn",
            text="unique phrase from session A",
            scope=history_vector_scope_for_session(session_a),
            source_type="session_turn",
            source_ref="web:alice:a:1",
            metadata={"session_id": session_a.id},
        ))
        service = ContextRetrievalService(
            history_vector_index=index,
            history_scope_resolver=history_vector_scope_for_session,
        )

        block_b, hits_b = service.retrieve_history(
            session=session_b,
            current_request="unique phrase",
            active_turn_messages=[],
        )
        block_c, hits_c = service.retrieve_history(
            session=session_c,
            current_request="unique phrase",
            active_turn_messages=[],
        )

        self.assertEqual("user:alice", history_vector_scope_for_session(session_a))
        self.assertEqual(
            history_vector_scope_for_session(session_a),
            history_vector_scope_for_session(session_b),
        )
        self.assertEqual(1, len(hits_b))
        self.assertIn("session A", block_b)
        self.assertEqual([], hits_c)
        self.assertEqual("", block_c)


if __name__ == "__main__":
    unittest.main()
