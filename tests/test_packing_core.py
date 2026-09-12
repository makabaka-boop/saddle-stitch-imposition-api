"""Unit tests for the pure packing core.

Pins the deterministic first-copy-to-last carton split (the acceptance
case: 1000 copies at 300/carton -> 300/300/300/100), the exact-division
case, the 500-carton limit and strict-integer capacity validation.
"""

from __future__ import annotations

import pytest

from app.packing import (
    MAX_CARTON_COUNT,
    MIN_CARTON_CAPACITY,
    InvalidCartonCapacity,
    PackingAllocation,
    TooManyCartons,
    allocate_cartons,
    build_packing,
    parse_packing_plan_id,
)


# ---------------------------------------------------------------------------
# The acceptance split: 16 pages / 1000 copies, 300 copies per carton.
# ---------------------------------------------------------------------------


def test_acceptance_split_1000_copies_at_300() -> None:
    allocation = build_packing(1000, 300)

    assert allocation.print_run == 1000
    assert allocation.carton_capacity == 300
    assert allocation.carton_count == 4
    ranges = [
        (c.index, c.start_copy, c.end_copy, c.copy_count)
        for c in allocation.cartons
    ]
    assert ranges == [
        (0, 1, 300, 300),
        (1, 301, 600, 300),
        (2, 601, 900, 300),
        (3, 901, 1000, 100),
    ]


@pytest.mark.parametrize(
    "run,capacity,expected",
    [
        # Exact division: no short last carton.
        (600, 300, [(0, 1, 300, 300), (1, 301, 600, 300)]),
        # One copy exactly fills a carton.
        (1, 1, [(0, 1, 1, 1)]),
        # Capacity larger than the run: a single short carton.
        (10, 50, [(0, 1, 10, 10)]),
        # One copy per carton.
        (3, 1, [(0, 1, 1, 1), (1, 2, 2, 1), (2, 3, 3, 1)]),
        # Last carton short by one.
        (7, 3, [(0, 1, 3, 3), (1, 4, 6, 3), (2, 7, 7, 1)]),
    ],
)
def test_deterministic_split_ranges(
    run: int, capacity: int, expected: list[tuple[int, int, int, int]]
) -> None:
    allocation = build_packing(run, capacity)
    assert [
        (c.index, c.start_copy, c.end_copy, c.copy_count)
        for c in allocation.cartons
    ] == expected
    assert allocation.carton_count == len(expected)


def test_ranges_partition_the_whole_run_without_gap_or_overlap() -> None:
    allocation = build_packing(1000, 300)

    covered: list[int] = []
    for carton in allocation.cartons:
        assert carton.end_copy >= carton.start_copy
        covered.extend(range(carton.start_copy, carton.end_copy + 1))

    # Every copy 1..run appears exactly once and in ascending order.
    assert covered == list(range(1, 1001))
    assert sum(c.copy_count for c in allocation.cartons) == 1000


def test_allocation_is_immutable() -> None:
    allocation = build_packing(1000, 300)
    with pytest.raises((AttributeError, TypeError)):
        allocation.carton_count = 9  # type: ignore[misc]
    with pytest.raises(TypeError):
        allocation.cartons[0] = allocation.cartons[0]  # type: ignore[index]


def test_same_inputs_produce_identical_allocation() -> None:
    assert build_packing(1000, 300) == build_packing(1000, 300)


# ---------------------------------------------------------------------------
# The 500-carton limit.
# ---------------------------------------------------------------------------


def test_exactly_max_cartons_is_accepted() -> None:
    allocation = build_packing(MAX_CARTON_COUNT, 1)
    assert allocation.carton_count == MAX_CARTON_COUNT
    assert allocation.cartons[-1].end_copy == MAX_CARTON_COUNT


def test_more_than_max_cartons_is_rejected() -> None:
    # 501 copies at 1/carton needs 501 cartons.
    with pytest.raises(TooManyCartons, match="501"):
        build_packing(MAX_CARTON_COUNT + 1, 1)


def test_capacity_one_below_run_forces_too_many_cartons() -> None:
    # run = 501, capacity = 2 -> ceil(501/2) = 251 cartons: fine.
    assert build_packing(501, 2).carton_count == 251
    # run = 1001, capacity = 2 -> 501 cartons: refused.
    with pytest.raises(TooManyCartons, match="501"):
        build_packing(1001, 2)


# ---------------------------------------------------------------------------
# Strict carton_capacity validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, -300])
def test_non_positive_capacity_is_rejected(value: int) -> None:
    with pytest.raises(InvalidCartonCapacity, match="at least 1"):
        build_packing(1000, value)


@pytest.mark.parametrize("value", ["300", 300.0, 1.5, True, False, None, [300]])
def test_non_integer_capacity_is_rejected(value) -> None:
    with pytest.raises(InvalidCartonCapacity, match="integer"):
        build_packing(1000, value)


def test_capacity_beyond_integer_width_is_rejected() -> None:
    with pytest.raises(InvalidCartonCapacity, match="at most"):
        build_packing(1000, 2**63)


def test_validate_carton_capacity_returns_strict_int() -> None:
    from app.packing import validate_carton_capacity

    assert validate_carton_capacity(300) == 300
    assert MIN_CARTON_CAPACITY == 1


# ---------------------------------------------------------------------------
# Allocation helper and plan-number parsing.
# ---------------------------------------------------------------------------


def test_allocate_cartons_uses_zero_based_indices() -> None:
    cartons = allocate_cartons(5, 2)
    assert [c.index for c in cartons] == [0, 1, 2]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("PK-000001", 1),
        ("PK-000042", 42),
        ("42", 42),
        ("PK-1", 1),
        ("  PK-000007  ", 7),
    ],
)
def test_parse_packing_plan_id_accepts_canonical_and_bare_digits(
    value: str, expected: int
) -> None:
    assert parse_packing_plan_id(value) == expected


@pytest.mark.parametrize(
    "value",
    ["Q-000001", "PK-", "PK-abc", "1.5", "", None, 7, True, ["PK-1"]],
)
def test_parse_packing_plan_id_rejects_other_forms(value) -> None:
    assert parse_packing_plan_id(value) is None


def test_build_packing_returns_typed_allocation() -> None:
    allocation = build_packing(1000, 300)
    assert isinstance(allocation, PackingAllocation)
