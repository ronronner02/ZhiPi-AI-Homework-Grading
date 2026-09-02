from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zhipi.config import Settings  # noqa: E402
from zhipi.main import create_app  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.for_test(tmp_path)


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as value:
        yield value

