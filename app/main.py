"""FastAPI application exposing the saddle-stitched imposition endpoint."""

from __future__ import annotations

from fastapi import FastAPI

from .imposition import impose, locate_page
from .schemas import (
    ImpositionRequest,
    ImpositionResponse,
    LocateRequest,
    LocateResponse,
    SheetOut,
)

app = FastAPI(
    title="Saddle-stitched Imposition API",
    version="1.0.0",
    description=(
        "Calculate the unique outside-in front/back page order for a "
        "saddle-stitched booklet."
    ),
)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/imposition",
    response_model=ImpositionResponse,
    status_code=200,
    tags=["imposition"],
    summary="Compute front/back layout for every folded sheet",
)
def create_imposition(request: ImpositionRequest) -> ImpositionResponse:
    result = impose(request.total_pages)
    return ImpositionResponse(
        total_pages=result.total_pages,
        sheet_count=result.sheet_count,
        sheets=[
            SheetOut(
                index=sheet.index,
                front=[*sheet.front],
                back=[*sheet.back],
            )
            for sheet in result.sheets
        ],
    )


@app.post(
    "/imposition/locate",
    response_model=LocateResponse,
    status_code=200,
    tags=["imposition"],
    summary="Locate one page: sheet index, side, slot and same-side partner",
)
def locate_imposition_page(request: LocateRequest) -> LocateResponse:
    # Reuse the same imposition object the full layout endpoint builds; the
    # core reads the unique position straight off that arrangement.
    booklet = impose(request.total_pages)
    location = locate_page(booklet, request.page_number)
    return LocateResponse(
        total_pages=booklet.total_pages,
        page_number=location.page_number,
        sheet_index=location.sheet_index,
        side=location.side,
        position=location.position,
        partner_page=location.partner_page,
    )
