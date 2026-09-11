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
    validate_page_number,
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

    total_pages: int = Field(
        description="Booklet page count: integer, 4..128, divisible by 4."
    )
    page_number: int = Field(
        description="Page to locate: strict integer, 1..total_pages."
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
        # invalid, only its field error is reported; the range check against
        # an unknown bound is skipped while strict-integer typing still
        # applies so both bad fields stay locatable when independent.
        if "total_pages" not in info.data:
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidPageNumber("page_number must be an integer.")
            return value
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
