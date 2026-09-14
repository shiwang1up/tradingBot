import pytest


@pytest.fixture
def repo():
    # Imported lazily: store modules arrive in Task 4, and no earlier test uses this fixture.
    from tradebot.store.db import connect
    from tradebot.store.repo import Repo

    conn = connect(":memory:")
    try:
        yield Repo(conn)
    finally:
        conn.close()
