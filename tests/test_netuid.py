"""Chain netuid defaults for Violet (mainnet 39 / testnet 292)."""

from __future__ import annotations

import os

import pytest

from violet.config import ChainConfig, _default_netuid_for_network, _resolve_netuid
from violet.constants import NETUID_MAINNET, NETUID_TESTNET


@pytest.fixture(autouse=True)
def _clear_chain_env(monkeypatch):
    for key in ("VIOLET_NETUID", "BT_NETWORK"):
        monkeypatch.delenv(key, raising=False)


def test_published_netuids():
    assert NETUID_MAINNET == 39
    assert NETUID_TESTNET == 292


@pytest.mark.parametrize(
    "network,expected",
    [
        ("finney", 39),
        ("main", 39),
        ("mainnet", 39),
        ("", 39),
        ("test", 292),
        ("testnet", 292),
    ],
)
def test_default_netuid_for_network(network, expected):
    assert _default_netuid_for_network(network) == expected


def test_resolve_from_bt_network(monkeypatch):
    monkeypatch.setenv("BT_NETWORK", "test")
    assert _resolve_netuid() == 292
    monkeypatch.setenv("BT_NETWORK", "finney")
    assert _resolve_netuid() == 39


def test_explicit_netuid_wins(monkeypatch):
    monkeypatch.setenv("BT_NETWORK", "finney")
    monkeypatch.setenv("VIOLET_NETUID", "39")
    assert _resolve_netuid() == 39


def test_validate_rejects_mismatched_pair():
    with pytest.raises(ValueError, match="292"):
        ChainConfig(netuid=39, network="test").validate()
    with pytest.raises(ValueError, match="39"):
        ChainConfig(netuid=292, network="finney").validate()


def test_validate_accepts_correct_pairs():
    ChainConfig(netuid=39, network="finney").validate()
    ChainConfig(netuid=292, network="test").validate()
