import pytest

from pipmind.contracts import ROOT, Contracts, loads
from pipmind.dataset import load_cases


@pytest.fixture(scope="session")
def contracts():
    return Contracts()


@pytest.fixture(scope="session")
def cases():
    return {case["case_id"]: case for case in load_cases()}


@pytest.fixture(scope="session")
def messages():
    return loads((ROOT / "evals" / "fixtures" / "messages-v1.json").read_bytes())
