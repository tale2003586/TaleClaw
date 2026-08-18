from concurrent.futures import ThreadPoolExecutor
import threading
import time

from memory.embeddings import FastEmbedProvider, HashEmbeddingProvider


class FakeTextEmbedding:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def embed(self, texts):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls += 1
        try:
            time.sleep(0.005)
            return [[0.25, 0.75] for _text in texts]
        finally:
            with self._lock:
                self.active -= 1


def _provider(fake_model: FakeTextEmbedding) -> FastEmbedProvider:
    provider = object.__new__(FastEmbedProvider)
    provider.model_name = "fake"
    provider._model = fake_model
    provider._dimensions = 0
    provider._lock = threading.RLock()
    return provider


def test_fastembed_shared_model_access_is_serialized_and_dimension_probe_is_safe():
    fake_model = FakeTextEmbedding()
    provider = _provider(fake_model)

    with ThreadPoolExecutor(max_workers=8) as pool:
        dimensions = list(pool.map(lambda _index: provider.vector_size, range(8)))
        vectors = list(pool.map(provider.embed_dense, [f"text-{i}" for i in range(8)]))

    assert dimensions == [2] * 8
    assert vectors == [[0.25, 0.75]] * 8
    assert provider.vector_size == 2
    assert fake_model.max_active == 1
    assert fake_model.calls == 9


def test_hash_embedding_behavior_remains_deterministic():
    provider = HashEmbeddingProvider(dimensions=8)

    assert provider.embed_dense("same text") == provider.embed_dense("same text")
    assert provider.embed_sparse("same text") == provider.embed_sparse("same text")
