"""Pure paper-cost quoting core: validation and snapshot computation.

This module contains no web framework or database code so the pricing
rules can be unit tested and reused independently.

A quote prices the *paper* for one print run of a saddle-stitched
booklet before imposition:

    sheets_per_booklet = total_pages // PAGES_PER_SHEET   (4 pages/sheet)
    base_sheets        = sheets_per_booklet * print_run
    loss_sheets        = ceil(base_sheets * loss_rate)    (rounded up)
    total_sheets       = base_sheets + loss_sheets
    total_amount       = total_sheets * unit_price        (Decimal, 2 places)

Money is handled with :class:`decimal.Decimal`; the persisted amount is
quantized to two decimal places (ROUND_HALF_UP) so the stored snapshot
matches what the order desk recomputes with a calculator. All decimal
arithmetic runs in a precision sized to the operands so neither the
default 28-digit context nor huge print runs can silently round a
snapshot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from .imposition import PAGES_PER_SHEET, validate_total_pages

MIN_PRINT_RUN = 1

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
QUOTE_STATUSES = (STATUS_PENDING, STATUS_CONFIRMED)

QUOTE_ID_PREFIX = "Q-"

# Amounts are money: exactly two fraction places, half-up rounding.
AMOUNT_PLACES = Decimal("0.01")


class InvalidPrintRun(ValueError):
    """Raised when ``print_run`` is not a strict positive integer."""


class InvalidUnitPrice(ValueError):
    """Raised when ``unit_price`` is not an exact non-negative decimal."""


class InvalidLossRate(ValueError):
    """Raised when ``loss_rate`` is not a non-negative decimal rate."""


class IncompatibleQuotes(ValueError):
    """Raised when two quotes do not describe the same print job.

    Only quotes with the same booklet page count and the same print run
    can be compared: the sheet/amount difference would otherwise mix two
    different jobs instead of two pricing alternatives for one job.
    """


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    """Immutable pricing facts persisted with a quote.

    The snapshot is computed once, at creation time, and is never
    rewritten afterwards — confirming a quote only flips its status.
    """

    total_pages: int
    print_run: int
    unit_price: Decimal
    loss_rate: Decimal
    sheets_per_booklet: int
    base_sheets: int
    loss_sheets: int
    total_sheets: int
    total_amount: Decimal


@dataclass(frozen=True, slots=True)
class Quote:
    """A persisted quote: the snapshot plus identity and lifecycle state.

    ``status`` is :data:`STATUS_PENDING` or :data:`STATUS_CONFIRMED`;
    ``confirmed_at`` is set exactly when the status becomes confirmed.
    """

    quote_id: str
    snapshot: QuoteSnapshot
    status: str
    created_at: str
    confirmed_at: str | None


def validate_print_run(print_run: object) -> int:
    """Return ``print_run`` as an int or raise InvalidPrintRun.

    ``bool`` is rejected explicitly even though it subclasses ``int``.
    """

    # bool check must come before isinstance(int) because bool ⊂ int.
    if isinstance(print_run, bool) or not isinstance(print_run, int):
        raise InvalidPrintRun("print_run must be an integer.")
    if print_run < MIN_PRINT_RUN:
        raise InvalidPrintRun(f"print_run must be at least {MIN_PRINT_RUN}.")
    return print_run


def validate_unit_price(unit_price: object) -> Decimal:
    """Return ``unit_price`` as an exact Decimal or raise InvalidUnitPrice.

    Floats are rejected on purpose: a binary float cannot represent most
    decimal prices exactly, so the price must arrive as a decimal string
    (e.g. ``"1.25"``) or an integer. ``bool`` is rejected even though it
    subclasses ``int``.
    """

    # bool check must come before isinstance(int) because bool ⊂ int.
    if isinstance(unit_price, bool):
        raise InvalidUnitPrice(
            "unit_price must be a decimal string or an integer."
        )
    if isinstance(unit_price, float):
        raise InvalidUnitPrice(
            "unit_price must be a decimal string or an integer, not a float."
        )
    if isinstance(unit_price, Decimal):
        price = unit_price
    elif isinstance(unit_price, int):
        price = Decimal(unit_price)
    elif isinstance(unit_price, str):
        try:
            price = Decimal(unit_price.strip())
        except InvalidOperation:
            raise InvalidUnitPrice(
                "unit_price must be a valid decimal number."
            ) from None
    else:
        raise InvalidUnitPrice(
            "unit_price must be a decimal string or an integer."
        )
    if not price.is_finite():
        raise InvalidUnitPrice("unit_price must be a finite decimal number.")
    if price < 0:
        raise InvalidUnitPrice("unit_price must not be negative.")
    return price


def validate_loss_rate(loss_rate: object) -> Decimal:
    """Return ``loss_rate`` as an exact Decimal or raise InvalidLossRate.

    Unlike the unit price, a JSON number is accepted here; it is
    converted through ``str`` so ``0.05`` means exactly 0.05, not the
    nearest binary float. ``bool`` is rejected even though it subclasses
    ``int``.
    """

    # bool check must come before isinstance(int) because bool ⊂ int.
    if isinstance(loss_rate, bool):
        raise InvalidLossRate("loss_rate must be a number.")
    if isinstance(loss_rate, Decimal):
        rate = loss_rate
    elif isinstance(loss_rate, (int, float)):
        rate = Decimal(str(loss_rate))
    elif isinstance(loss_rate, str):
        try:
            rate = Decimal(loss_rate.strip())
        except InvalidOperation:
            raise InvalidLossRate(
                "loss_rate must be a valid decimal number."
            ) from None
    else:
        raise InvalidLossRate("loss_rate must be a number.")
    if not rate.is_finite():
        raise InvalidLossRate("loss_rate must be finite.")
    if rate < 0:
        raise InvalidLossRate("loss_rate must not be negative.")
    return rate


def _exact_product(factor: int, multiplier: Decimal) -> Decimal:
    """Multiply an int by a Decimal without context-precision rounding.

    The default 28-digit context would silently round products with more
    than 28 significant digits (possible because the print run is
    unbounded), so the multiplication runs in a context sized to hold
    every significant digit of both operands.
    """

    precision = len(str(factor)) + len(multiplier.as_tuple().digits)
    with localcontext() as context:
        context.prec = max(28, precision)
        return Decimal(factor) * multiplier


def _quantize_amount(amount: Decimal) -> Decimal:
    """Round to two money places (half-up) at whatever precision fits.

    The context must hold every integer digit plus the two fraction
    places plus one digit of rounding headroom, otherwise quantize
    raises InvalidOperation for very large amounts.
    """

    # Two fraction places plus two digits of headroom: rounding up can
    # carry into a new integer digit (99.999 -> 100.00).
    integer_digits = max(amount.adjusted(), 0) + 1
    with localcontext() as context:
        context.prec = integer_digits + 4
        return amount.quantize(AMOUNT_PLACES, rounding=ROUND_HALF_UP)


def build_snapshot(
    total_pages: object,
    print_run: object,
    unit_price: object,
    loss_rate: object,
) -> QuoteSnapshot:
    """Compute the immutable pricing snapshot for one quote.

    Raises:
        InvalidTotalPages: if the page count is not a valid booklet size.
        InvalidPrintRun: if the print run is not a positive integer.
        InvalidUnitPrice: if the price is a float, bool, negative or not
            a decimal number.
        InvalidLossRate: if the loss rate is negative or not a number.
    """

    pages = validate_total_pages(total_pages)
    run = validate_print_run(print_run)
    price = validate_unit_price(unit_price)
    rate = validate_loss_rate(loss_rate)

    per_booklet = pages // PAGES_PER_SHEET
    base = per_booklet * run
    # The loss is rounded up to a whole sheet: partial sheets are bought
    # whole, and math.ceil on a Decimal is exact regardless of context.
    loss = math.ceil(_exact_product(base, rate))
    total = base + loss
    amount = _quantize_amount(_exact_product(total, price))
    return QuoteSnapshot(
        total_pages=pages,
        print_run=run,
        unit_price=price,
        loss_rate=rate,
        sheets_per_booklet=per_booklet,
        base_sheets=base,
        loss_sheets=loss,
        total_sheets=total,
        total_amount=amount,
    )


def format_quote_id(row_id: int) -> str:
    """Canonical public quote number, e.g. ``Q-000042``."""

    return f"{QUOTE_ID_PREFIX}{row_id:06d}"


def parse_quote_id(quote_id: object) -> int | None:
    """Parse a public quote number back to its row id, or return None.

    Accepts the canonical ``Q-000042`` form and bare digits (``42``);
    anything else is not a quote number this service could have issued.
    """

    if not isinstance(quote_id, str):
        return None
    text = quote_id.strip()
    if text.startswith(QUOTE_ID_PREFIX):
        text = text[len(QUOTE_ID_PREFIX) :]
    if not text.isascii() or not text.isdigit():
        return None
    return int(text)


@dataclass(frozen=True, slots=True)
class QuoteComparison:
    """The signed difference between two persisted quote snapshots.

    ``sheet_difference`` and ``amount_difference`` are
    baseline-minus-candidate, so swapping the two inputs reverses both
    signs. ``lower_quote_id`` names the cheaper quote, or is ``None``
    when the amounts tie.
    """

    baseline_quote_id: str
    candidate_quote_id: str
    sheet_difference: int
    amount_difference: Decimal
    lower_quote_id: str | None


def compare_quotes(baseline: Quote, candidate: Quote) -> QuoteComparison:
    """Diff two quotes' paper totals and amounts.

    Both snapshots must price the same job — identical booklet page
    count and identical print run — even though their paper price or
    loss alternatives may differ. The persisted two-place amount
    snapshots are subtracted directly, so the difference itself stays an
    exact money decimal with two fraction places.

    Raises:
        IncompatibleQuotes: if the page counts or print runs differ.
    """

    if (
        baseline.snapshot.total_pages != candidate.snapshot.total_pages
        or baseline.snapshot.print_run != candidate.snapshot.print_run
    ):
        raise IncompatibleQuotes(
            "Quotes "
            f"{baseline.quote_id!r} ({baseline.snapshot.total_pages} pages, "
            f"{baseline.snapshot.print_run} booklets) and "
            f"{candidate.quote_id!r} ({candidate.snapshot.total_pages} pages, "
            f"{candidate.snapshot.print_run} booklets) price different jobs; "
            "only quotes with the same total_pages and print_run are "
            "comparable."
        )

    sheet_difference = (
        baseline.snapshot.total_sheets - candidate.snapshot.total_sheets
    )
    amount_difference = (
        baseline.snapshot.total_amount - candidate.snapshot.total_amount
    )
    # amount_difference is baseline minus candidate: negative means the
    # baseline is cheaper, positive that the candidate is.
    if amount_difference < 0:
        lower_quote_id = baseline.quote_id
    elif amount_difference > 0:
        lower_quote_id = candidate.quote_id
    else:
        lower_quote_id = None
    return QuoteComparison(
        baseline_quote_id=baseline.quote_id,
        candidate_quote_id=candidate.quote_id,
        sheet_difference=sheet_difference,
        amount_difference=amount_difference,
        lower_quote_id=lower_quote_id,
    )


def decimal_to_str(value: Decimal) -> str:
    """Render a Decimal without scientific notation (``5250.00``)."""

    return format(value, "f")
