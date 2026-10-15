import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage import load_dir  # noqa: E402

_SEED = load_dir(ROOT / "data" / "seed")


@pytest.fixture
def db():
    """A fresh in-memory copy of the seed data for every test."""
    return copy.deepcopy(_SEED)
