import pytest

from app.main import create_app
from app.review.store import Store


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "workbench.sqlite3"))
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "store.sqlite3"))
