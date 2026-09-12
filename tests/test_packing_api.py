"""HTTP-level tests for the packing-plan module.

Pins the acceptance case (a quote for 1000 copies split at 300/carton ->
300, 300, 300, 100 in four cartons), exact-division splits, 404 feedback
for unknown quotes/plans, field-level 422 semantics (invalid capacity,
the 500-carton limit and extra fields) that never write a plan or consume
a number, and reading the stored result straight back out of SQLite.
"""

from __future__ import annotations

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app

# Same acceptance quote as the quote suite: 16 pages / 1000 copies.
ACCEPTANCE_QUOTE = {
    "total_pages": 16,
    "print_run": 1000,
    "unit_price": "1.25",
    "loss_rate": 0.05,
}


def _create_quote(client, payload: dict | None = None) -> str:
    response = client.post("/quotes", json=payload or ACCEPTANCE_QUOTE)
    assert response.status_code == 201, response.text
    return response.json()["quote_id"]


# ---------------------------------------------------------------------------
# The acceptance split.
# ---------------------------------------------------------------------------


def test_create_packing_plan_acceptance_split(client) -> None:
    quote_id = _create_quote(client)

    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 300}
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["plan_id"] == "PK-000001"
    assert body["quote_id"] == "Q-000001"
    assert body["print_run"] == 1000
    assert body["carton_capacity"] == 300
    assert body["carton_count"] == 4
    assert body["created_at"]
    assert body["cartons"] == [
        {"index": 0, "start_copy": 1, "end_copy": 300, "copy_count": 300},
        {"index": 1, "start_copy": 301, "end_copy": 600, "copy_count": 300},
        {"index": 2, "start_copy": 601, "end_copy": 900, "copy_count": 300},
        {"index": 3, "start_copy": 901, "end_copy": 1000, "copy_count": 100},
    ]


def test_plan_numbers_are_independent_and_sequential(client) -> None:
    quote_id = _create_quote(client)

    first = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 300}
    ).json()
    second = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 500}
    ).json()

    assert first["plan_id"] == "PK-000001"
    assert second["plan_id"] == "PK-000002"
    assert second["carton_count"] == 2
    assert second["cartons"][-1]["copy_count"] == 500


# ---------------------------------------------------------------------------
# Exact-division splits (no short last carton).
# ---------------------------------------------------------------------------


def test_exact_division_split_600_at_300(client) -> None:
    quote_id = _create_quote(client, {**ACCEPTANCE_QUOTE, "print_run": 600})

    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 300}
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["carton_count"] == 2
    assert body["cartons"] == [
        {"index": 0, "start_copy": 1, "end_copy": 300, "copy_count": 300},
        {"index": 1, "start_copy": 301, "end_copy": 600, "copy_count": 300},
    ]


def test_run_smaller_than_capacity_is_single_full_used_carton(client) -> None:
    quote_id = _create_quote(client, {**ACCEPTANCE_QUOTE, "print_run": 10})

    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 50}
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["carton_count"] == 1
    assert body["cartons"] == [
        {"index": 0, "start_copy": 1, "end_copy": 10, "copy_count": 10},
    ]


# ---------------------------------------------------------------------------
# GET: re-read the complete result.
# ---------------------------------------------------------------------------


def test_get_packing_plan_rereads_full_result(client) -> None:
    quote_id = _create_quote(client)
    created = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 300}
    ).json()

    reread = client.get(f"/packing-plans/{created['plan_id']}")
    assert reread.status_code == 200, reread.text
    assert reread.json() == created


def test_get_packing_plan_accepts_bare_digits(client) -> None:
    quote_id = _create_quote(client)
    client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 300}
    )

    reread = client.get("/packing-plans/1")
    assert reread.status_code == 200, reread.text
    assert reread.json()["plan_id"] == "PK-000001"
    assert reread.json()["carton_count"] == 4


def test_packing_plan_survives_app_restart(client) -> None:
    quote_id = _create_quote(client)
    created = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 300}
    ).json()

    with TestClient(app, raise_server_exceptions=True) as restarted:
        reread = restarted.get(f"/packing-plans/{created['plan_id']}")

    assert reread.status_code == 200, reread.text
    assert reread.json() == created


def test_get_unknown_plan_returns_404(client) -> None:
    response = client.get("/packing-plans/PK-999999")
    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown packing plan_id: 'PK-999999'."}


@pytest.mark.parametrize(
    "plan_id", ["not-a-plan", "PK-", "1.5", "PK-abc", "Q-000001"]
)
def test_malformed_plan_id_returns_404(client, plan_id: str) -> None:
    response = client.get(f"/packing-plans/{plan_id}")
    assert response.status_code == 404, response.text
    assert "detail" in response.json()


