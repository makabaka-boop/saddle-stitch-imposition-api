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
default 28-digit context nor a run at the 64-bit storage width can
silently round a snapshot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from .imposition import PAGES_PER_SHEET, validate_total_pages

MIN_PRINT_RUN = 1

# Every sheet count column is a SQLite INTEGER, i.e. a signed 64-bit
# value. Inputs whose snapshot could not be stored are rejected at the
# validation boundary rather than dying mid-persistence with an
# OverflowError. The print run itself is stored verbatim, and the
# (larger) sheet totals derived from it must fit in the same width.
SQLITE_INTEGER_MAX = 2**63 - 1
MAX_PRINT_RUN = SQLITE_INTEGER_MAX
MAX_TOTAL_SHEETS = SQLITE_INTEGER_MAX

# The sheet factors hold at most 19 integer digits; the default Decimal
# context caps the exponent at Emax 999999. A price whose adjusted
# exponent leaves no room for the multiplication (plus rounding carry)
# would make total_sheets * unit_price raise decimal.Overflow, so such
# prices are refused as field errors. 999999 - 19 - 1 (carry) = 999979;
# rounding to 999970 keeps a safety margin.
MAX_UNIT_PRICE_EXPONENT = 999_970

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
    """Raised when ``loss_rate`` is not a non-negative decimal rate, or
    when it pushes the paper total beyond the storage integer width."""


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

    The upper bound is the storage width: the print run is persisted in a
    SQLite INTEGER column (signed 64-bit), so a larger value could never
    be stored and is refused here instead of overflowing on insert.
    """

    # bool check must come before isinstance(int) because bool ⊂ int.
    if isinstance(print_run, bool) or not isinstance(print_run, int):
        raise InvalidPrintRun("print_run must be an integer.")
    if print_run < MIN_PRINT_RUN:
        raise InvalidPrintRun(f"print_run must be at least {MIN_PRINT_RUN}.")
    if print_run > MAX_PRINT_RUN:
        raise InvalidPrintRun(
            f"print_run must be at most {MAX_PRINT_RUN} "
            "(storage integer capacity)."
        )
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
    if price.adjusted() > MAX_UNIT_PRICE_EXPONENT:
        # Such a finite value parses (and even survives a multiplication
        # at a raised precision) but total_sheets * unit_price would
        # overflow the Decimal exponent range (Emax 999999) while the
        # amount is computed. Refuse it on the field instead of failing
        # the money calculation.
        raise InvalidUnitPrice(
            "unit_price exponent is too large to compute a quote amount."
        )
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


def validate_base_sheets(total_pages: int, print_run: int) -> int:
    """Validate the pre-waste sheet count against storage width.

    A print run within its own column can still produce more base sheets
    than a signed 64-bit INTEGER holds (up to 32 sheets/booklet). No loss
    rate can make that storable, so the offending input is the run.

    Raises:
        InvalidPrintRun: when ``(total_pages / 4) * print_run`` exceeds the
            storage integer capacity.
    """

    per_booklet = total_pages // PAGES_PER_SHEET
    base_sheets = per_booklet * print_run
    if base_sheets > MAX_TOTAL_SHEETS:
        raise InvalidPrintRun(
            f"print_run must keep base sheets at most {MAX_TOTAL_SHEETS} "
            "(storage integer capacity); "
            f"{per_booklet} sheets/booklet times {print_run} booklets "
            f"is {base_sheets}."
        )
    return base_sheets


def validate_loss_rate_capacity(loss_rate: Decimal) -> None:
    """Reject rates too large to keep any positive sheet total storable.

    A rate at/above 10**19 applied to any positive base yields more
    sheets than the 19-digit INTEGER column could ever hold; checking
    the exponent also avoids a product beyond Decimal Emax (which would
    raise decimal.Overflow instead of a field error).
    """

    max_sheet_exponent = Decimal(MAX_TOTAL_SHEETS).adjusted()
    if loss_rate > 0 and loss_rate.adjusted() > max_sheet_exponent:
        raise InvalidLossRate(
            "loss_rate must keep total sheets at most "
            f"{MAX_TOTAL_SHEETS} (storage integer capacity)."
        )


def validate_loss_sheets(base_sheets: int, loss_rate: Decimal) -> int:
    """Validate the waste-inclusive sheet total against storage width.

    A positive rate can push the ceiled total past the signed 64-bit
    limit even though the base count and the run fit. The check runs
    before the snapshot is computed or written, and the offending input
    is the rate. math.ceil on a Decimal is exact regardless of context.

    Raises:
        InvalidLossRate: when the loss rate drives the total sheet count
            beyond the storage integer capacity, or is itself so large
            the waste product would overflow the Decimal exponent range.
    """

    validate_loss_rate_capacity(loss_rate)
    loss_sheets = math.ceil(_exact_product(base_sheets, loss_rate))
    if base_sheets + loss_sheets > MAX_TOTAL_SHEETS:
        raise InvalidLossRate(
            "loss_rate must keep total sheets at most "
            f"{MAX_TOTAL_SHEETS} (storage integer capacity)."
        )
    return loss_sheets


def validate_snapshot_capacity(
    total_pages: int, print_run: int, loss_rate: Decimal
) -> int:
    """Validate every sheet column of a snapshot; return the loss count.

    Raises:
        InvalidPrintRun: when the base sheet count already exceeds the
            storage capacity.
        InvalidLossRate: when the loss rate drives the total beyond it.
    """

    base_sheets = validate_base_sheets(total_pages, print_run)
    return validate_loss_sheets(base_sheets, loss_rate)


def _exact_product(factor: int, multiplier: Decimal) -> Decimal:
    """Multiply an int by a Decimal without context-precision rounding.

    The default 28-digit context would silently round products with more
    than 28 significant digits (a run at the 19-digit storage width
    multiplied by a high-precision rate reaches that easily), so the
    multiplication runs in a context sized to hold every significant
    digit of both operands.
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
        InvalidPrintRun: if the print run is not a positive integer that
            keeps the base sheet count within storage.
        InvalidUnitPrice: if the price is a float, bool, negative, not a
            decimal number, or too large to compute the amount.
        InvalidLossRate: if the rate is negative/non-numeric or pushes
            the total sheet count beyond storage.
    """

    pages = validate_total_pages(total_pages)
    run = validate_print_run(print_run)
    price = validate_unit_price(unit_price)
    rate = validate_loss_rate(loss_rate)

    per_booklet = pages // PAGES_PER_SHEET
    base = per_booklet * run
    # Refuse sheet totals the INTEGER columns cannot hold *before* any
    # amount is computed or a row written; this also computes the ceiled
    # loss count reused below.
    loss = validate_snapshot_capacity(pages, run, rate)
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


def _money_difference(baseline_amount: Decimal, candidate_amount: Decimal) -> Decimal:
    """Subtract two two-place money decimals without losing low digits.

    Under the default 28-digit context, subtracting operands whose digit
    counts differ by more than the context precision (e.g. a quote priced
    at 1E30 against one priced at 0.01) silently discards the smaller
    operand's low-order digits. The subtraction therefore runs in a
    context wide enough to hold every significant digit of both operands
    plus the two money places, and the result is re-quantized to two
    places so the difference is always complete money.
    """

    digits_needed = (
        max(baseline_amount.adjusted(), candidate_amount.adjusted(), 0) + 3
    )
    with localcontext() as context:
        context.prec = max(28, digits_needed)
        difference = baseline_amount - candidate_amount
        # Re-quantize inside the wide context; both inputs already carry
        # two places so this only normalizes the two-place money form.
        return difference.quantize(AMOUNT_PLACES, rounding=ROUND_HALF_UP)


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
    # Size the subtraction context to the operands: the default 28-digit
    # context would drop the low places of the smaller money amount when
    # the two amounts differ by more than ~28 digits of magnitude.
    amount_difference = _money_difference(
        baseline.snapshot.total_amount, candidate.snapshot.total_amount
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
