"""FastAPI application exposing the saddle-stitched imposition endpoint."""

from __future__ import annotations

from fastapi import FastAPI

from .imposition import impose
from .schemas import ImpositionRequest, ImpositionResponse, SheetOut

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
