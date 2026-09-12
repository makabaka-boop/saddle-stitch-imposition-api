"""HTTP-level tests for POST /imposition/locate.

Pins the single-page positioning contract: sheet index, side, left/right
slot and same-side partner, plus field-level 422 semantics that never leak
a partial location.
"""

from __future__ import annotations

import pytest

# Every legal booklet size; the location must stay self-consistent for all
# pages of each booklet while keeping the HTTP suite fast.
VALID_TOTALS = list(range(4, 128 + 1, 4))


# ---------------------------------------------------------------------------
# Acceptance cases from the plate-maker workflow.
# ---------------------------------------------------------------------------


def test_sixteen_pages_page_one_is_outer_front_right(client) -> None:
    response = client.post(
        "/imposition/locate", json={"total_pages": 16, "page_number": 1}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "total_pages": 16,
        "page_number": 1,
        "sheet_index": 0,
        "side": "front",
        "position": "right",
        "partner_page": 16,
    }


def test_sixteen_pages_page_eight_is_inner_back_left(client) -> None:
    response = client.post(
        "/imposition/locate", json={"total_pages": 16, "page_number": 8}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "total_pages": 16,
        "page_number": 8,
        "sheet_index": 3,
        "side": "back",
        "position": "left",
        "partner_page": 9,
    }


def test_four_page_booklet_boundary_pages(client) -> None:
    first = client.post(
        "/imposition/locate", json={"total_pages": 4, "page_number": 1}
    )
    assert first.status_code == 200, first.text
    assert first.json() == {
        "total_pages": 4,
        "page_number": 1,
        "sheet_index": 0,
        "side": "front",
        "position": "right",
        "partner_page": 4,
    }

    last = client.post(
        "/imposition/locate", json={"total_pages": 4, "page_number": 4}
    )
    assert last.status_code == 200, last.text
    assert last.json() == {
        "total_pages": 4,
        "page_number": 4,
        "sheet_index": 0,
        "side": "front",
        "position": "left",
        "partner_page": 1,
    }


def test_128_page_booklet_boundary_pages(client) -> None:
    first = client.post(
        "/imposition/locate", json={"total_pages": 128, "page_number": 1}
    )
    assert first.status_code == 200, first.text
    assert first.json() == {
        "total_pages": 128,
        "page_number": 1,
        "sheet_index": 0,
        "side": "front",
        "position": "right",
        "partner_page": 128,
    }

    last = client.post(
        "/imposition/locate", json={"total_pages": 128, "page_number": 128}
    )
    assert last.status_code == 200, last.text
    assert last.json() == {
        "total_pages": 128,
        "page_number": 128,
        "sheet_index": 0,
        "side": "front",
        "position": "left",
        "partner_page": 1,
    }


