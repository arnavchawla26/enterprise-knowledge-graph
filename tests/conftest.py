"""Small hand-made fixtures: a 6-row enterprise with every mess rule present once."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def enterprise() -> dict[str, pd.DataFrame]:
    from ekg.load import read_enterprise
    return read_enterprise(FIX / "enterprise")


@pytest.fixture
def truth_dir() -> Path:
    return FIX / "truth"


def neo4j_available() -> bool:
    if os.environ.get("EKG_SKIP_LIVE"):
        return False
    try:
        from ekg.config import neo4j_driver
        with neo4j_driver() as drv:
            drv.verify_connectivity()
        return True
    except Exception:  # noqa: BLE001
        return False


live = pytest.mark.skipif(not neo4j_available(), reason="no Neo4j listening (docker compose up -d neo4j)")
