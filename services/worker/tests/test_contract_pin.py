"""The worker pins contract 1.0.0: its copy of the generated DTOs and hash must match the coordinator's."""

from pathlib import Path

from promisegraph_worker import contracts_generated
from promisegraph_worker.http import CONTRACT_VERSION

REPO = Path(__file__).resolve().parents[3]
PKG = Path(contracts_generated.__file__).parent


def test_contract_hash_and_generated_models_match_coordinator_copy():
    assert (PKG / "CONTRACT_HASH").read_text() == (REPO / "contracts" / "CONTRACT_HASH").read_text()
    assert (PKG / "CONTRACT_HASH").read_text().split()[0] == CONTRACT_VERSION
    assert Path(contracts_generated.__file__).read_bytes() == (REPO / "contracts/generated/python/promisegraph_contracts.py").read_bytes()
