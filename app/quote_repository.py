"""SQLite repository for quote snapshots and their lifecycle.

The repository lives in the same process as the API: migrations run at
application startup and every operation opens a short-lived connection
to the same database file — no external service is introduced. Amounts
are stored as TEXT so the exact Decimal snapshot survives the round
trip (SQLite has no native decimal type).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from .quotes import (
    MAX_TOTAL_SHEETS,
    QUOTE_STATUSES,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    Quote,
    QuoteSnapshot,
    decimal_to_str,
    format_quote_id,
)

DEFAULT_DB_PATH = "quotes.sqlite3"

# Versioned migrations, applied in order at application startup. Each
# runs once and is recorded in schema_migrations, so startup is
# idempotent against an existing database file.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        f"""
        CREATE TABLE quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_pages INTEGER NOT NULL,
            print_run INTEGER NOT NULL,
            unit_price TEXT NOT NULL,
            loss_rate TEXT NOT NULL,
            sheets_per_booklet INTEGER NOT NULL,
            base_sheets INTEGER NOT NULL,
            loss_sheets INTEGER NOT NULL,
            total_sheets INTEGER NOT NULL,
            total_amount TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ({", ".join(repr(s) for s in QUOTE_STATUSES)})),
            created_at TEXT NOT NULL,
            confirmed_at TEXT
        )
        """,
    ),
)


class QuoteNotFound(Exception):
    """Raised when no quote exists for the requested number."""


class QuoteAlreadyConfirmed(Exception):
    """Raised when an already-confirmed quote is confirmed again."""


class QuoteExceedsStorageCapacity(Exception):
    """Raised when a snapshot's INTEGER columns cannot fit storage.

    Defence in depth: the request boundary rejects such input with a
    field-level 422 before this layer is reached, but a snapshot built
    directly against the core is still refused up front instead of dying
    inside the SQLite driver with an OverflowError mid-insert.
    """


# Every sheet/run column is a signed 64-bit SQLite INTEGER.
_INTEGER_COLUMNS = (
    "total_pages",
    "print_run",
    "sheets_per_booklet",
    "base_sheets",
    "loss_sheets",
    "total_sheets",
)


def _assert_snapshot_fits_storage(snapshot: QuoteSnapshot) -> None:
    for column in _INTEGER_COLUMNS:
        value = getattr(snapshot, column)
        if value > MAX_TOTAL_SHEETS:
            raise QuoteExceedsStorageCapacity(
                f"snapshot column {column}={value} exceeds the SQLite "
                f"INTEGER capacity of {MAX_TOTAL_SHEETS}."
            )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuoteRepository:
    """Persists quote snapshots in a SQLite database file."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        try:
            # The inner ``with`` commits on success and rolls back on
            # exception; the finally closes the connection itself.
            with connection:
                yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        """Apply pending migrations; safe to run on every startup."""

        with self._connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, statement in MIGRATIONS:
                if version not in applied:
                    connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, applied_at)"
                        " VALUES (?, ?)",
                        (version, _utcnow()),
                    )

    def insert(self, snapshot: QuoteSnapshot) -> Quote:
        """Persist a new pending quote and return it with its number."""

        # Refuse snapshots the INTEGER columns cannot hold before opening
        # the write transaction; the request boundary already prevents
        # these via 422, this keeps direct core callers safe as well.
        _assert_snapshot_fits_storage(snapshot)
        created_at = _utcnow()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO quotes (
                    total_pages, print_run, unit_price, loss_rate,
                    sheets_per_booklet, base_sheets, loss_sheets,
                    total_sheets, total_amount, status,
                    created_at, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    snapshot.total_pages,
                    snapshot.print_run,
                    decimal_to_str(snapshot.unit_price),
                    decimal_to_str(snapshot.loss_rate),
                    snapshot.sheets_per_booklet,
                    snapshot.base_sheets,
                    snapshot.loss_sheets,
                    snapshot.total_sheets,
                    decimal_to_str(snapshot.total_amount),
                    STATUS_PENDING,
                    created_at,
                ),
            )
            row_id = cursor.lastrowid
        assert row_id is not None  # sqlite always sets it after INSERT
        return Quote(
            quote_id=format_quote_id(row_id),
            snapshot=snapshot,
            status=STATUS_PENDING,
            created_at=created_at,
            confirmed_at=None,
        )

    def get(self, row_id: int) -> Quote | None:
        """Return the stored quote for ``row_id``, or None."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM quotes WHERE id = ?", (row_id,)
            ).fetchone()
        return _row_to_quote(row) if row is not None else None

    def get_many(self, row_ids: tuple[int, ...]) -> list[Quote | None]:
        """Fetch several quotes in one read, keeping the input order.

        Returns one slot per requested id: the stored quote, or ``None``
        for an id without a row. The SQL ``IN`` clause itself does not
        guarantee result order, so rows are mapped back onto the request
        explicitly — the comparison relies on ``result[0]`` being the
        baseline even when its number is larger than the candidate's.
        """

        if not row_ids:
            return []
        placeholders = ", ".join("?" for _ in row_ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM quotes WHERE id IN ({placeholders})",
                tuple(row_ids),
            ).fetchall()
        by_id = {row["id"]: _row_to_quote(row) for row in rows}
        return [by_id.get(row_id) for row_id in row_ids]

    def confirm(self, row_id: int) -> Quote:
        """Move a pending quote to confirmed and return the stored row.

        The UPDATE is conditional on the current status, so a concurrent
        or repeated confirm can never rewrite the stored snapshot or the
        original confirmed timestamp.

        Raises:
            QuoteNotFound: if no quote has this number.
            QuoteAlreadyConfirmed: if the quote is already confirmed.
        """

        confirmed_at = _utcnow()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE quotes SET status = ?, confirmed_at = ?"
                " WHERE id = ? AND status = ?",
                (STATUS_CONFIRMED, confirmed_at, row_id, STATUS_PENDING),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT * FROM quotes WHERE id = ?", (row_id,)
                ).fetchone()
                if row is None:
                    raise QuoteNotFound(row_id)
                raise QuoteAlreadyConfirmed(row_id)
            row = connection.execute(
                "SELECT * FROM quotes WHERE id = ?", (row_id,)
            ).fetchone()
        return _row_to_quote(row)


def _row_to_quote(row: sqlite3.Row) -> Quote:
    snapshot = QuoteSnapshot(
        total_pages=row["total_pages"],
        print_run=row["print_run"],
        unit_price=Decimal(row["unit_price"]),
        loss_rate=Decimal(row["loss_rate"]),
        sheets_per_booklet=row["sheets_per_booklet"],
        base_sheets=row["base_sheets"],
        loss_sheets=row["loss_sheets"],
        total_sheets=row["total_sheets"],
        total_amount=Decimal(row["total_amount"]),
    )
    return Quote(
        quote_id=format_quote_id(row["id"]),
        snapshot=snapshot,
        status=row["status"],
        created_at=row["created_at"],
        confirmed_at=row["confirmed_at"],
    )
