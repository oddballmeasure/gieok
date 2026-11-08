import pytest

from gieok.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "node", "test-node")
    yield s
    s.db.close()