def test_range_endpoints_accepted(client) -> None:
    for page_number in (1, 16):
        response = client.post(
            "/imposition/locate",
            json={"total_pages": 16, "page_number": page_number},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["page_number"] == page_number


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_location_matches_full_imposition_for_every_page(
    client, total_pages: int
) -> None:
    full = client.post("/imposition", json={"total_pages": total_pages}).json()
    slots: dict[int, tuple[int, str, str, int]] = {}
    for sheet in full["sheets"]:
        slots[sheet["front"][0]] = (
            sheet["index"], "front", "left", sheet["front"][1]
        )
        slots[sheet["front"][1]] = (
            sheet["index"], "front", "right", sheet["front"][0]
        )
        slots[sheet["back"][0]] = (
            sheet["index"], "back", "left", sheet["back"][1]
        )
        slots[sheet["back"][1]] = (
            sheet["index"], "back", "right", sheet["back"][0]
        )

    for page in range(1, total_pages + 1):
        response = client.post(
            "/imposition/locate",
            json={"total_pages": total_pages, "page_number": page},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {
            "total_pages",
            "page_number",
            "sheet_index",
            "side",
            "position",
            "partner_page",
        }
        assert body["total_pages"] == total_pages
        index, side, position, partner = slots[page]
        assert body["sheet_index"] == index
        assert body["side"] == side
        assert body["position"] == position
        assert body["partner_page"] == partner


def test_openapi_documents_locate_contract(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/imposition/locate" in schema["paths"]
    locate_schema = schema["components"]["schemas"]["LocateResponse"]
    assert set(locate_schema["properties"]) == {
        "total_pages",
        "page_number",
        "sheet_index",
        "side",
        "position",
        "partner_page",
    }
    # side/position are closed enums, not free-form strings.
    assert locate_schema["properties"]["side"]["enum"] == ["front", "back"]
    assert locate_schema["properties"]["position"]["enum"] == ["left", "right"]
    request_props = schema["components"]["schemas"]["LocateRequest"]["properties"]
    assert "total_pages" in request_props
    assert "page_number" in request_props


def test_openapi_locate_request_exposes_structured_constraints(client) -> None:
    # Generated clients must see the whole valid envelope up front, not
    # just "integer": an 18-page count or page 0 must look invalid in the
    # contract itself, matching what the endpoint enforces.
    props = client.get("/openapi.json").json()["components"]["schemas"][
        "LocateRequest"
    ]["properties"]
    total = props["total_pages"]
    assert total["type"] == "integer"
    assert total["minimum"] == 4
    assert total["maximum"] == 128
    assert total["multipleOf"] == 4
    # Page numbers are one-based. The upper bound is per-request
    # (total_pages), so it stays in the description, not the schema.
    page = props["page_number"]
    assert page["type"] == "integer"
    assert page["minimum"] == 1


# ---------------------------------------------------------------------------
# 422 field-level errors: every failure points at the offending field and
# never includes a partial location.
# ---------------------------------------------------------------------------


def _assert_page_number_error(response) -> dict:
    assert response.status_code == 422, response.text
    body = response.json()
    assert "detail" in body and isinstance(body["detail"], list)
    assert body["detail"], "expected at least one error"
    for error in body["detail"]:
        assert error["loc"][-1] == "page_number"
        assert error["msg"]
    # No partial location may ride along with the error envelope.
    for key in (
        "sheet_index",
        "side",
        "position",
        "partner_page",
        "sheets",
        "sheet_count",
    ):
        assert key not in body
    return body


@pytest.mark.parametrize("value", [0, -1, 17, 18, 1000])
def test_page_number_out_of_range_returns_422_on_field(client, value: int) -> None:
    response = client.post(
        "/imposition/locate", json={"total_pages": 16, "page_number": value}
    )
    body = _assert_page_number_error(response)
    assert "between 1 and 16" in body["detail"][0]["msg"]


@pytest.mark.parametrize("value", ["8", 8.0, 8.5, True, False, None, [8], {"n": 8}])
def test_page_number_wrong_type_returns_422_on_field(client, value) -> None:
    response = client.post(
        "/imposition/locate", json={"total_pages": 16, "page_number": value}
    )
    _assert_page_number_error(response)


def test_page_number_missing_returns_422_on_field(client) -> None:
    response = client.post("/imposition/locate", json={"total_pages": 16})
    body = _assert_page_number_error(response)
    assert body["detail"][0]["type"] == "missing"


def test_page_number_null_returns_422_on_field(client) -> None:
    response = client.post(
        "/imposition/locate", json={"total_pages": 16, "page_number": None}
    )
    _assert_page_number_error(response)


def test_total_pages_error_is_still_named(client) -> None:
    response = client.post(
        "/imposition/locate", json={"total_pages": 18, "page_number": 8}
    )
    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert {tuple(err["loc"]) for err in errors} == {("body", "total_pages")}
    assert "divisible by 4" in errors[0]["msg"]


def test_invalid_total_pages_and_non_integer_page_number_both_reported(
    client,
) -> None:
    response = client.post(
        "/imposition/locate", json={"total_pages": 18, "page_number": "x"}
    )
    assert response.status_code == 422, response.text
    error_by_field = {err["loc"][-1]: err for err in response.json()["detail"]}
    assert set(error_by_field) == {"total_pages", "page_number"}
    assert "divisible by 4" in error_by_field["total_pages"]["msg"]
    assert "integer" in error_by_field["page_number"]["msg"]


@pytest.mark.parametrize("page_number", [0, -1, -100])
def test_invalid_total_pages_and_out_of_range_page_number_both_reported(
    client, page_number: int
) -> None:
    # A page below 1 violates the one-based lower bound no matter what the
    # valid total would be, so it must be reported alongside the
    # total_pages error instead of disappearing with it.
    response = client.post(
        "/imposition/locate",
        json={"total_pages": 18, "page_number": page_number},
    )
    assert response.status_code == 422, response.text
    error_by_field = {err["loc"][-1]: err for err in response.json()["detail"]}
    assert set(error_by_field) == {"total_pages", "page_number"}
    assert "divisible by 4" in error_by_field["total_pages"]["msg"]
    assert "at least 1" in error_by_field["page_number"]["msg"]
    # No partial location may ride along with the error envelope.
    for key in ("sheet_index", "side", "position", "partner_page"):
        assert key not in response.json()


@pytest.mark.parametrize(
    "extra",
    [
        {"rotate": True},
        {"side": "front"},
        {"sheet_index": 0},
        {"page": 8},
        {"Page_Number": 8},
    ],
)
def test_extra_fields_are_rejected_with_field_loc(client, extra: dict) -> None:
    payload: dict = {"total_pages": 16, "page_number": 8, **extra}
    response = client.post("/imposition/locate", json=payload)
    assert response.status_code == 422, response.text
    body = response.json()
    assert "partner_page" not in body
    reported = {tuple(err["loc"]) for err in body["detail"]}
    for name in extra:
        assert ("body", name) in reported
    for err in body["detail"]:
        assert err["type"] == "extra_forbidden"


def test_malformed_json_returns_422(client) -> None:
    response = client.post(
        "/imposition/locate",
        content="{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "detail" in response.json()


def test_empty_body_names_both_missing_fields(client) -> None:
    response = client.post("/imposition/locate", json={})
    assert response.status_code == 422, response.text
    missing = {
        err["loc"][-1]
        for err in response.json()["detail"]
        if err["type"] == "missing"
    }
    assert missing == {"total_pages", "page_number"}


# ---------------------------------------------------------------------------
# Regression: the original endpoints keep their exact contracts.
# ---------------------------------------------------------------------------


def test_original_imposition_still_serves_sixteen_page_layout(client) -> None:
    response = client.post("/imposition", json={"total_pages": 16})
    assert response.status_code == 200
    assert response.json() == {
        "total_pages": 16,
        "sheet_count": 4,
        "sheets": [
            {"index": 0, "front": [16, 1], "back": [2, 15]},
            {"index": 1, "front": [14, 3], "back": [4, 13]},
            {"index": 2, "front": [12, 5], "back": [6, 11]},
            {"index": 3, "front": [10, 7], "back": [8, 9]},
        ],
    }


def test_original_imposition_error_semantics_unchanged(client) -> None:
    response = client.post("/imposition", json={"total_pages": 18})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "total_pages"]
    assert "divisible by 4" in detail[0]["msg"]


def test_health_still_ok(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
