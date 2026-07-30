import os

import pytest

TEST_DATABASE_URL = os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:test@localhost:55432/hermes_test"
)


@pytest.fixture(scope="session", autouse=True)
def _database_url():
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    yield TEST_DATABASE_URL
