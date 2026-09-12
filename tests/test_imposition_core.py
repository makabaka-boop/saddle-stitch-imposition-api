"""Invariant tests for the pure imposition algorithm.

These do not touch FastAPI: they pin down the mathematical contract that
every page appears exactly once and the exact outside-in signature is
produced, for every legal input 4..128 divisible by 4.
"""

from __future__ import annotations

import pytest

from app.imposition import (
    InvalidPageNumber,
    InvalidTotalPages,
    PAGES_PER_SHEET,
    PageLocation,
    Sheet,
    impose,
    locate_page,
    validate_page_number,
    validate_page_number_lower_bound,
    validate_total_pages,
)

VALID_TOTALS = list(range(4, 128 + 1, 4))


def _all_pages(result) -> list[int]:
    pages: list[int] = []
    for sheet in result.sheets:
        pages.extend(sheet.front)
        pages.extend(sheet.back)
    return pages


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_every_page_appears_exactly_once(total_pages: int) -> None:
    result = impose(total_pages)
    pages = _all_pages(result)

    # No duplicate, no missing page: multiset equals {1..total_pages}.
    assert sorted(pages) == list(range(1, total_pages + 1))
    assert len(pages) == len(set(pages)) == total_pages


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_sheet_count_and_indices(total_pages: int) -> None:
    result = impose(total_pages)

    assert result.sheet_count == total_pages // PAGES_PER_SHEET
    assert [sheet.index for sheet in result.sheets] == list(
        range(result.sheet_count)
    )
    assert len(result.sheets) == result.sheet_count


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_sheets_match_declared_formula(total_pages: int) -> None:
    result = impose(total_pages)

    for i, sheet in enumerate(result.sheets):
        assert isinstance(sheet, Sheet)
        assert sheet.index == i
        assert sheet.front == (total_pages - 2 * i, 1 + 2 * i)
        assert sheet.back == (2 + 2 * i, total_pages - 1 - 2 * i)


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_all_page_numbers_are_one_based_ints_in_range(total_pages: int) -> None:
    result = impose(total_pages)
    for sheet in result.sheets:
        for page in (*sheet.front, *sheet.back):
            assert isinstance(page, int)
            assert not isinstance(page, bool)
            assert 1 <= page <= total_pages


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_front_left_is_even_front_right_is_odd(total_pages: int) -> None:
    """Reading a folded sheet left-to-right, the left page is even
    (verso) and the right page is odd (recto) on both sides — the visual
    signature the plate maker verifies."""
    result = impose(total_pages)
    for sheet in result.sheets:
        assert sheet.front[0] % 2 == 0
        assert sheet.front[1] % 2 == 1
        assert sheet.back[0] % 2 == 0
        assert sheet.back[1] % 2 == 1


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_outermost_sheet_contains_first_and_last_pages(total_pages: int) -> None:
    result = impose(total_pages)
    outer = result.sheets[0]
    assert outer.front == (total_pages, 1)
    assert outer.back == (2, total_pages - 1)


def test_known_16_page_signature() -> None:
    # Hand-checkable reference signature.
    result = impose(16)
    assert [sheet.front for sheet in result.sheets] == [
        (16, 1),
        (14, 3),
        (12, 5),
        (10, 7),
    ]
    assert [sheet.back for sheet in result.sheets] == [
        (2, 15),
        (4, 13),
        (6, 11),
        (8, 9),
    ]


def test_known_8_page_signature() -> None:
    result = impose(8)
    assert result.sheet_count == 2
    assert result.sheets[0].front == (8, 1)
    assert result.sheets[0].back == (2, 7)
    assert result.sheets[1].front == (6, 3)
    assert result.sheets[1].back == (4, 5)


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_deterministic_across_calls(total_pages: int) -> None:
    first = impose(total_pages)
    second = impose(total_pages)
    assert first == second


