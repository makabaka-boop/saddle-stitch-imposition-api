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
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_PAGES = 4
MAX_PAGES = 128
PAGES_PER_SHEET = 4


class InvalidTotalPages(ValueError):
    """Raised when ``total_pages`` cannot describe a valid booklet."""


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
