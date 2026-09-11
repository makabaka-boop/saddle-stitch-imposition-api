"""Pydantic boundary schemas for the imposition API.

All invalid input is rejected at this boundary so handlers only ever run
with a validated page count and never emit a partial imposition.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .imposition import InvalidTotalPages, MIN_PAGES, MAX_PAGES, validate_total_pages


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


class SheetOut(BaseModel):
    index: int = Field(description="Sheet number from outside, 0-based.")
    front: list[int] = Field(description="Front side pages, left to right.")
    back: list[int] = Field(description="Back side pages, left to right.")


class ImpositionResponse(BaseModel):
    total_pages: int
    sheet_count: int
    sheets: list[SheetOut]


__all__ = [
    "ImpositionRequest",
    "ImpositionResponse",
    "SheetOut",
    "MIN_PAGES",
    "MAX_PAGES",
]
