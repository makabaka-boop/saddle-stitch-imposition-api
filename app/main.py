"""FastAPI application: saddle-stitched imposition and paper-cost quotes."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request

from .imposition import impose, locate_page
from .quote_repository import (
    DEFAULT_DB_PATH,
    QuoteAlreadyConfirmed,
    QuoteNotFound,
    QuoteRepository,
)
from .quotes import (
    IncompatibleQuotes,
    Quote,
    build_snapshot,
    compare_quotes,
    decimal_to_str,
    parse_quote_id,
)
from .schemas import (
    ImpositionRequest,
    ImpositionResponse,
    LocateRequest,
    LocateResponse,
    QuoteCompareRequest,
    QuoteCompareResponse,
    QuoteRequest,
    QuoteResponse,
    SheetOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The SQLite repository lives in this same process: pending
    # migrations are applied at startup, before the first request.
    repository = QuoteRepository(os.environ.get("QUOTE_DB_PATH", DEFAULT_DB_PATH))
    repository.migrate()
    app.state.quote_repository = repository
    yield


app = FastAPI(
    title="Saddle-stitched Imposition API",
    version="1.2.0",
    description=(
        "Calculate the unique outside-in front/back page order for a "
        "saddle-stitched booklet, and price the paper for a print run "
        "as a traceable, confirmable quote."
    ),
    lifespan=lifespan,
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


def _get_quote_repository(request: Request) -> QuoteRepository:
    # Stored on app.state by the lifespan, so every request in this
    # process shares the one repository (and the one database file).
    return request.app.state.quote_repository


QuoteRepositoryDep = Annotated[QuoteRepository, Depends(_get_quote_repository)]


def _quote_response(quote: Quote) -> QuoteResponse:
    snapshot = quote.snapshot
    return QuoteResponse(
        quote_id=quote.quote_id,
        status=quote.status,
        total_pages=snapshot.total_pages,
        print_run=snapshot.print_run,
        unit_price=decimal_to_str(snapshot.unit_price),
        loss_rate=decimal_to_str(snapshot.loss_rate),
        sheets_per_booklet=snapshot.sheets_per_booklet,
        base_sheets=snapshot.base_sheets,
        loss_sheets=snapshot.loss_sheets,
        total_sheets=snapshot.total_sheets,
        total_amount=decimal_to_str(snapshot.total_amount),
        created_at=quote.created_at,
        confirmed_at=quote.confirmed_at,
    )


@app.post(
    "/quotes",
    response_model=QuoteResponse,
    status_code=201,
    tags=["quotes"],
    summary="Price the paper for one print run and persist the quote snapshot",
)
def create_quote(
    request: QuoteRequest, repository: QuoteRepositoryDep
) -> QuoteResponse:
    snapshot = build_snapshot(
        request.total_pages,
        request.print_run,
        request.unit_price,
        request.loss_rate,
    )
    quote = repository.insert(snapshot)
    return _quote_response(quote)


@app.post(
    "/quotes/{quote_id}/confirm",
    response_model=QuoteResponse,
    status_code=200,
    tags=["quotes"],
    summary="Adopt a pending quote; only pending quotes can be confirmed",
    responses={
        404: {"description": "Unknown quote number."},
        409: {"description": "Quote is already confirmed."},
    },
)
def confirm_quote(quote_id: str, repository: QuoteRepositoryDep) -> QuoteResponse:
    row_id = parse_quote_id(quote_id)
    if row_id is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown quote_id: {quote_id!r}."
        )
    try:
        quote = repository.confirm(row_id)
    except QuoteNotFound:
        raise HTTPException(
            status_code=404, detail=f"Unknown quote_id: {quote_id!r}."
        ) from None
    except QuoteAlreadyConfirmed:
        raise HTTPException(
            status_code=409, detail=f"Quote {quote_id!r} is already confirmed."
        ) from None
    return _quote_response(quote)


@app.get(
    "/quotes/{quote_id}",
    response_model=QuoteResponse,
    status_code=200,
    tags=["quotes"],
    summary="Re-read a persisted quote snapshot by its number",
    responses={404: {"description": "Unknown quote number."}},
)
def get_quote(quote_id: str, repository: QuoteRepositoryDep) -> QuoteResponse:
    row_id = parse_quote_id(quote_id)
    quote = repository.get(row_id) if row_id is not None else None
    if quote is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown quote_id: {quote_id!r}."
        )
    return _quote_response(quote)


@app.post(
    "/quotes/compare",
    response_model=QuoteCompareResponse,
    status_code=200,
    tags=["quotes"],
    summary="Diff two persisted quotes for the same print job",
    responses={
        404: {"description": "A submitted quote number is unknown."},
        409: {"description": "The quotes price different jobs."},
    },
)
def compare_quote_snapshots(
    request: QuoteCompareRequest, repository: QuoteRepositoryDep
) -> QuoteCompareResponse:
    # The handler only parses numbers, assembles domain objects from the
    # repository batch read, and maps domain failures onto HTTP codes:
    # no pricing or compatibility rules live here.
    baseline_id = parse_quote_id(request.baseline_quote_id)
    if baseline_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown quote_id: {request.baseline_quote_id!r}.",
        )
    candidate_id = parse_quote_id(request.candidate_quote_id)
    if candidate_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown quote_id: {request.candidate_quote_id!r}.",
        )
    # One batch read keeps the input order: result[0] is the baseline
    # even when its number is larger than the candidate's.
    baseline, candidate = repository.get_many((baseline_id, candidate_id))
    if baseline is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown quote_id: {request.baseline_quote_id!r}.",
        )
    if candidate is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown quote_id: {request.candidate_quote_id!r}.",
        )
    try:
        comparison = compare_quotes(baseline, candidate)
    except IncompatibleQuotes as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return QuoteCompareResponse(
        baseline_quote_id=comparison.baseline_quote_id,
        candidate_quote_id=comparison.candidate_quote_id,
        sheet_difference=comparison.sheet_difference,
        amount_difference=decimal_to_str(comparison.amount_difference),
        lower_quote_id=comparison.lower_quote_id,
    )
