import pytest


@pytest.fixture(autouse=True)
def _clean_secret_env(monkeypatch):
    """No test may observe credentials leaked into the process by another test."""
    for name in ("GROWW_API_KEY", "GROWW_TOTP_SECRET", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


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
