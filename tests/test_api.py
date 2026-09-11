"""HTTP-level tests: success contract and 422 field-level errors."""

from __future__ import annotations

import pytest

VALID_TOTALS = list(range(4, 128 + 1, 4))


def test_health(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("total_pages", VALID_TOTALS)
def test_valid_requests_return_unique_complete_order(client, total_pages: int) -> None:
    response = client.post("/imposition", json={"total_pages": total_pages})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["total_pages"] == total_pages
    assert body["sheet_count"] == total_pages // 4
    assert len(body["sheets"]) == total_pages // 4

    pages: list[int] = []
    for position, sheet in enumerate(body["sheets"]):
        assert set(sheet.keys()) == {"index", "front", "back"}
        assert sheet["index"] == position
        assert len(sheet["front"]) == 2
        assert len(sheet["back"]) == 2
        assert all(isinstance(p, int) for p in sheet["front"] + sheet["back"])
        pages.extend(sheet["front"])
        pages.extend(sheet["back"])

    # The full permutation invariant over the HTTP response.
    assert sorted(pages) == list(range(1, total_pages + 1))
    assert len(pages) == len(set(pages)) == total_pages


def test_sixteen_page_response_example(client) -> None:
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


def test_openapi_documents_total_pages(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/imposition" in schema["paths"]
    request_schema_name = "ImpositionRequest"
    assert "total_pages" in schema["components"]["schemas"][request_schema_name][
        "properties"
    ]


# ---------------------------------------------------------------------------
# 422 field-level errors: every failure must point at total_pages and must
# never include a partial "sheets" payload.
# ---------------------------------------------------------------------------


def _assert_field_error(response) -> dict:
    assert response.status_code == 422, response.text
    body = response.json()
    assert "detail" in body and isinstance(body["detail"], list)
    assert body["detail"], "expected at least one error"
    for error in body["detail"]:
        # The caller must be able to locate the offending field.
        assert error["loc"][-1] == "total_pages"
        assert error["msg"]
    assert "sheets" not in body
    assert "sheet_count" not in body
    return body


@pytest.mark.parametrize("value", [0, 1, 2, 3, -4, 129, 130, 1000])
def test_out_of_range_returns_422_on_field(client, value: int) -> None:
    response = client.post("/imposition", json={"total_pages": value})
    body = _assert_field_error(response)
    assert "between 4 and 128" in body["detail"][0]["msg"]


@pytest.mark.parametrize("value", [5, 6, 7, 9, 13, 14, 126, 127])
def test_not_divisible_by_four_returns_422_on_field(client, value: int) -> None:
    response = client.post("/imposition", json={"total_pages": value})
    body = _assert_field_error(response)
    assert "divisible by 4" in body["detail"][0]["msg"]


@pytest.mark.parametrize("value", ["8", 8.0, 8.5, True, False, None, [8], {"n": 8}])
def test_wrong_type_returns_422_on_field(client, value) -> None:
    response = client.post("/imposition", json={"total_pages": value})
    _assert_field_error(response)


def test_missing_field_returns_422_on_field(client) -> None:
    response = client.post("/imposition", json={})
    body = _assert_field_error(response)
    assert body["detail"][0]["type"] == "missing"


def test_malformed_json_returns_422(client) -> None:
    response = client.post(
        "/imposition",
        content="{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    # Body-level JSON parse error is loc "body"; field semantics still hold
    # for any structurally understood request (covered above).
    assert "detail" in response.json()


@pytest.mark.parametrize(
    "extra",
    [
        {"rotate": True},
        {"rotation": 90},
        {"blank_pages": 2},
        {"total_page": 8},  # common typo of total_pages
        {"Total_Pages": 8},  # case mismatch is also a distinct field
        {"rotate": True, "blank_pages": 2},
    ],
)
def test_extra_fields_are_rejected_with_field_loc(client, extra: dict) -> None:
    payload: dict = {"total_pages": 8, **extra}
    response = client.post("/imposition", json=payload)

    assert response.status_code == 422, response.text
    body = response.json()
    assert isinstance(body["detail"], list)
    # No partial imposition is ever returned.
    assert "sheets" not in body
    assert "sheet_count" not in body

    reported_fields = {tuple(err["loc"]) for err in body["detail"]}
    for name in extra:
        assert ("body", name) in reported_fields
    for err in body["detail"]:
        assert err["type"] == "extra_forbidden"
        assert err["loc"][-1] in extra
        assert err["msg"]


def test_extra_field_and_invalid_total_pages_both_reported(client) -> None:
    # Every problem must be locatable at once: the stray field AND the
    # invalid page count, with no partial result.
    response = client.post(
        "/imposition",
        json={"total_pages": 18, "rotate": True},
    )
    assert response.status_code == 422, response.text
    body = response.json()

    error_by_field = {err["loc"][-1]: err for err in body["detail"]}
    assert set(error_by_field) == {"total_pages", "rotate"}
    assert "divisible by 4" in error_by_field["total_pages"]["msg"]
    assert error_by_field["rotate"]["type"] == "extra_forbidden"
    assert "sheets" not in body
