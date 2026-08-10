"""Multi-UID collapse, strikes, exclusion and endpoint-collision penalties."""

from __future__ import annotations

import time

import pytest

from violet.constants import MULTI_UID_STRIKE_BLACKLIST, MULTI_UID_STRIKE_EXCLUSION
from violet.validator.antigaming import (
    apply_endpoint_collision_penalty,
    apply_multi_uid_policy,
    endpoint_collisions,
)
from violet.validator.scoring import MinerScore
from violet.validator.store import ValidatorStore


@pytest.fixture
def store(tmp_path):
    instance = ValidatorStore(str(tmp_path / "validator.sqlite3"))
    yield instance
    instance.close()


def score(hotkey: str, final: float, uid: int = 0) -> MinerScore:
    return MinerScore(
        hotkey=hotkey, uid=uid, capacity=final, work=final, quality=final,
        raw=final, smoothed=final, final=final,
    )


class TestMultiUIDPolicy:
    def test_single_hotkey_untouched(self, store):
        scores = [score("a", 0.8, 0)]
        result, report = apply_multi_uid_policy(scores, {"a": "cold1"}, store)
        assert result[0].final == 0.8
        assert not report.zeroed

    def test_only_the_best_sibling_survives(self, store):
        scores = [score("a", 0.9, 0), score("b", 0.5, 1), score("c", 0.7, 2)]
        coldkeys = {"a": "cold1", "b": "cold1", "c": "cold1"}
        result, report = apply_multi_uid_policy(scores, coldkeys, store)

        by_hotkey = {s.hotkey: s.final for s in result}
        assert by_hotkey["a"] == 0.9
        assert by_hotkey["b"] == 0.0
        assert by_hotkey["c"] == 0.0
        assert set(report.zeroed) == {"b", "c"}
        assert report.retained["cold1"] == "a"

    def test_distinct_coldkeys_are_not_collapsed(self, store):
        scores = [score("a", 0.9, 0), score("b", 0.8, 1)]
        result, report = apply_multi_uid_policy(
            scores, {"a": "cold1", "b": "cold2"}, store
        )
        assert all(s.final > 0 for s in result)
        assert not report.zeroed

    def test_zeroed_miners_are_told_why(self, store):
        scores = [score("a", 0.9, 0), score("b", 0.5, 1)]
        result, _ = apply_multi_uid_policy(scores, {"a": "cold1", "b": "cold1"}, store)
        loser = next(s for s in result if s.hotkey == "b")
        assert any("zeroed" in note for note in loser.notes)

    def test_repeat_offence_excludes_the_whole_coldkey(self, store):
        coldkeys = {"a": "cold1", "b": "cold1"}
        for _ in range(MULTI_UID_STRIKE_EXCLUSION):
            scores = [score("a", 0.9, 0), score("b", 0.5, 1)]
            result, report = apply_multi_uid_policy(scores, coldkeys, store)

        # Once excluded, even the winner earns nothing.
        assert all(s.final == 0.0 for s in result)
        assert "cold1" in report.excluded

    def test_persistent_offence_blacklists(self, store):
        coldkeys = {"a": "cold1", "b": "cold1"}
        for _ in range(MULTI_UID_STRIKE_BLACKLIST + 1):
            scores = [score("a", 0.9, 0), score("b", 0.5, 1)]
            result, report = apply_multi_uid_policy(scores, coldkeys, store)

        assert store.coldkey_state("cold1")["blacklisted"] is True
        assert all(s.final == 0.0 for s in result)

    def test_blacklist_survives_reform(self, store):
        store.add_coldkey_strike("cold1", blacklist=True, detail="test")
        scores = [score("a", 0.9, 0)]
        result, report = apply_multi_uid_policy(scores, {"a": "cold1"}, store)
        assert result[0].final == 0.0
        assert "cold1" in report.blacklisted

    def test_clean_window_clears_strikes(self, store):
        coldkeys = {"a": "cold1", "b": "cold1"}
        apply_multi_uid_policy([score("a", 0.9, 0), score("b", 0.5, 1)], coldkeys, store)
        assert store.coldkey_state("cold1")["strikes"] == 1

        # Operator consolidates onto one hotkey: the strike should clear rather
        # than compound forever.
        apply_multi_uid_policy([score("a", 0.9, 0)], {"a": "cold1"}, store)
        assert store.coldkey_state("cold1")["strikes"] == 0

    def test_unknown_coldkey_is_skipped(self, store):
        scores = [score("a", 0.9, 0)]
        result, _ = apply_multi_uid_policy(scores, {}, store)
        assert result[0].final == 0.9

    def test_works_without_a_store(self):
        scores = [score("a", 0.9, 0), score("b", 0.5, 1)]
        result, report = apply_multi_uid_policy(scores, {"a": "c1", "b": "c1"}, None)
        assert {s.hotkey: s.final for s in result}["b"] == 0.0


class TestEndpointCollisions:
    def test_detects_shared_host(self):
        collisions = endpoint_collisions(
            {"a": "http://1.2.3.4:8091", "b": "http://1.2.3.4:8091", "c": "http://5.6.7.8:8091"}
        )
        assert len(collisions) == 1
        assert collisions["1.2.3.4:8091"] == ["a", "b"]

    def test_normalises_scheme_and_trailing_slash(self):
        collisions = endpoint_collisions(
            {"a": "https://m.example.com/", "b": "https://M.example.com"}
        )
        assert len(collisions) == 1

    def test_different_ports_are_distinct(self):
        # Two containers on one host is a legitimate multi-GPU layout.
        assert endpoint_collisions(
            {"a": "http://1.2.3.4:8091", "b": "http://1.2.3.4:8092"}
        ) == {}

    def test_score_divided_among_sharers(self):
        scores = [score("a", 0.8, 0), score("b", 0.8, 1)]
        endpoints = {"a": "http://1.2.3.4:8091", "b": "http://1.2.3.4:8091"}
        apply_endpoint_collision_penalty(scores, endpoints, {"a": "c1", "b": "c2"})
        # One box earns one box's worth however many UIDs point at it.
        assert all(s.final == pytest.approx(0.4) for s in scores)

    def test_no_penalty_without_collision(self):
        scores = [score("a", 0.8, 0)]
        apply_endpoint_collision_penalty(
            scores, {"a": "http://1.2.3.4:8091"}, {"a": "c1"}
        )
        assert scores[0].final == 0.8
