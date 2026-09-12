"""Pure finished-goods packing core: deterministic carton allocation.

This module contains no web framework or database code so the allocation
rules can be unit tested and reused independently.

After binding, the print run of a confirmed quotation is split into
cartons *from the first copy*: copies are one-based and every copy from
``1`` through ``print_run`` lands in exactly one carton, with the ranges
contiguous and non-overlapping. For a carton holding up to
``carton_capacity`` copies::

    carton_count = ceil(print_run / carton_capacity)
    carton i covers copies [i * capacity + 1, min((i + 1) * capacity, run)]

The split is therefore a pure function of (print_run, capacity): the same
inputs always produce the same box ranges, which is how the floor avoids
repacks, missed copies or a wrongly-sized last carton.
"""

from __future__ import annotations

from dataclasses import dataclass

from .quotes import SQLITE_INTEGER_MAX, validate_print_run

MIN_CARTON_CAPACITY = 1

# The handover between binding and dispatch deals in at most this many
# cartons per packing plan: a split asking for more is almost certainly a
# unit mistake (pieces instead of copies), so it is refused before any
# plan is numbered or stored.
MAX_CARTON_COUNT = 500

PACKING_PLAN_ID_PREFIX = "PK-"


class InvalidCartonCapacity(ValueError):
    """Raised when ``carton_capacity`` is not a strict positive integer."""


class TooManyCartons(ValueError):
    """Raised when a split would produce more than ``MAX_CARTON_COUNT``."""


@dataclass(frozen=True, slots=True)
class Carton:
    """One carton: a contiguous, inclusive range of one-based copy numbers.

    ``index`` is the carton number from the first one packed (0 = first
    carton). ``start_copy``/``end_copy`` bound the inclusive copy range and
    ``copy_count`` is the number of copies actually in the carton (the
    last one may be short).
    """

    index: int
    start_copy: int
    end_copy: int

    @property
    def copy_count(self) -> int:
        return self.end_copy - self.start_copy + 1


@dataclass(frozen=True, slots=True)
class PackingAllocation:
    """The immutable carton split for one print run at one capacity.

    The allocation is derived deterministically from ``print_run`` and
    ``carton_capacity``; it is computed once and persisted unchanged.
    """

    print_run: int
    carton_capacity: int
    carton_count: int
    cartons: tuple[Carton, ...]


@dataclass(frozen=True, slots=True)
class PackingPlan:
    """A persisted packing plan: an allocation plus identity and provenance.

    ``quote_id`` names the quotation whose print-run snapshot the split
    was built from (``source_quote_row_id`` is the stored reference), and
    ``created_at`` records when the plan was numbered.
    """

    plan_id: str
    quote_id: str
    allocation: PackingAllocation
    created_at: str


def validate_carton_capacity(carton_capacity: object) -> int:
    """Return ``carton_capacity`` as an int or raise InvalidCartonCapacity.

    ``bool`` is rejected explicitly even though it subclasses ``int``.
    The upper bound is the storage width: the capacity is persisted in a
    SQLite INTEGER column (signed 64-bit), so a larger value could never
    be stored and is refused here instead of overflowing on insert.
    """

    # bool check must come before isinstance(int) because bool ⊂ int.
    if isinstance(carton_capacity, bool) or not isinstance(
        carton_capacity, int
    ):
        raise InvalidCartonCapacity(
            "carton_capacity must be an integer."
        )
    if carton_capacity < MIN_CARTON_CAPACITY:
        raise InvalidCartonCapacity(
            f"carton_capacity must be at least {MIN_CARTON_CAPACITY}."
        )
    if carton_capacity > SQLITE_INTEGER_MAX:
        raise InvalidCartonCapacity(
            "carton_capacity must be at most "
            f"{SQLITE_INTEGER_MAX} (storage integer capacity)."
        )
    return carton_capacity


def allocate_cartons(print_run: int, carton_capacity: int) -> tuple[Carton, ...]:
    """Split one print run into contiguous cartons from the first copy.

    Both arguments must already be strict positive ints (this is the pure
    core; the boundary validates request input first). Every one-based
    copy number from 1 through ``print_run`` occurs in exactly one carton,
    ranges are contiguous with no gap or overlap, and every carton except
    the last holds exactly ``carton_capacity`` copies.
    """

    cartons: list[Carton] = []
    start = 1
    while start <= print_run:
        end = min(start - 1 + carton_capacity, print_run)
        cartons.append(Carton(index=len(cartons), start_copy=start, end_copy=end))
        start = end + 1
    return tuple(cartons)


def build_packing(print_run: object, carton_capacity: object) -> PackingAllocation:
    """Compute the immutable carton allocation for one print run.

    Raises:
        InvalidPrintRun: if the print run is not a storable positive int.
        InvalidCartonCapacity: if the capacity is not a strict positive int.
        TooManyCartons: if the split needs more than ``MAX_CARTON_COUNT``.
    """

    run = validate_print_run(print_run)
    capacity = validate_carton_capacity(carton_capacity)

    # Exact ceiling division (no floats): a positive run divided by a
    # positive capacity rounded up gives the number of cartons.
    carton_count = (run + capacity - 1) // capacity
    if carton_count > MAX_CARTON_COUNT:
        raise TooManyCartons(
            f"carton_capacity would produce {carton_count} cartons, "
            f"more than the maximum of {MAX_CARTON_COUNT}."
        )

    cartons = allocate_cartons(run, capacity)
    return PackingAllocation(
        print_run=run,
        carton_capacity=capacity,
        carton_count=carton_count,
        cartons=cartons,
    )


def format_packing_plan_id(row_id: int) -> str:
    """Canonical public packing plan number, e.g. ``PK-000042``."""

    return f"{PACKING_PLAN_ID_PREFIX}{row_id:06d}"


def parse_packing_plan_id(plan_id: object) -> int | None:
    """Parse a public packing plan number back to its row id, or None.

    Accepts the canonical ``PK-000042`` form and bare digits (``42``);
    anything else is not a packing plan number this service could have
    issued.
    """

    if not isinstance(plan_id, str):
        return None
    text = plan_id.strip()
    if text.startswith(PACKING_PLAN_ID_PREFIX):
        text = text[len(PACKING_PLAN_ID_PREFIX) :]
    if not text.isascii() or not text.isdigit():
        return None
    return int(text)
