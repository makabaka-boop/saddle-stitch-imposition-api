"""Persistence tests for the SQLite packing-plan repository.

These run against a real database file in a tmp directory: migrations,
the exact carton-range round trip, atomic (number-free) failure, and
reading the result straight out of the SQLite tables.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.quote_repository import (
    PackingExceedsStorageCapacity,
    QuoteRepository,
)
from app.quotes import build_snapshot
from app.packing import MAX_CARTON_COUNT, PackingAllocation, build_packing


@pytest.fixture()
def repository(tmp_path) -> QuoteRepository:
    repo = QuoteRepository(str(tmp_path / "quotes.sqlite3"))
    repo.migrate()
    return repo


@pytest.fixture()
def quote_id(repository: QuoteRepository) -> int:
    quote = repository.insert(build_snapshot(16, 1000, "1.25", 0.05))
    return int(quote.quote_id.split("-")[1])


def test_migrations_create_packing_tables(repository: QuoteRepository) -> None:
    with repository._connection() as connection:  # noqa: SLF001
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"quotes", "packing_plans", "packing_cartons", "schema_migrations"} <= names


def test_migrate_is_idempotent_with_packing_tables(repository: QuoteRepository) -> None:
    # Repeated startup migration must not error on the already-created
    # packing tables and must not double-record versions.
    repository.migrate()
    repository.migrate()
    with repository._connection() as connection:  # noqa: SLF001
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    assert versions == [1, 2, 3]


def test_insert_assigns_independent_sequential_plan_numbers(
    repository: QuoteRepository, quote_id: int
) -> None:
    first = repository.insert_packing_plan(quote_id, build_packing(1000, 300))
    second = repository.insert_packing_plan(quote_id, build_packing(1000, 500))

    assert first.plan_id == "PK-000001"
    assert second.plan_id == "PK-000002"
    assert first.quote_id == "Q-000001"
    assert second.quote_id == "Q-000001"


def test_inserted_plan_round_trips_exactly(
    repository: QuoteRepository, quote_id: int
) -> None:
    allocation = build_packing(1000, 300)
    repository.insert_packing_plan(quote_id, allocation)

    stored = repository.get_packing_plan(1)

    assert stored is not None
    assert stored.plan_id == "PK-000001"
    assert stored.quote_id == "Q-000001"
    assert stored.created_at
    assert stored.allocation == allocation
    assert stored.allocation.carton_count == 4
    assert [
        (c.start_copy, c.end_copy, c.copy_count) for c in stored.allocation.cartons
    ] == [
        (1, 300, 300),
        (301, 600, 300),
        (601, 900, 300),
        (901, 1000, 100),
    ]


def test_exact_division_plan_round_trips(
    repository: QuoteRepository, quote_id: int
) -> None:
    repository.insert_packing_plan(quote_id, build_packing(600, 300))

    stored = repository.get_packing_plan(1)
    assert stored is not None
    assert stored.allocation.carton_count == 2
    assert [
        (c.start_copy, c.end_copy, c.copy_count) for c in stored.allocation.cartons
    ] == [(1, 300, 300), (301, 600, 300)]


def test_get_unknown_plan_returns_none(repository: QuoteRepository) -> None:
    assert repository.get_packing_plan(999999) is None


def test_header_and_cartons_are_readable_from_raw_sqlite(
    repository: QuoteRepository, quote_id: int
) -> None:
    # "Read back as-is from SQLite": open the file independently and
    # verify the stored header row and every carton row verbatim.
    allocation = build_packing(1000, 300)
    plan = repository.insert_packing_plan(quote_id, allocation)

    connection = sqlite3.connect(repository._db_path)  # noqa: SLF001
    try:
        connection.row_factory = sqlite3.Row
        header = connection.execute(
            "SELECT * FROM packing_plans WHERE id = 1"
        ).fetchone()
        assert header is not None
        assert header["source_quote_id"] == quote_id
        assert header["print_run"] == 1000
        assert header["carton_capacity"] == 300
        assert header["carton_count"] == 4
        assert header["created_at"] == plan.created_at

        carton_rows = connection.execute(
            "SELECT * FROM packing_cartons WHERE packing_plan_id = 1"
            " ORDER BY carton_index"
        ).fetchall()
        assert [
            (
                row["carton_index"],
                row["start_copy"],
                row["end_copy"],
                row["copy_count"],
            )
            for row in carton_rows
        ] == [
            (0, 1, 300, 300),
            (1, 301, 600, 300),
            (2, 601, 900, 300),
            (3, 901, 1000, 100),
        ]
    finally:
        connection.close()


def test_rejected_allocation_consumes_no_plan_number(
    repository: QuoteRepository, quote_id: int
) -> None:
    # An allocation built straight from the core with an out-of-width
    # column is refused before the write transaction; the next insert must
    # still receive the first plan number.
    from app.packing import PackingAllocation

    oversized = PackingAllocation(
        print_run=1000,
        carton_capacity=300,
        carton_count=2**63,  # not storable in an INTEGER column
        cartons=build_packing(1000, 300).cartons,
    )
    with pytest.raises(PackingExceedsStorageCapacity, match="carton_count"):
        repository.insert_packing_plan(quote_id, oversized)

    after = repository.insert_packing_plan(quote_id, build_packing(1000, 300))
    assert after.plan_id == "PK-000001"


def test_max_cartons_plan_persists_all_ranges(
    repository: QuoteRepository, quote_id: int
) -> None:
    allocation = build_packing(MAX_CARTON_COUNT, 1)
    repository.insert_packing_plan(quote_id, allocation)

    stored = repository.get_packing_plan(1)
    assert stored is not None
    assert stored.allocation.carton_count == MAX_CARTON_COUNT
    assert len(stored.allocation.cartons) == MAX_CARTON_COUNT
    assert (
        stored.allocation.cartons[0].start_copy == 1
        and stored.allocation.cartons[-1].end_copy == MAX_CARTON_COUNT
    )


def test_two_plans_keep_independent_carton_rows(
    repository: QuoteRepository, quote_id: int
) -> None:
    repository.insert_packing_plan(quote_id, build_packing(1000, 300))
    repository.insert_packing_plan(quote_id, build_packing(600, 300))

    first = repository.get_packing_plan(1)
    second = repository.get_packing_plan(2)
    assert first is not None and second is not None
    assert len(first.allocation.cartons) == 4
    assert len(second.allocation.cartons) == 2
