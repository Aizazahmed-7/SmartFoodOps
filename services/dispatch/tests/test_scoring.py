"""The pure arithmetic: distances, acceptance, deterministic rank."""

import pytest
from dispatch.domain.scoring import (
    Candidate,
    acceptance_rate,
    haversine_m,
    radius_km_for,
    rank,
    score,
)


def test_haversine_known_distance():
    # Springfield box corner-to-corner: ~5.6 km (checked by hand).
    d = haversine_m(39.780, -89.670, 39.820, -89.630)
    assert d == pytest.approx(5560, rel=0.02)
    assert haversine_m(39.8, -89.65, 39.8, -89.65) == 0.0


def test_acceptance_rate_branches():
    assert acceptance_rate(0, 0) == 1.0  # cold start scores perfect
    assert acceptance_rate(4, 2) == 0.5
    assert acceptance_rate(2, 5) == 1.0  # clamped (replayed accepts can outrun offers)


def test_score_stretches_distance_for_decliners():
    keen = Candidate("keen", 1000.0, offers_made=10, offers_accepted=10)
    ghost = Candidate("ghost", 1000.0, offers_made=10, offers_accepted=0)
    assert score(keen) == 1000.0
    assert score(ghost) == 1500.0  # +50% at zero acceptance


def test_rank_is_a_deterministic_total_order():
    a = Candidate("r_b", 1000.0)
    b = Candidate("r_a", 1000.0)  # identical score — id breaks the tie
    c = Candidate("r_c", 500.0)
    assert [x.rider_id for x in rank([a, b, c])] == ["r_c", "r_a", "r_b"]


def test_radius_widen_schedule():
    for attempt in (1, 2, 3):
        assert radius_km_for(attempt, base_km=3.0, widened_km=6.0, widen_after=3) == 3.0
    assert radius_km_for(4, base_km=3.0, widened_km=6.0, widen_after=3) == 6.0