# ---------------------------------------------------------------------------
# 404 on the source quote.
# ---------------------------------------------------------------------------


def test_create_plan_for_unknown_quote_returns_404(client) -> None:
    response = client.post(
        "/packing-plans", json={"quote_id": "Q-999999", "carton_capacity": 300}
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown quote_id: 'Q-999999'."}


@pytest.mark.parametrize("quote_id", ["not-a-quote", "Q-", "1.5", "Q-abc", ""])
def test_create_plan_for_malformed_quote_returns_404(client, quote_id: str) -> None:
    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 300}
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": f"Unknown quote_id: {quote_id!r}."}


def test_create_plan_works_for_confirmed_quote(client) -> None:
    quote_id = _create_quote(client)
    assert client.post(f"/quotes/{quote_id}/confirm").status_code == 200

    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 300}
    )
    assert response.status_code == 201, response.text
    assert response.json()["carton_count"] == 4


# ---------------------------------------------------------------------------
# 422: carton_capacity validation and the 500-carton limit.
# ---------------------------------------------------------------------------


def _assert_capacity_field_error(response) -> dict:
    assert response.status_code == 422, response.text
    body = response.json()
    assert "detail" in body and isinstance(body["detail"], list)
    assert body["detail"], "expected at least one error"
    for error in body["detail"]:
        assert error["loc"][-1] == "carton_capacity"
        assert error["msg"]
    for key in ("plan_id", "carton_count", "cartons"):
        assert key not in body
    return body


@pytest.mark.parametrize("value", [0, -1, -300])
def test_non_positive_capacity_is_rejected(client, value: int) -> None:
    quote_id = _create_quote(client)
    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": value}
    )
    body = _assert_capacity_field_error(response)
    assert "at least 1" in body["detail"][0]["msg"]


@pytest.mark.parametrize("value", ["300", 300.0, 1.5, True, False, None, [300]])
def test_non_integer_capacity_is_rejected(client, value) -> None:
    quote_id = _create_quote(client)
    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": value}
    )
    _assert_capacity_field_error(response)


def test_missing_capacity_is_rejected_on_cartons_field(client) -> None:
    quote_id = _create_quote(client)
    response = client.post("/packing-plans", json={"quote_id": quote_id})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "carton_capacity"


def test_over_500_cartons_is_rejected_on_capacity_field(client) -> None:
    quote_id = _create_quote(client)  # print run 1000

    # capacity 1 -> 1000 cartons, over the 500 limit.
    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 1}
    )
    body = _assert_capacity_field_error(response)
    assert "500" in body["detail"][0]["msg"]


def test_exactly_500_cartons_is_accepted(client) -> None:
    quote_id = _create_quote(client, {**ACCEPTANCE_QUOTE, "print_run": 500})

    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 1}
    )
    assert response.status_code == 201, response.text
    assert response.json()["carton_count"] == 500


def test_501_cartons_is_rejected_even_when_run_is_1001(client) -> None:
    quote_id = _create_quote(client, {**ACCEPTANCE_QUOTE, "print_run": 1001})

    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 2}
    )  # ceil(1001/2) = 501
    body = _assert_capacity_field_error(response)
    assert "501" in body["detail"][0]["msg"]


def test_non_string_quote_id_is_rejected_on_quote_field(client) -> None:
    for value in (1, True, None, ["Q-000001"]):
        response = client.post(
            "/packing-plans", json={"quote_id": value, "carton_capacity": 300}
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"][0]["loc"][-1] == "quote_id"


# ---------------------------------------------------------------------------
# 422: extra fields.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"rotate": True},
        {"carton_capacities": 300},  # common typo
        {"label": "A"},
        {"Carton_Capacity": 300},  # case mismatch is a distinct field
    ],
)
def test_extra_fields_are_rejected_with_field_loc(client, extra: dict) -> None:
    quote_id = _create_quote(client)
    response = client.post(
        "/packing-plans",
        json={"quote_id": quote_id, "carton_capacity": 300, **extra},
    )
    assert response.status_code == 422, response.text
    reported = {tuple(err["loc"]) for err in response.json()["detail"]}
    for name in extra:
        assert ("body", name) in reported
    assert all(
        err["type"] == "extra_forbidden" for err in response.json()["detail"]
    )


def test_missing_both_fields_is_rejected(client) -> None:
    response = client.post("/packing-plans", json={})
    assert response.status_code == 422
    missing = {
        err["loc"][-1]
        for err in response.json()["detail"]
        if err["type"] == "missing"
    }
    assert missing == {"quote_id", "carton_capacity"}


