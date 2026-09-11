"""Pure imposition core: build saddle-stitched sheet layout.

This module contains no web framework code so the algorithm can be unit
tested and reused independently.

Pages are numbered from 1 (one-based). Sheets are numbered i = 0, 1, ...
from the outermost sheet toward the innermost sheet. For sheet i:

    front (the sheet side seen first when opening the folded sheet),
    read left to right: [total_pages - 2*i, 1 + 2*i]
    back, read left to right:  [2 + 2*i, total_pages - 1 - 2*i]

Pages are never rotated and no blank pages are appended; therefore
``total_pages`` must already be a multiple of 4.

:func:`locate_page` answers the single-page question ("which sheet, side
and slot is page *n* printed on, and which page shares that side?") by
scanning the arrangement produced by :func:`impose`, so the plate maker
does not have to walk the whole nested sheet stack by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_PAGES = 4
MAX_PAGES = 128
PAGES_PER_SHEET = 4

SIDE_FRONT = "front"
SIDE_BACK = "back"
POSITION_LEFT = "left"
POSITION_RIGHT = "right"


class InvalidTotalPages(ValueError):
    """Raised when ``total_pages`` cannot describe a valid booklet."""


class InvalidPageNumber(ValueError):
    """Raised when ``page_number`` is not a strict int in 1..total_pages."""


@dataclass(frozen=True, slots=True)
class Sheet:
    """A single folded sheet.

    ``index`` is the sheet number from the outside (0 = outermost).
    ``front`` and ``back`` list page numbers from left to right.
    """

    index: int
    front: tuple[int, int]
    back: tuple[int, int]


@dataclass(frozen=True, slots=True)
class Imposition:
    """Full imposition result for one booklet."""

    total_pages: int
    sheets: tuple[Sheet, ...]

    @property
    def sheet_count(self) -> int:
        return len(self.sheets)


@dataclass(frozen=True, slots=True)
class PageLocation:
    """Unique location of one page within an imposed booklet.

    ``side`` is :data:`SIDE_FRONT` or :data:`SIDE_BACK`; ``position`` is
    :data:`POSITION_LEFT` or :data:`POSITION_RIGHT` on that side.
    ``partner_page`` is the other page on the same side, read left to right.
    """

    page_number: int
    sheet_index: int
    side: str
    position: str
    partner_page: int


def validate_total_pages(total_pages: object) -> int:
    """Return ``total_pages`` as an int or raise InvalidTotalPages.

    ``bool`` is rejected explicitly even though it subclasses ``int``.
    """

    # bool check must come before isinstance(int) because bool ⊂ int.
    if isinstance(total_pages, bool) or not isinstance(total_pages, int):
        raise InvalidTotalPages("total_pages must be an integer.")
    if total_pages < MIN_PAGES or total_pages > MAX_PAGES:
        raise InvalidTotalPages(
            f"total_pages must be between {MIN_PAGES} and {MAX_PAGES}."
        )
    if total_pages % PAGES_PER_SHEET != 0:
        raise InvalidTotalPages(
            f"total_pages must be divisible by {PAGES_PER_SHEET}."
        )
    return total_pages


def impose(total_pages: int) -> Imposition:
    """Build the complete outside-in sheet sequence.

    Raises:
        InvalidTotalPages: if the page count is not an integer between 4
            and 128 inclusive, or is not a multiple of 4.
    """

    pages = validate_total_pages(total_pages)
    sheet_count = pages // PAGES_PER_SHEET
    sheets = tuple(
        Sheet(
            index=i,
            front=(pages - 2 * i, 1 + 2 * i),
            back=(2 + 2 * i, pages - 1 - 2 * i),
        )
        for i in range(sheet_count)
    )
    return Imposition(total_pages=pages, sheets=sheets)


def validate_page_number(page_number: object, total_pages: int) -> int:
    """Return ``page_number`` as an int or raise InvalidPageNumber.

    Like total pages, ``bool`` is rejected even though it subclasses ``int``.
    The page must lie in the one-based range 1..total_pages.
    """

    # bool check must come before isinstance(int) because bool ⊂ int.
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise InvalidPageNumber("page_number must be an integer.")
    if page_number < 1 or page_number > total_pages:
        raise InvalidPageNumber(
            f"page_number must be between 1 and {total_pages}."
        )
    return page_number


def locate_page(booklet: Imposition, page_number: object) -> PageLocation:
    """Locate one page inside an already-built imposition.

    The unique location is read off the existing arrangement rather than
    derived from a parallel formula: the sheets' left-to-right slots are the
    single source of truth, so the result can never disagree with
    :func:`impose`. Exactly one slot matches because every page 1..total_pages
    appears exactly once.

    Raises:
        InvalidPageNumber: if ``page_number`` is not a strict integer in
            1..booklet.total_pages.
    """

    page = validate_page_number(page_number, booklet.total_pages)
    for sheet in booklet.sheets:
        for side, slots in (
            (SIDE_FRONT, sheet.front),
            (SIDE_BACK, sheet.back),
        ):
            for position, slot in (
                (POSITION_LEFT, slots[0]),
                (POSITION_RIGHT, slots[1]),
            ):
                if slot == page:
                    return PageLocation(
                        page_number=page,
                        sheet_index=sheet.index,
                        side=side,
                        position=position,
                        partner_page=slots[1]
                        if position == POSITION_LEFT
                        else slots[0],
                    )
    # Unreachable for an Imposition produced by impose(); kept defensive so
    # a hand-built object raises instead of returning None.
    raise InvalidPageNumber(
        f"page_number must be between 1 and {booklet.total_pages}."
    )
