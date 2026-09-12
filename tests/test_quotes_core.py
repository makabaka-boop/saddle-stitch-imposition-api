"""Invariant tests for the pure quoting core.

These do not touch FastAPI or SQLite: they pin down the pricing
arithmetic (sheets per booklet, ceiled loss, two-place Decimal amount)
and the strict input validation for every quote field.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from app.imposition import InvalidTotalPages
from app.quotes import (
    IncompatibleQuotes,
    InvalidLossRate,
    InvalidPrintRun,
    InvalidUnitPrice,
    STATUS_PENDING,
    Quote,
    build_snapshot,
    compare_quotes,
    decimal_to_str,
    format_quote_id,
    parse_quote_id,
    validate_loss_rate,
    validate_print_run,
    validate_unit_price,
)


# ---------------------------------------------------------------------------
# The acceptance snapshot: 1000 booklets of 16 pages.
# ---------------------------------------------------------------------------


def test_acceptance_snapshot_thousand_sixteen_page_booklets() -> None:
    snapshot = build_snapshot(16, 1000, "1.25", 0.05)

    assert snapshot.total_pages == 16
    assert snapshot.print_run == 1000
    assert snapshot.unit_price == Decimal("1.25")
    assert snapshot.loss_rate == Decimal("0.05")
    assert snapshot.sheets_per_booklet == 4
    assert snapshot.base_sheets == 4000
    assert snapshot.loss_sheets == 200
    assert snapshot.total_sheets == 4200
    assert snapshot.total_amount == Decimal("5250.00")


def test_loss_rounds_up_to_whole_sheets() -> None:
    # 4 sheets/booklet * 1001 booklets = 4004 base sheets; at 5% the
    # loss is 200.2, which rounds up to 201 whole sheets.
    snapshot = build_snapshot(16, 1001, "1", 0.05)
    assert snapshot.base_sheets == 4004
    assert snapshot.loss_sheets == 201
    assert snapshot.total_sheets == 4205


def test_zero_loss_rate_adds_no_sheets() -> None:
    snapshot = build_snapshot(16, 1000, "1.25", 0)
    assert snapshot.loss_sheets == 0
    assert snapshot.total_sheets == 4000
    assert snapshot.total_amount == Decimal("5000.00")


def test_amount_is_quantized_half_up_to_two_places() -> None:
    # 3 sheets at 0.255 -> 0.765 -> 0.77 (ROUND_HALF_UP).
    snapshot = build_snapshot(4, 3, "0.255", 0)
    assert snapshot.total_sheets == 3
    assert snapshot.total_amount == Decimal("0.77")


def test_amount_always_carries_two_places() -> None:
    snapshot = build_snapshot(8, 10, 2, 0)
    assert snapshot.total_amount == Decimal("40.00")


def test_integer_unit_price_is_exact() -> None:
    snapshot = build_snapshot(8, 10, 2, 0)
    assert snapshot.unit_price == Decimal(2)


def test_float_loss_rate_is_converted_exactly() -> None:
    # 0.05 the JSON number must mean exactly 5%, not the binary float.
    snapshot = build_snapshot(16, 1000, "1", 0.05)
    assert snapshot.loss_rate == Decimal("0.05")
    assert snapshot.loss_sheets == 200


def test_huge_print_run_does_not_lose_precision() -> None:
    # Far beyond the default 28-digit decimal context: the loss and the
    # amount must still be exact. 4 pages -> 1 sheet per booklet.
    snapshot = build_snapshot(4, 10**30, "1", 0.05)
    assert snapshot.base_sheets == 10**30
    assert snapshot.loss_sheets == 5 * 10**28
    assert snapshot.total_sheets == 105 * 10**28
    assert snapshot.total_amount == Decimal(105 * 10**28)


def test_snapshot_is_immutable() -> None:
    snapshot = build_snapshot(16, 1000, "1.25", 0.05)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.total_amount = Decimal("0.00")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# print_run validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 2, 1000, 1_000_000])
def test_print_run_accepts_positive_ints(value: int) -> None:
    assert validate_print_run(value) == value


@pytest.mark.parametrize("value", [0, -1, -1000])
def test_print_run_rejects_non_positive(value: int) -> None:
    with pytest.raises(InvalidPrintRun, match="at least 1"):
        validate_print_run(value)


@pytest.mark.parametrize("value", [True, False, "100", 1.5, 1000.0, None, [1000]])
def test_print_run_rejects_non_integers(value: object) -> None:
    with pytest.raises(InvalidPrintRun, match="integer"):
        validate_print_run(value)


# ---------------------------------------------------------------------------
# unit_price validation: exact decimals only, never floats.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.25", Decimal("1.25")),
        ("0.01", Decimal("0.01")),
        ("0", Decimal("0")),
        (0, Decimal(0)),
        (2, Decimal(2)),
        (Decimal("3.50"), Decimal("3.50")),
    ],
)
def test_unit_price_accepts_exact_decimals(raw, expected: Decimal) -> None:
    assert validate_unit_price(raw) == expected


@pytest.mark.parametrize("value", [1.25, 2.0, 0.1])
def test_unit_price_rejects_floats(value: float) -> None:
    # Even an integral float is rejected: the type itself is inexact.
    with pytest.raises(InvalidUnitPrice, match="not a float"):
        validate_unit_price(value)


@pytest.mark.parametrize("value", [True, False])
def test_unit_price_rejects_bools(value: bool) -> None:
    with pytest.raises(InvalidUnitPrice):
        validate_unit_price(value)


@pytest.mark.parametrize("value", ["-0.01", -1])
def test_unit_price_rejects_negatives(value) -> None:
    with pytest.raises(InvalidUnitPrice, match="negative"):
        validate_unit_price(value)


@pytest.mark.parametrize("value", ["abc", "", "1.2.3", None, [1], {"p": 1}])
def test_unit_price_rejects_non_decimals(value: object) -> None:
    with pytest.raises(InvalidUnitPrice):
        validate_unit_price(value)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_unit_price_rejects_non_finite(value: str) -> None:
    with pytest.raises(InvalidUnitPrice, match="finite"):
        validate_unit_price(value)


# ---------------------------------------------------------------------------
# loss_rate validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, Decimal(0)),
        (1, Decimal(1)),
        (0.05, Decimal("0.05")),
        ("0.05", Decimal("0.05")),
        ("1.5", Decimal("1.5")),
        (Decimal("0.1"), Decimal("0.1")),
    ],
)
def test_loss_rate_accepts_non_negative_decimals(raw, expected: Decimal) -> None:
    assert validate_loss_rate(raw) == expected


@pytest.mark.parametrize("value", [-0.01, "-0.5", -1])
def test_loss_rate_rejects_negatives(value) -> None:
    with pytest.raises(InvalidLossRate, match="negative"):
        validate_loss_rate(value)


@pytest.mark.parametrize("value", [True, False, "abc", "", None, [0.05]])
def test_loss_rate_rejects_non_numbers(value: object) -> None:
    with pytest.raises(InvalidLossRate):
        validate_loss_rate(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", "Infinity"])
def test_loss_rate_rejects_non_finite(value) -> None:
    with pytest.raises(InvalidLossRate, match="finite"):
        validate_loss_rate(value)


# ---------------------------------------------------------------------------
# build_snapshot delegates every field to its validator.
# ---------------------------------------------------------------------------


def test_build_snapshot_rejects_invalid_total_pages() -> None:
    with pytest.raises(InvalidTotalPages):
        build_snapshot(18, 1000, "1.25", 0.05)


def test_build_snapshot_rejects_invalid_print_run() -> None:
    with pytest.raises(InvalidPrintRun):
        build_snapshot(16, 0, "1.25", 0.05)


def test_build_snapshot_rejects_float_unit_price() -> None:
    with pytest.raises(InvalidUnitPrice):
        build_snapshot(16, 1000, 1.25, 0.05)


def test_build_snapshot_rejects_negative_loss_rate() -> None:
    with pytest.raises(InvalidLossRate):
        build_snapshot(16, 1000, "1.25", -0.05)


# ---------------------------------------------------------------------------
# Quote numbers and decimal rendering.
# ---------------------------------------------------------------------------


def test_quote_id_roundtrip() -> None:
    assert format_quote_id(1) == "Q-000001"
    assert format_quote_id(42) == "Q-000042"
    assert parse_quote_id("Q-000001") == 1
    assert parse_quote_id("Q-1") == 1
    assert parse_quote_id("42") == 42


@pytest.mark.parametrize(
    "value", ["", "Q-", "abc", "Q-abc", "-1", "1.5", "Q-1.5", None, 42]
)
def test_parse_quote_id_rejects_garbage(value: object) -> None:
    assert parse_quote_id(value) is None


def test_decimal_to_str_avoids_scientific_notation() -> None:
    assert decimal_to_str(Decimal("5250.00")) == "5250.00"
    assert decimal_to_str(Decimal("0.05")) == "0.05"
    assert decimal_to_str(Decimal("1E-7")) == "0.0000001"


# ---------------------------------------------------------------------------
# compare_quotes: signed paper/amount differences for one print job.
# ---------------------------------------------------------------------------


def _quote(number: int, **kwargs) -> Quote:
    """Build a pending Quote with the given number from snapshot kwargs."""

    snapshot = build_snapshot(
        kwargs.get("total_pages", 16),
        kwargs.get("print_run", 1000),
        kwargs.get("unit_price", "1.25"),
        kwargs.get("loss_rate", 0.05),
    )
    return Quote(
        quote_id=format_quote_id(number),
        snapshot=snapshot,
        status=STATUS_PENDING,
        created_at="2026-09-12T00:00:00+00:00",
        confirmed_at=None,
    )


def test_compare_acceptance_pair_baseline_minus_candidate() -> None:
    # Both are 16-page, 1000-booklet jobs but with different paper
    # prices/loss plans:
    #   baseline (Q-000001): 1.25/sheet, 5% loss -> 4200 sheets, 5250.00
    #   candidate (Q-000002): 1.20/sheet, 3% loss -> 4120 sheets, 4944.00
    baseline = _quote(1)
    candidate = _quote(
        2, unit_price="1.20", loss_rate=0.03
    )

    result = compare_quotes(baseline, candidate)

    assert result.baseline_quote_id == "Q-000001"
    assert result.candidate_quote_id == "Q-000002"
    assert result.sheet_difference == 4200 - 4120 == 80
    assert result.amount_difference == Decimal("5250.00") - Decimal("4944.00")
    assert result.amount_difference == Decimal("306.00")
    assert result.lower_quote_id == "Q-000002"


def test_compare_swap_reverses_both_signs_but_not_lower_id() -> None:
    baseline = _quote(1)
    candidate = _quote(2, unit_price="1.20", loss_rate=0.03)

    swapped = compare_quotes(candidate, baseline)

    assert swapped.baseline_quote_id == "Q-000002"
    assert swapped.candidate_quote_id == "Q-000001"
    assert swapped.sheet_difference == -80
    assert swapped.amount_difference == Decimal("-306.00")
    # The cheaper side is a property of the two quotes, not of order.
    assert swapped.lower_quote_id == "Q-000002"


def test_compare_identical_amounts_ties_with_null_lower_id() -> None:
    first = _quote(1)
    second = _quote(2)

    result = compare_quotes(first, second)

    assert result.sheet_difference == 0
    assert result.amount_difference == Decimal("0.00")
    assert result.lower_quote_id is None


def test_compare_cheaper_baseline_names_baseline_as_lower() -> None:
    baseline = _quote(1, unit_price="1.00", loss_rate=0)  # 4000.00
    candidate = _quote(2)  # 5250.00

    result = compare_quotes(baseline, candidate)

    assert result.amount_difference == Decimal("-1250.00")
    assert result.sheet_difference == 4000 - 4200
    assert result.lower_quote_id == "Q-000001"


def test_compare_rejects_different_total_pages() -> None:
    baseline = _quote(1, total_pages=16)
    candidate = _quote(2, total_pages=8)

    with pytest.raises(IncompatibleQuotes, match="16 pages"):
        compare_quotes(baseline, candidate)


def test_compare_rejects_different_print_runs() -> None:
    baseline = _quote(1, print_run=1000)
    candidate = _quote(2, print_run=500)

    with pytest.raises(IncompatibleQuotes, match="1000 booklets"):
        compare_quotes(baseline, candidate)


def test_compare_keeps_exact_two_place_money_difference() -> None:
    # 4 pages, 3 booklets, no loss: 3 sheets.
    # baseline at 0.255 -> 0.77 (half-up snapshot); candidate at 0.25 -> 0.75.
    baseline = _quote(
        1, total_pages=4, print_run=3, unit_price="0.255", loss_rate=0
    )
    candidate = _quote(
        2, total_pages=4, print_run=3, unit_price="0.25", loss_rate=0
    )

    result = compare_quotes(baseline, candidate)

    assert result.sheet_difference == 0
    assert result.amount_difference == Decimal("0.02")
    assert decimal_to_str(result.amount_difference) == "0.02"


def test_compare_negative_amount_difference_renders_with_minus_sign() -> None:
    baseline = _quote(1, total_pages=4, print_run=1, unit_price="0.01", loss_rate=0)
    candidate = _quote(2, total_pages=4, print_run=1, unit_price="1.00", loss_rate=0)

    result = compare_quotes(baseline, candidate)

    assert result.amount_difference == Decimal("-0.99")
    assert decimal_to_str(result.amount_difference) == "-0.99"
