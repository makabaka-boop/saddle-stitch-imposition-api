"""HTTP-level tests for the quote module.

Pins the acceptance case (1000 booklets of 16 pages -> 4200 sheets,
5250.00), the pending -> confirmed lifecycle with 404/409 feedback, and
field-level 422 semantics that never leak a partial quote.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

# The acceptance case: 1000 booklets of 16 pages at 1.25 per sheet with
# a 5% loss rate -> 4 sheets/booklet, 4000 base sheets, 200 loss sheets,
# 4200 sheets in total, 5250.00 total amount.
ACCEPTANCE_PAYLOAD = {
    "total_pages": 16,
    "print_run": 1000,
    "unit_price": "1.25",
    "loss_rate": 0.05,
}

ACCEPTANCE_SNAPSHOT = {
    "total_pages": 16,
    "print_run": 1000,
    "unit_price": "1.25",
    "loss_rate": "0.05",
    "sheets_per_booklet": 4,
    "base_sheets": 4000,
    "loss_sheets": 200,
    "total_sheets": 4200,
    "total_amount": "5250.00",
}


# ---------------------------------------------------------------------------
# Creation: the fixed acceptance numbers and the persisted snapshot.
# ---------------------------------------------------------------------------


def test_create_quote_acceptance_snapshot(client) -> None:
    response = client.post("/quotes", json=ACCEPTANCE_PAYLOAD)
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["quote_id"] == "Q-000001"
    assert body["status"] == "pending"
    assert body["confirmed_at"] is None
    assert body["created_at"]
    for key, expected in ACCEPTANCE_SNAPSHOT.items():
        assert body[key] == expected


def test_quote_numbers_are_independent_and_sequential(client) -> None:
    first = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()
    second = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "print_run": 500}
    ).json()

    assert first["quote_id"] == "Q-000001"
    assert second["quote_id"] == "Q-000002"
    assert second["base_sheets"] == 2000
    assert second["total_sheets"] == 2100
    assert second["total_amount"] == "2625.00"


def test_pending_quote_can_be_re_read(client) -> None:
    created = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()

    reread = client.get(f"/quotes/{created['quote_id']}")
    assert reread.status_code == 200, reread.text
    assert reread.json() == created


# ---------------------------------------------------------------------------
# Lifecycle: pending -> confirmed, with re-reads and restart persistence.
# ---------------------------------------------------------------------------


def test_confirm_transitions_quote_and_preserves_snapshot(client) -> None:
    created = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()

    response = client.post(f"/quotes/{created['quote_id']}/confirm")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["quote_id"] == created["quote_id"]
    assert body["status"] == "confirmed"
    assert body["confirmed_at"] is not None
    # The status transition never rewrites the pricing snapshot.
    for key in ACCEPTANCE_SNAPSHOT:
        assert body[key] == created[key]
    assert body["created_at"] == created["created_at"]


def test_confirmed_quote_can_be_re_read(client) -> None:
    created = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()
    confirmed = client.post(f"/quotes/{created['quote_id']}/confirm").json()

    reread = client.get(f"/quotes/{created['quote_id']}")
    assert reread.status_code == 200, reread.text
    assert reread.json() == confirmed


def test_confirmed_quote_survives_app_restart(client) -> None:
    # A brand-new app instance (lifespan runs again) against the same
    # database file must re-read the confirmed snapshot: SQLite
    # persistence inside the same process model, no external service.
    created = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()
    client.post(f"/quotes/{created['quote_id']}/confirm")

    with TestClient(app, raise_server_exceptions=True) as restarted:
        reread = restarted.get(f"/quotes/{created['quote_id']}")

    assert reread.status_code == 200, reread.text
    body = reread.json()
    assert body["status"] == "confirmed"
    assert body["confirmed_at"] is not None
    for key, expected in ACCEPTANCE_SNAPSHOT.items():
        assert body[key] == expected


# ---------------------------------------------------------------------------
# 404 / 409 feedback keeps the standard FastAPI error envelope.
# ---------------------------------------------------------------------------


def test_confirm_unknown_quote_returns_404(client) -> None:
    response = client.post("/quotes/Q-999999/confirm")
    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown quote_id: 'Q-999999'."}


def test_get_unknown_quote_returns_404(client) -> None:
    response = client.get("/quotes/Q-999999")
    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown quote_id: 'Q-999999'."}


@pytest.mark.parametrize("quote_id", ["not-a-quote", "Q-", "1.5", "Q-abc"])
def test_malformed_quote_id_returns_404(client, quote_id: str) -> None:
    for method in ("get", "post"):
        url = f"/quotes/{quote_id}" + ("/confirm" if method == "post" else "")
        response = getattr(client, method)(url)
        assert response.status_code == 404, response.text
        assert "detail" in response.json()


def test_repeat_confirm_returns_409_and_keeps_original_snapshot(client) -> None:
    created = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()
    first = client.post(f"/quotes/{created['quote_id']}/confirm")
    assert first.status_code == 200

    second = client.post(f"/quotes/{created['quote_id']}/confirm")
    assert second.status_code == 409
    assert second.json() == {
        "detail": f"Quote '{created['quote_id']}' is already confirmed."
    }

    # The failed confirm must not rewrite the stored snapshot, including
    # the original confirmed timestamp.
    reread = client.get(f"/quotes/{created['quote_id']}")
    assert reread.status_code == 200
    assert reread.json() == first.json()


def test_confirming_one_quote_leaves_others_pending(client) -> None:
    first = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()
    second = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()

    client.post(f"/quotes/{first['quote_id']}/confirm")

    reread = client.get(f"/quotes/{second['quote_id']}").json()
    assert reread["status"] == "pending"
    assert reread["confirmed_at"] is None


# ---------------------------------------------------------------------------
# 422 field-level errors: booleans, float prices, negatives, extras.
# ---------------------------------------------------------------------------


def _assert_quote_field_error(response, field: str) -> dict:
    assert response.status_code == 422, response.text
    body = response.json()
    assert "detail" in body and isinstance(body["detail"], list)
    assert body["detail"], "expected at least one error"
    for error in body["detail"]:
        # The caller must be able to locate the offending field.
        assert error["loc"][-1] == field
        assert error["msg"]
    # No partial quote may ride along with the error envelope.
    for key in ("quote_id", "status", "total_sheets", "total_amount"):
        assert key not in body
    return body


@pytest.mark.parametrize(
    "field", ["total_pages", "print_run", "unit_price", "loss_rate"]
)
def test_boolean_fields_are_rejected(client, field: str) -> None:
    response = client.post("/quotes", json={**ACCEPTANCE_PAYLOAD, field: True})
    _assert_quote_field_error(response, field)


def test_float_unit_price_is_rejected(client) -> None:
    # The central pricing rule: a JSON float price is never accepted,
    # not even an integral one; use a string ("1.25") or an integer.
    for value in (1.25, 2.0, 0.1):
        response = client.post(
            "/quotes", json={**ACCEPTANCE_PAYLOAD, "unit_price": value}
        )
        body = _assert_quote_field_error(response, "unit_price")
        assert "not a float" in body["detail"][0]["msg"]


def test_integer_unit_price_is_accepted(client) -> None:
    response = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "unit_price": 2}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["unit_price"] == "2"
    assert body["total_amount"] == "8400.00"


@pytest.mark.parametrize("value", ["-0.01", "-1"])
def test_negative_unit_price_is_rejected(client, value: str) -> None:
    response = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "unit_price": value}
    )
    body = _assert_quote_field_error(response, "unit_price")
    assert "negative" in body["detail"][0]["msg"]


@pytest.mark.parametrize("value", ["abc", "", "1.2.3", "NaN", "Infinity", None])
def test_invalid_unit_price_is_rejected(client, value) -> None:
    response = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "unit_price": value}
    )
    _assert_quote_field_error(response, "unit_price")


@pytest.mark.parametrize("value", [0, -1, -1000])
def test_non_positive_print_run_is_rejected(client, value: int) -> None:
    response = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "print_run": value}
    )
    body = _assert_quote_field_error(response, "print_run")
    assert "at least 1" in body["detail"][0]["msg"]


@pytest.mark.parametrize("value", ["1000", 1000.0, 1.5, None])
def test_non_integer_print_run_is_rejected(client, value) -> None:
    response = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "print_run": value}
    )
    _assert_quote_field_error(response, "print_run")


@pytest.mark.parametrize("value", [-0.01, "-0.5"])
def test_negative_loss_rate_is_rejected(client, value) -> None:
    response = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "loss_rate": value}
    )
    body = _assert_quote_field_error(response, "loss_rate")
    assert "negative" in body["detail"][0]["msg"]


@pytest.mark.parametrize("value", ["abc", "", None, [0.05]])
def test_invalid_loss_rate_is_rejected(client, value) -> None:
    response = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "loss_rate": value}
    )
    _assert_quote_field_error(response, "loss_rate")


def test_loss_rate_as_string_is_accepted(client) -> None:
    response = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "loss_rate": "0.05"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["loss_sheets"] == 200


@pytest.mark.parametrize("value", [0, 18, 129, "16", 16.0])
def test_total_pages_rules_still_apply(client, value) -> None:
    response = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "total_pages": value}
    )
    _assert_quote_field_error(response, "total_pages")


@pytest.mark.parametrize(
    "extra",
    [
        {"rotate": True},
        {"discount": 0.1},
        {"total_page": 16},  # common typo of total_pages
        {"Unit_Price": "1.25"},  # case mismatch is also a distinct field
    ],
)
def test_extra_fields_are_rejected_with_field_loc(client, extra: dict) -> None:
    response = client.post("/quotes", json={**ACCEPTANCE_PAYLOAD, **extra})

    assert response.status_code == 422, response.text
    body = response.json()
    assert "quote_id" not in body
    reported = {tuple(err["loc"]) for err in body["detail"]}
    for name in extra:
        assert ("body", name) in reported
    for err in body["detail"]:
        assert err["type"] == "extra_forbidden"


def test_missing_fields_are_all_reported(client) -> None:
    response = client.post("/quotes", json={})
    assert response.status_code == 422, response.text
    missing = {
        err["loc"][-1]
        for err in response.json()["detail"]
        if err["type"] == "missing"
    }
    assert missing == {"total_pages", "print_run", "unit_price", "loss_rate"}


def test_malformed_json_returns_422(client) -> None:
    response = client.post(
        "/quotes",
        content="{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "detail" in response.json()


def test_rejected_quote_persists_nothing(client) -> None:
    response = client.post(
        "/quotes", json={**ACCEPTANCE_PAYLOAD, "unit_price": 1.25}
    )
    assert response.status_code == 422
    # No row was written: the next valid quote still gets the first number.
    created = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()
    assert created["quote_id"] == "Q-000001"


# ---------------------------------------------------------------------------
# OpenAPI contract and regression of the pre-existing endpoints.
# ---------------------------------------------------------------------------


def test_openapi_documents_quotes(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/quotes" in schema["paths"]
    assert "/quotes/compare" in schema["paths"]
    assert "/quotes/{quote_id}" in schema["paths"]
    assert "/quotes/{quote_id}/confirm" in schema["paths"]

    props = schema["components"]["schemas"]["QuoteRequest"]["properties"]
    assert set(props) == {"total_pages", "print_run", "unit_price", "loss_rate"}
    # The price contract advertises exactly what is accepted: a decimal
    # string or an integer, never a float.
    assert props["unit_price"]["anyOf"] == [
        {"type": "string"},
        {"type": "integer"},
    ]

    compare_props = schema["components"]["schemas"]["QuoteCompareRequest"][
        "properties"
    ]
    assert set(compare_props) == {"baseline_quote_id", "candidate_quote_id"}
    compare_response_props = schema["components"]["schemas"][
        "QuoteCompareResponse"
    ]["properties"]
    assert set(compare_response_props) == {
        "baseline_quote_id",
        "candidate_quote_id",
        "sheet_difference",
        "amount_difference",
        "lower_quote_id",
    }


def test_imposition_still_serves_sixteen_page_layout(client) -> None:
    response = client.post("/imposition", json={"total_pages": 16})
    assert response.status_code == 200
    assert response.json()["sheet_count"] == 4


def test_locate_still_positions_page(client) -> None:
    response = client.post(
        "/imposition/locate", json={"total_pages": 16, "page_number": 1}
    )
    assert response.status_code == 200
    assert response.json()["sheet_index"] == 0


def test_health_still_ok(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /quotes/compare: signed differences between two persisted snapshots.
# ---------------------------------------------------------------------------


# Two paper/loss alternatives for the SAME job (16 pages, 1000 booklets):
#   baseline Q-000001: 1.25 per sheet, 5% loss -> 4200 sheets, 5250.00
#   candidate Q-000002: 1.20 per sheet, 3% loss -> 4120 sheets, 4944.00
CANDIDATE_PAYLOAD = {
    "total_pages": 16,
    "print_run": 1000,
    "unit_price": "1.20",
    "loss_rate": 0.03,
}


def _create_comparable_pair(client) -> tuple[str, str]:
    baseline = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()
    candidate = client.post("/quotes", json=CANDIDATE_PAYLOAD).json()
    return baseline["quote_id"], candidate["quote_id"]


def test_compare_acceptance_pair_positive_sheet_and_amount_gap(client) -> None:
    baseline_id, candidate_id = _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": baseline_id, "candidate_quote_id": candidate_id},
    )
    assert response.status_code == 200, response.text

    assert response.json() == {
        "baseline_quote_id": "Q-000001",
        "candidate_quote_id": "Q-000002",
        "sheet_difference": 80,  # 4200 - 4120
        "amount_difference": "306.00",  # 5250.00 - 4944.00, two places
        "lower_quote_id": "Q-000002",  # the candidate is cheaper
    }


def test_compare_swap_reverses_both_signs(client) -> None:
    baseline_id, candidate_id = _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": candidate_id, "candidate_quote_id": baseline_id},
    )
    assert response.status_code == 200, response.text

    assert response.json() == {
        "baseline_quote_id": "Q-000002",
        "candidate_quote_id": "Q-000001",
        "sheet_difference": -80,
        "amount_difference": "-306.00",
        # Cheaper quote is a fact about the pair, independent of order.
        "lower_quote_id": "Q-000002",
    }


def test_compare_keeps_input_order_when_baseline_number_is_larger(
    client,
) -> None:
    # The storage IN clause makes no order promise; the response must
    # still label Q-000002 as the baseline when submitted first.
    baseline_id, candidate_id = _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": candidate_id, "candidate_quote_id": baseline_id},
    )
    body = response.json()
    assert body["baseline_quote_id"] == candidate_id
    assert body["candidate_quote_id"] == baseline_id


def test_compare_accepts_bare_digit_quote_numbers(client) -> None:
    _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": "1", "candidate_quote_id": "2"},
    )
    assert response.status_code == 200, response.text
    # Canonical numbers are echoed even when bare digits were submitted.
    assert response.json()["baseline_quote_id"] == "Q-000001"
    assert response.json()["amount_difference"] == "306.00"


def test_compare_equal_amounts_reports_null_lower_quote_id(client) -> None:
    first = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()
    second = client.post("/quotes", json=ACCEPTANCE_PAYLOAD).json()

    response = client.post(
        "/quotes/compare",
        json={
            "baseline_quote_id": first["quote_id"],
            "candidate_quote_id": second["quote_id"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "baseline_quote_id": "Q-000001",
        "candidate_quote_id": "Q-000002",
        "sheet_difference": 0,
        "amount_difference": "0.00",
        "lower_quote_id": None,
    }


# ---------------------------------------------------------------------------
# 404: malformed or unknown numbers name the offending input.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["not-a-quote", "Q-", "1.5", "Q-abc", ""])
def test_compare_malformed_quote_id_returns_404_naming_it(
    client, bad_id: str
) -> None:
    _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": "Q-000001", "candidate_quote_id": bad_id},
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": f"Unknown quote_id: {bad_id!r}."}


@pytest.mark.parametrize("bad_id", ["not-a-quote", "Q-", "1.5", "Q-abc", ""])
def test_compare_malformed_baseline_quote_id_returns_404_naming_it(
    client, bad_id: str
) -> None:
    _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": bad_id, "candidate_quote_id": "Q-000001"},
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": f"Unknown quote_id: {bad_id!r}."}


def test_compare_unknown_baseline_returns_404_naming_it(client) -> None:
    _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": "Q-999999", "candidate_quote_id": "Q-000001"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown quote_id: 'Q-999999'."}


def test_compare_unknown_candidate_returns_404_naming_it(client) -> None:
    _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": "Q-000001", "candidate_quote_id": "Q-999999"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown quote_id: 'Q-999999'."}


# ---------------------------------------------------------------------------
# 409: only same-page-count, same-run quotes are comparable.
# ---------------------------------------------------------------------------


def test_compare_different_page_counts_returns_409(client) -> None:
    baseline_id, _ = _create_comparable_pair(client)
    other = client.post(
        "/quotes",
        json={**CANDIDATE_PAYLOAD, "total_pages": 8},
    ).json()

    response = client.post(
        "/quotes/compare",
        json={
            "baseline_quote_id": baseline_id,
            "candidate_quote_id": other["quote_id"],
        },
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert baseline_id in detail and other["quote_id"] in detail
    assert "16 pages" in detail and "8 pages" in detail


def test_compare_different_print_runs_returns_409(client) -> None:
    baseline_id, _ = _create_comparable_pair(client)
    other = client.post(
        "/quotes",
        json={**CANDIDATE_PAYLOAD, "print_run": 500},
    ).json()

    response = client.post(
        "/quotes/compare",
        json={
            "baseline_quote_id": baseline_id,
            "candidate_quote_id": other["quote_id"],
        },
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "1000 booklets" in detail and "500 booklets" in detail


def test_compare_failure_never_rewrites_quotes(client) -> None:
    baseline_id, candidate_id = _create_comparable_pair(client)
    other = client.post(
        "/quotes", json={**CANDIDATE_PAYLOAD, "print_run": 500}
    ).json()
    before = {
        quote_id: client.get(f"/quotes/{quote_id}").json()
        for quote_id in (baseline_id, candidate_id, other["quote_id"])
    }

    for payload in (
        {"baseline_quote_id": baseline_id, "candidate_quote_id": "Q-999999"},
        {"baseline_quote_id": baseline_id, "candidate_quote_id": other["quote_id"]},
        {"baseline_quote_id": baseline_id, "candidate_quote_id": baseline_id},
    ):
        client.post("/quotes/compare", json=payload)

    for quote_id, snapshot in before.items():
        # No status flip, no confirmed_at stamp, no snapshot rewrite.
        assert client.get(f"/quotes/{quote_id}").json() == snapshot
        assert snapshot["status"] == "pending"
        assert snapshot["confirmed_at"] is None


# ---------------------------------------------------------------------------
# 422 boundary: identical numbers, booleans, extras, missing fields.
# ---------------------------------------------------------------------------


def test_compare_same_canonical_number_is_rejected(client) -> None:
    _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": "Q-000001", "candidate_quote_id": "Q-000001"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert "different" in body["detail"][0]["msg"]


def test_compare_same_number_canonical_and_bare_digits_is_rejected(
    client,
) -> None:
    _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": "Q-000001", "candidate_quote_id": "1"},
    )
    assert response.status_code == 422
    assert "different" in response.json()["detail"][0]["msg"]


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        1,
        2,
        None,
        1.5,
        ["Q-000001"],
        {"id": "Q-000001"},
    ],
)
def test_compare_rejects_non_string_quote_ids(client, value) -> None:
    _create_comparable_pair(client)

    for field in ("baseline_quote_id", "candidate_quote_id"):
        payload = {
            "baseline_quote_id": "Q-000001",
            "candidate_quote_id": "Q-000002",
            field: value,
        }
        response = client.post("/quotes/compare", json=payload)
        assert response.status_code == 422, response.text
        assert response.json()["detail"][0]["loc"][-1] == field


@pytest.mark.parametrize("extra", [{"rotate": True}, {"quote_id": "Q-000003"}])
def test_compare_rejects_extra_fields(client, extra: dict) -> None:
    _create_comparable_pair(client)

    response = client.post(
        "/quotes/compare",
        json={
            "baseline_quote_id": "Q-000001",
            "candidate_quote_id": "Q-000002",
            **extra,
        },
    )
    assert response.status_code == 422
    reported = {tuple(err["loc"]) for err in response.json()["detail"]}
    for name in extra:
        assert ("body", name) in reported
    assert all(
        err["type"] == "extra_forbidden" for err in response.json()["detail"]
    )


def test_compare_missing_fields_are_all_reported(client) -> None:
    response = client.post("/quotes/compare", json={})
    assert response.status_code == 422
    missing = {
        err["loc"][-1]
        for err in response.json()["detail"]
        if err["type"] == "missing"
    }
    assert missing == {"baseline_quote_id", "candidate_quote_id"}


def test_compare_confirmed_quotes_are_allowed_and_unchanged(client) -> None:
    baseline_id, candidate_id = _create_comparable_pair(client)
    confirmed = client.post(f"/quotes/{baseline_id}/confirm")
    assert confirmed.status_code == 200

    response = client.post(
        "/quotes/compare",
        json={"baseline_quote_id": baseline_id, "candidate_quote_id": candidate_id},
    )
    assert response.status_code == 200, response.text
    assert response.json()["amount_difference"] == "306.00"

    reread = client.get(f"/quotes/{baseline_id}").json()
    assert reread["status"] == "confirmed"
    assert reread["confirmed_at"] == confirmed.json()["confirmed_at"]
