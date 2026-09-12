"""Pydantic boundary schemas for the imposition API.

All invalid input is rejected at this boundary so handlers only ever run
with a validated page count and never emit a partial imposition.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .imposition import (
    InvalidPageNumber,
    InvalidTotalPages,
    MAX_PAGES,
    MIN_PAGES,
    PAGES_PER_SHEET,
    validate_page_number,
    validate_page_number_lower_bound,
    validate_total_pages,
)
from .quotes import (
    MIN_PRINT_RUN,
    InvalidLossRate,
    InvalidPrintRun,
    InvalidUnitPrice,
    parse_quote_id,
    validate_loss_rate,
    validate_print_run,
    validate_unit_price,
)


class ImpositionRequest(BaseModel):
    # Typos (e.g. "total_page") and unrelated fields (e.g. "rotate")
    # must be rejected with a field-level 422 naming the offending field,
    # instead of being silently dropped.
    model_config = ConfigDict(extra="forbid")

    total_pages: int = Field(
        description="Booklet page count: integer, 4..128, divisible by 4."
    )

    @field_validator("total_pages", mode="before")
    @classmethod
    def _validate_total_pages(cls, value: object) -> int:
        # Single source of truth is the pure core; its ValueError subclass
        # is reported by Pydantic as a field error on total_pages (HTTP 422).
        try:
            return validate_total_pages(value)
        except InvalidTotalPages as exc:
            raise ValueError(str(exc)) from exc


class LocateRequest(BaseModel):
    # Same strict boundary as ImpositionRequest: typos and unrelated fields
    # are rejected with a field-level 422 instead of being silently dropped.
    model_config = ConfigDict(extra="forbid")

    # Structured constraints (minimum/maximum/multipleOf in the OpenAPI
    # schema) let generated clients see the valid envelope up front instead
    # of only "integer". The before-validators below still run first, so
    # callers keep receiving the domain error messages from the core.
    total_pages: int = Field(
        ge=MIN_PAGES,
        le=MAX_PAGES,
        multiple_of=PAGES_PER_SHEET,
        description="Booklet page count: integer, 4..128, divisible by 4.",
    )
    page_number: int = Field(
        ge=1,
        description="Page to locate: strict integer, 1..total_pages.",
    )

    @field_validator("total_pages", mode="before")
    @classmethod
    def _validate_total_pages(cls, value: object) -> int:
        try:
            return validate_total_pages(value)
        except InvalidTotalPages as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("page_number", mode="before")
    @classmethod
    def _validate_page_number(cls, value: object, info: ValidationInfo) -> int:
        # Validators run in field declaration order, so an accepted
        # total_pages is already in info.data. When total_pages itself is
        # invalid the upper bound is unknowable, but strict-integer typing
        # and the one-based lower bound still apply, so an out-of-range page
        # (e.g. 0) is reported alongside the total_pages error instead of
        # being silently dropped.
        if "total_pages" not in info.data:
            try:
                return validate_page_number_lower_bound(value)
            except InvalidPageNumber as exc:
                raise ValueError(str(exc)) from exc
        try:
            return validate_page_number(value, info.data["total_pages"])
        except InvalidPageNumber as exc:
            raise ValueError(str(exc)) from exc


class QuoteRequest(BaseModel):
    # Same strict boundary as the imposition requests: typos and
    # unrelated fields are rejected with a field-level 422 instead of
    # being silently dropped.
    model_config = ConfigDict(extra="forbid")

    # Structured constraints (minimum/maximum/multipleOf in the OpenAPI
    # schema) let generated clients see the valid envelope up front. The
    # before-validators below still run first, so callers keep receiving
    # the domain error messages from the core.
    total_pages: int = Field(
        ge=MIN_PAGES,
        le=MAX_PAGES,
        multiple_of=PAGES_PER_SHEET,
        description="Booklet page count: integer, 4..128, divisible by 4.",
    )
    print_run: int = Field(
        ge=MIN_PRINT_RUN,
        description="Number of booklets to print: strict integer, at least 1.",
    )
    unit_price: Decimal = Field(
        ge=0,
        # The generated Decimal schema would advertise any JSON number,
        # but floats are rejected: only exact forms are accepted.
        json_schema_extra={
            "anyOf": [{"type": "string"}, {"type": "integer"}],
            "minimum": 0,
        },
        description=(
            "Price per sheet of paper: exact decimal given as a string "
            '(e.g. "1.25") or an integer. JSON floats are rejected '
            "because they cannot represent decimal prices exactly."
        ),
    )
    loss_rate: Decimal = Field(
        ge=0,
        description=(
            "Waste/loss rate as a non-negative decimal, e.g. 0.05 for 5%. "
            "The loss sheet count is rounded up to a whole sheet."
        ),
    )

    @field_validator("total_pages", mode="before")
    @classmethod
    def _validate_total_pages(cls, value: object) -> int:
        # Single source of truth is the pure core; its ValueError subclass
        # is reported by Pydantic as a field error on total_pages (HTTP 422).
        try:
            return validate_total_pages(value)
        except InvalidTotalPages as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("print_run", mode="before")
    @classmethod
    def _validate_print_run(cls, value: object) -> int:
        try:
            return validate_print_run(value)
        except InvalidPrintRun as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("unit_price", mode="before")
    @classmethod
    def _validate_unit_price(cls, value: object) -> Decimal:
        try:
            return validate_unit_price(value)
        except InvalidUnitPrice as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("loss_rate", mode="before")
    @classmethod
    def _validate_loss_rate(cls, value: object) -> Decimal:
        try:
            return validate_loss_rate(value)
        except InvalidLossRate as exc:
            raise ValueError(str(exc)) from exc


class QuoteResponse(BaseModel):
    quote_id: str = Field(description="Public quote number, e.g. Q-000001.")
    status: Literal["pending", "confirmed"] = Field(
        description="Lifecycle state: pending until confirmed."
    )
    total_pages: int = Field(description="Echoed validated booklet page count.")
    print_run: int = Field(description="Echoed validated print run.")
    unit_price: str = Field(description="Exact decimal price per sheet.")
    loss_rate: str = Field(description="Exact decimal loss rate.")
    sheets_per_booklet: int = Field(
        description="Folded sheets per booklet: total_pages / 4."
    )
    base_sheets: int = Field(
        description="Sheets before waste: sheets_per_booklet * print_run."
    )
    loss_sheets: int = Field(
        description="Waste sheets, rounded up: ceil(base_sheets * loss_rate)."
    )
    total_sheets: int = Field(description="base_sheets + loss_sheets.")
    total_amount: str = Field(
        description="Total paper cost: total_sheets * unit_price, 2 places."
    )
    created_at: str = Field(description="ISO 8601 creation timestamp (UTC).")
    confirmed_at: str | None = Field(
        description="ISO 8601 confirmation timestamp (UTC), null while pending."
    )


class SheetOut(BaseModel):
    index: int = Field(description="Sheet number from outside, 0-based.")
    front: list[int] = Field(description="Front side pages, left to right.")
    back: list[int] = Field(description="Back side pages, left to right.")


class QuoteCompareRequest(BaseModel):
    # Same strict boundary as the other requests: a typo or an unrelated
    # field is a field-level 422 instead of being silently dropped.
    model_config = ConfigDict(extra="forbid")

    baseline_quote_id: str = Field(
        description="Number of the quote treated as the baseline, e.g. Q-000001."
    )
    candidate_quote_id: str = Field(
        description="Number of the alternative quote, e.g. Q-000002."
    )

    @field_validator("baseline_quote_id", "candidate_quote_id", mode="before")
    @classmethod
    def _reject_non_strings(cls, value: object) -> str:
        # Quote numbers are strings: bool (an int subclass), ints, null,
        # lists, ... are boundary type errors rather than unknown rows.
        if not isinstance(value, str):
            raise ValueError("quote_id must be a string.")
        return value

    @model_validator(mode="after")
    def _reject_same_quote(self) -> QuoteCompareRequest:
        # Comparing a quote against itself produces zero-everything noise;
        # the boundary refuses it before any storage lookup. The check is
        # on the parsed numbers so "Q-000001" and "1" are also the same.
        baseline_id = parse_quote_id(self.baseline_quote_id)
        candidate_id = parse_quote_id(self.candidate_quote_id)
        if (
            baseline_id is not None
            and candidate_id is not None
            and baseline_id == candidate_id
        ):
            raise ValueError(
                "baseline_quote_id and candidate_quote_id must be different."
            )
        return self


class QuoteCompareResponse(BaseModel):
    baseline_quote_id: str = Field(description="Echoed baseline quote number.")
    candidate_quote_id: str = Field(description="Echoed candidate quote number.")
    sheet_difference: int = Field(
        description="Baseline minus candidate total paper sheets (signed)."
    )
    amount_difference: str = Field(
        description="Baseline minus candidate total amount, 2 places (signed)."
    )
    lower_quote_id: str | None = Field(
        description="The cheaper quote's number; null when amounts tie."
    )


class ImpositionResponse(BaseModel):
    total_pages: int
    sheet_count: int
    sheets: list[SheetOut]


class LocateResponse(BaseModel):
    total_pages: int = Field(description="Echoed validated booklet page count.")
    page_number: int = Field(description="Echoed validated located page.")
    sheet_index: int = Field(description="Sheet number from outside, 0-based.")
    side: Literal["front", "back"] = Field(
        description="Sheet side carrying the page."
    )
    position: Literal["left", "right"] = Field(
        description="Left/right slot of the page on that side."
    )
    partner_page: int = Field(
        description="The other page printed on the same side, left to right."
    )


__all__ = [
    "ImpositionRequest",
    "ImpositionResponse",
    "LocateRequest",
    "LocateResponse",
    "QuoteCompareRequest",
    "QuoteCompareResponse",
    "QuoteRequest",
    "QuoteResponse",
    "SheetOut",
    "MIN_PAGES",
    "MAX_PAGES",
]