def test_malformed_json_returns_422(client) -> None:
    response = client.post(
        "/packing-plans",
        content="{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "detail" in response.json()


# ---------------------------------------------------------------------------
# Failures persist nothing and consume no plan number.
# ---------------------------------------------------------------------------


def test_failed_creation_persists_nothing_and_consumes_no_number(client) -> None:
    quote_id = _create_quote(client)

    for payload in (
        {"quote_id": quote_id, "carton_capacity": 0},
        {"quote_id": quote_id, "carton_capacity": "300"},
        {"quote_id": quote_id, "carton_capacity": 1},  # 1000 > 500 cartons
        {"quote_id": "Q-999999", "carton_capacity": 300},
        {"quote_id": quote_id, "carton_capacity": 300, "extra": 1},
    ):
        response = client.post("/packing-plans", json=payload)
        assert response.status_code in (404, 422), response.text

    created = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 300}
    )
    assert created.status_code == 201, created.text
    assert created.json()["plan_id"] == "PK-000001"

    # And nothing is readable back for would-be earlier numbers.
    assert client.get("/packing-plans/PK-000001").status_code == 200
    assert client.get("/packing-plans/PK-000002").status_code == 404


def test_raw_sqlite_holds_exactly_the_acceptance_plan(client) -> None:
    quote_id = _create_quote(client)
    response = client.post(
        "/packing-plans", json={"quote_id": quote_id, "carton_capacity": 300}
    )
    assert response.status_code == 201, response.text
    plan_id = response.json()["plan_id"]

    db_path = os.environ["QUOTE_DB_PATH"]
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        header = connection.execute(
            "SELECT * FROM packing_plans WHERE id = 1"
        ).fetchone()
        assert dict(header) == {
            "id": 1,
            "source_quote_id": 1,
            "print_run": 1000,
            "carton_capacity": 300,
            "carton_count": 4,
            "created_at": response.json()["created_at"],
        }
        cartons = connection.execute(
            "SELECT carton_index, start_copy, end_copy, copy_count"
            " FROM packing_cartons WHERE packing_plan_id = 1 ORDER BY carton_index"
        ).fetchall()
        assert [tuple(row) for row in cartons] == [
            (0, 1, 300, 300),
            (1, 301, 600, 300),
            (2, 601, 900, 300),
            (3, 901, 1000, 100),
        ]
        # The snapshot itself is unchanged by packing.
        quote = connection.execute("SELECT print_run FROM quotes WHERE id = 1")
        assert quote.fetchone()["print_run"] == 1000
    finally:
        connection.close()

    # The GET response is exactly what raw SQLite stores.
    reread = client.get(f"/packing-plans/{plan_id}").json()
    assert reread["print_run"] == 1000
    assert reread["carton_count"] == 4
    assert [(c["start_copy"], c["end_copy"], c["copy_count"]) for c in reread["cartons"]] == [
        (1, 300, 300),
        (301, 600, 300),
        (601, 900, 300),
        (901, 1000, 100),
    ]


# ---------------------------------------------------------------------------
# OpenAPI contract and regression of the pre-existing endpoints.
# ---------------------------------------------------------------------------


def test_openapi_documents_packing_plans(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/packing-plans" in schema["paths"]
    assert "/packing-plans/{plan_id}" in schema["paths"]

    props = schema["components"]["schemas"]["PackingPlanRequest"]["properties"]
    assert set(props) == {"quote_id", "carton_capacity"}
    assert props["carton_capacity"]["minimum"] == 1

    response_props = schema["components"]["schemas"]["PackingPlanResponse"][
        "properties"
    ]
    assert set(response_props) == {
        "plan_id",
        "quote_id",
        "print_run",
        "carton_capacity",
        "carton_count",
        "cartons",
        "created_at",
    }


def test_imposition_still_serves_sixteen_page_layout(client) -> None:
    response = client.post("/imposition", json={"total_pages": 16})
    assert response.status_code == 200
    assert response.json()["sheet_count"] == 4


def test_locate_still_positions_page(client) -> None:
    response = client.post(
        "/imposition/locate", json={"total_pages": 16, "page_number": 8}
    )
    assert response.status_code == 200
    assert response.json()["sheet_index"] == 3


def test_quotes_still_create_and_read(client) -> None:
    created = client.post("/quotes", json=ACCEPTANCE_QUOTE)
    assert created.status_code == 201
    quote_id = created.json()["quote_id"]
    reread = client.get(f"/quotes/{quote_id}")
    assert reread.status_code == 200
    assert reread.json()["total_sheets"] == 4200


def test_health_still_ok(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}