@pytest.mark.parametrize("value", [0, 1, 2, 3, -4, -8, 129, 132, 1000])
def test_rejects_out_of_range_integers(value: int) -> None:
    with pytest.raises(InvalidTotalPages, match="between 4 and 128"):
        validate_total_pages(value)
    with pytest.raises(InvalidTotalPages):
        impose(value)


@pytest.mark.parametrize("value", [5, 6, 7, 9, 10, 11, 13, 17, 30, 126, 127])
def test_rejects_non_multiple_of_four(value: int) -> None:
    # Note: 126/127 are also multiples failures within range or range
    # boundary cases; each must be rejected without partial output.
    with pytest.raises(InvalidTotalPages):
        validate_total_pages(value)


def test_error_order_range_before_divisibility() -> None:
    # 2 is below the minimum; the range message takes priority.
    with pytest.raises(InvalidTotalPages, match="between 4 and 128"):
        validate_total_pages(2)
    # 130 is in-range-of-divisibility? no, it is above max, range wins.
    with pytest.raises(InvalidTotalPages, match="between 4 and 128"):
        validate_total_pages(130)
    # 6 is inside the range: divisibility error.
    with pytest.raises(InvalidTotalPages, match="divisible by 4"):
        validate_total_pages(6)


@pytest.mark.parametrize("value", ["8", 8.0, 8.5, None, True, False, [8], {}, object()])
def test_rejects_non_integer_types(value: object) -> None:
    with pytest.raises(InvalidTotalPages, match="integer"):
        validate_total_pages(value)


def test_boundary_values_accepted() -> None:
    assert validate_total_pages(4) == 4
    assert validate_total_pages(128) == 128
    assert impose(4).sheets[0].front == (4, 1)
    assert impose(4).sheets[0].back == (2, 3)
    assert impose(128).sheet_count == 32
    assert impose(128).sheets[-1] == Sheet(
        index=31, front=(66, 63), back=(64, 65)
    )


# ---------------------------------------------------------------------------
# locate_page: single-page positioning is read off the existing arrangement.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_locate_agrees_with_every_slot_of_the_imposition(total_pages: int) -> None:
    booklet = impose(total_pages)
    for sheet in booklet.sheets:
        expected = {
            sheet.front[0]: (sheet.index, "front", "left", sheet.front[1]),
            sheet.front[1]: (sheet.index, "front", "right", sheet.front[0]),
            sheet.back[0]: (sheet.index, "back", "left", sheet.back[1]),
            sheet.back[1]: (sheet.index, "back", "right", sheet.back[0]),
        }
        for page, (index, side, position, partner) in expected.items():
            location = locate_page(booklet, page)
            assert isinstance(location, PageLocation)
            assert location.page_number == page
            assert location.sheet_index == index
            assert location.side == side
            assert location.position == position
            assert location.partner_page == partner


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_locate_partner_is_the_other_page_on_the_same_side(
    total_pages: int,
) -> None:
    booklet = impose(total_pages)
    for page in range(1, total_pages + 1):
        location = locate_page(booklet, page)
        sheet = booklet.sheets[location.sheet_index]
        slots = sheet.front if location.side == "front" else sheet.back
        assert page in slots and location.partner_page in slots
        assert location.partner_page != page
        # Partner relation is symmetric: the partner locates back to us.
        partner_location = locate_page(booklet, location.partner_page)
        assert (
            partner_location.sheet_index == location.sheet_index
            and partner_location.side == location.side
            and partner_location.partner_page == page
        )


def test_locate_sixteen_pages_acceptance_locations() -> None:
    # Page 1: outermost sheet (index 0), front side, right slot, paired 16.
    assert locate_page(impose(16), 1) == PageLocation(
        page_number=1,
        sheet_index=0,
        side="front",
        position="right",
        partner_page=16,
    )
    # Page 8: innermost sheet (index 3), back side, left slot, paired 9.
    assert locate_page(impose(16), 8) == PageLocation(
        page_number=8,
        sheet_index=3,
        side="back",
        position="left",
        partner_page=9,
    )


