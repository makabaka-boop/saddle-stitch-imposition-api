"""Persistence tests for the SQLite quote repository.

These run against a real database file in a tmp directory: migrations,
the pending -> confirmed lifecycle, and the exact Decimal round trip.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.quote_repository import (
    QuoteAlreadyConfirmed,
    QuoteNotFound,
    QuoteRepository,
)
from app.quotes import STATUS_CONFIRMED, STATUS_PENDING, build_snapshot


@pytest.fixture()
def repository(tmp_path) -> QuoteRepository:
    repo = QuoteRepository(str(tmp_path / "quotes.sqlite3"))
    repo.migrate()
    return repo


def test_migrate_is_idempotent(repository: QuoteRepository) -> None:
    # Startup runs migrate() every time; it must be safe to repeat.
    repository.migrate()
    repository.migrate()


def test_insert_assigns_independent_sequential_numbers(
    repository: QuoteRepository,
) -> None:
    first = repository.insert(build_snapshot(16, 1000, "1.25", 0.05))
    second = repository.insert(build_snapshot(8, 10, 2, 0))

    assert first.quote_id == "Q-000001"
    assert second.quote_id == "Q-000002"
    assert first.status == STATUS_PENDING
    assert first.confirmed_at is None
    assert first.created_at


def test_inserted_snapshot_round_trips_exactly(
    repository: QuoteRepository,
) -> None:
    snapshot = build_snapshot(16, 1000, "1.25", 0.05)
    repository.insert(snapshot)

    stored = repository.get(1)

    assert stored is not None
    assert stored.snapshot == snapshot
    assert stored.snapshot.unit_price == Decimal("1.25")
    assert stored.snapshot.total_amount == Decimal("5250.00")


def test_decimal_round_trip_keeps_unusual_price(
    repository: QuoteRepository,
) -> None:
    # A price with three fraction digits must survive the TEXT storage
    # unchanged; only the amount is quantized to two places.
    quote = repository.insert(build_snapshot(4, 3, "0.083", 0))
    reread = repository.get(1)

    assert reread is not None
    assert reread.snapshot.unit_price == Decimal("0.083")
    assert reread.snapshot.total_amount == Decimal("0.25")
    assert reread.quote_id == quote.quote_id


def test_get_unknown_returns_none(repository: QuoteRepository) -> None:
    assert repository.get(999999) is None


def test_get_many_returns_quotes_in_input_order(repository: QuoteRepository) -> None:
    first = repository.insert(build_snapshot(16, 1000, "1.25", 0.05))
    second = repository.insert(build_snapshot(8, 10, 2, 0))
    third = repository.insert(build_snapshot(4, 3, "0.25", 0))

    # Reversed and non-contiguous ids: the result order follows the
    # request, not the IN-clause storage order, and missing ids slot in
    # as None without shifting the found rows.
    result = repository.get_many((3, 999999, 1, 2))

    assert [quote.quote_id if quote is not None else None for quote in result] == [
        "Q-000003",
        None,
        "Q-000001",
        "Q-000002",
    ]
    assert result[0] is not None and result[0].snapshot == third.snapshot
    assert result[2] == first
    assert result[3] == second


def test_get_many_deduplicates_repeated_ids(repository: QuoteRepository) -> None:
    created = repository.insert(build_snapshot(16, 1000, "1.25", 0.05))

    result = repository.get_many((1, 1))

    assert len(result) == 2
    assert result[0] == result[1] == created


def test_get_many_empty_tuple_returns_empty_list(
    repository: QuoteRepository,
) -> None:
    assert repository.get_many(()) == []


def test_confirm_flips_status_and_stamps_timestamp(
    repository: QuoteRepository,
) -> None:
    created = repository.insert(build_snapshot(16, 1000, "1.25", 0.05))

    confirmed = repository.confirm(1)

    assert confirmed.status == STATUS_CONFIRMED
    assert confirmed.confirmed_at is not None
    # The snapshot and the creation timestamp are untouched.
    assert confirmed.snapshot == created.snapshot
    assert confirmed.created_at == created.created_at
    # And the stored row reflects the same state when re-read.
    assert repository.get(1) == confirmed


def test_confirm_unknown_raises_not_found(repository: QuoteRepository) -> None:
    with pytest.raises(QuoteNotFound):
        repository.confirm(999999)


def test_repeat_confirm_raises_and_keeps_original_snapshot(
    repository: QuoteRepository,
) -> None:
    repository.insert(build_snapshot(16, 1000, "1.25", 0.05))
    first = repository.confirm(1)

    with pytest.raises(QuoteAlreadyConfirmed):
        repository.confirm(1)

    # The failed confirm must not rewrite anything, including the
    # original confirmed timestamp.
    assert repository.get(1) == first
