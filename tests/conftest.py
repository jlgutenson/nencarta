import os
import shutil
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_CACHE = REPO_ROOT / "ignore" / "_pytest_cache"
TEST_RUNS = REPO_ROOT / "ignore" / "_pytest_runs" / "unit"

TEST_CACHE.mkdir(parents=True, exist_ok=True)
TEST_RUNS.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("NUMBA_CACHE_DIR", str(TEST_CACHE / "numba"))
os.environ.setdefault("NENCARTA_CACHE_DIR", str(TEST_CACHE / "nencarta"))
os.environ.setdefault("USERPROFILE", str(TEST_CACHE / "home"))


@pytest.fixture
def workspace_tmp():
    path = TEST_RUNS / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
