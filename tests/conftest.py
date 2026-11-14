import pytest

from src.store.db import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    d.init()
    yield d
    d.close()


@pytest.fixture
def file_db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.init()
    yield d
    d.close()