def test_locate_four_page_boundary_booklet() -> None:
    booklet = impose(4)
    assert locate_page(booklet, 1) == PageLocation(1, 0, "front", "right", 4)
    assert locate_page(booklet, 2) == PageLocation(2, 0, "back", "left", 3)
    assert locate_page(booklet, 3) == PageLocation(3, 0, "back", "right", 2)
    assert locate_page(booklet, 4) == PageLocation(4, 0, "front", "left", 1)


def test_locate_128_page_boundary_pages() -> None:
    booklet = impose(128)
    # First page: outer front right, paired with the last page.
    assert locate_page(booklet, 1) == PageLocation(1, 0, "front", "right", 128)
    # Last page: outer front left.
    assert locate_page(booklet, 128) == PageLocation(128, 0, "front", "left", 1)
    # Innermost sheet (index 31): front [66, 63], back [64, 65].
    assert locate_page(booklet, 64) == PageLocation(64, 31, "back", "left", 65)
    assert locate_page(booklet, 65) == PageLocation(65, 31, "back", "right", 64)
    assert locate_page(booklet, 63) == PageLocation(63, 31, "front", "right", 66)


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_locate_is_unique_and_deterministic(total_pages: int) -> None:
    booklet = impose(total_pages)
    seen: set[tuple[int, str, str]] = set()
    for page in range(1, total_pages + 1):
        first = locate_page(booklet, page)
        second = locate_page(booklet, page)
        assert first == second
        triple = (first.sheet_index, first.side, first.position)
        assert triple not in seen
        seen.add(triple)
    # Every physical slot of every sheet is occupied exactly once.
    assert len(seen) == total_pages


@pytest.mark.parametrize("value", [0, -1, 17, 100, 129])
def test_validate_page_number_rejects_out_of_range(value: int) -> None:
    with pytest.raises(InvalidPageNumber, match="between 1 and 16"):
        validate_page_number(value, 16)
    with pytest.raises(InvalidPageNumber):
        locate_page(impose(16), value)


def test_validate_page_number_rejects_above_small_booklet_bound() -> None:
    with pytest.raises(InvalidPageNumber, match="between 1 and 4"):
        validate_page_number(5, 4)
    with pytest.raises(InvalidPageNumber):
        locate_page(impose(4), 5)


@pytest.mark.parametrize("value", [1, 8, 16])
def test_validate_page_number_accepts_endpoints(value: int) -> None:
    assert validate_page_number(value, 16) == value


@pytest.mark.parametrize("value", ["8", 8.0, 8.5, None, True, False, [8], {}, object()])
def test_validate_page_number_rejects_non_integer_types(value: object) -> None:
    with pytest.raises(InvalidPageNumber, match="integer"):
        validate_page_number(value, 16)
    with pytest.raises(InvalidPageNumber):
        locate_page(impose(16), value)


def test_locate_range_bound_tracks_total_pages() -> None:
    # 16 is valid in a 16-page booklet but out of range in a 4-page one.
    assert locate_page(impose(16), 16).partner_page == 1
    with pytest.raises(InvalidPageNumber, match="between 1 and 4"):
        locate_page(impose(4), 16)


# ---------------------------------------------------------------------------
# validate_page_number_lower_bound: the checks that hold without a valid
# total_pages (used when the booklet size itself failed validation).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 2, 100, 128, 1000])
def test_lower_bound_accepts_one_based_ints(value: int) -> None:
    # Any int >= 1 passes: the upper bound is unknowable without a valid
    # total_pages, so even 1000 is not rejected here.
    assert validate_page_number_lower_bound(value) == value


@pytest.mark.parametrize("value", [0, -1, -100])
def test_lower_bound_rejects_below_one(value: int) -> None:
    with pytest.raises(InvalidPageNumber, match="at least 1"):
        validate_page_number_lower_bound(value)


@pytest.mark.parametrize("value", ["8", 8.0, 8.5, None, True, False, [8], {}, object()])
def test_lower_bound_rejects_non_integer_types(value: object) -> None:
    with pytest.raises(InvalidPageNumber, match="integer"):
        validate_page_number_lower_bound(value)
