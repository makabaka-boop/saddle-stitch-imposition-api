"""Pydantic boundary schemas for the imposition API.

All invalid input is rejected at this boundary so handlers only ever run
with a validated page count and never emit a partial imposition.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

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


class SheetOut(BaseModel):
    index: int = Field(description="Sheet number from outside, 0-based.")
    front: list[int] = Field(description="Front side pages, left to right.")
    back: list[int] = Field(description="Back side pages, left to right.")


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
    "SheetOut",
    "MIN_PAGES",
    "MAX_PAGES",
]
