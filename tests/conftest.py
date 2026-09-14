import pytest

from tradebot.store.db import connect
from tradebot.store.repo import Repo


@pytest.fixture
def repo():
    conn = connect(":memory:")
    yield Repo(conn)
    conn.close()
