"""The HTTP service behind the portal's "Similar companies" panel. Run with `just serve`.

Every error uses one body shape, so the portal can handle failures in one place:
`{"error": {"code": "...", "message": "..."}}`.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import repository
from app.config import load_settings
from app.db import create_pool, ensure_schema
from app.events import InvalidEvent, parse_registration_number

log = logging.getLogger("api")

DEFAULT_LIMIT = 4  # the mock shows four cards
MAX_LIMIT = 20


class CamelModel(BaseModel):
    """JSON uses camelCase, like the registry feed (`registrationNumber`, `companyName`)."""

    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, serialize_by_alias=True)


class CompanyOut(CamelModel):
    registration_number: str
    company_name: str | None
    description: str | None
    industry: str | None
    state: str | None
    status: Literal["active", "struck_off"]


class SimilarCompanyOut(CamelModel):
    """One card in the panel."""

    registration_number: str
    company_name: str | None
    industry: str | None
    state: str | None
    similarity: float  # 1 - cosine distance; higher is more similar


class SimilarCompaniesOut(CamelModel):
    company: CompanyOut
    similar: list[SimilarCompanyOut]
    # Why `similar` is empty when it is not obvious; null otherwise.
    reason: Literal["no_description"] | None = None


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def create_app(database_url: str) -> FastAPI:
    pool = create_pool(database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await ensure_schema(database_url)
        await pool.open(wait=True)
        yield
        await pool.close()

    app = FastAPI(title="Similar companies", lifespan=lifespan)

    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, error: ApiError) -> JSONResponse:
        return error_response(error.status_code, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors())
        return error_response(422, "invalid_parameter", problems)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, error: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if error.status_code == 404 else "http_error"
        return error_response(error.status_code, code, str(error.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, error: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=error)
        return error_response(500, "internal_error", "Something went wrong. Please try again.")

    @app.get("/health")
    async def health() -> dict[str, str]:
        async with pool.connection() as connection:
            await connection.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/companies/{registration_number}/similar", response_model=SimilarCompaniesOut)
    async def similar_companies(
        registration_number: Annotated[str, Path(description="For example SYN-001550 (case-insensitive)")],
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="How many cards to return")] = DEFAULT_LIMIT,
    ) -> SimilarCompaniesOut:
        """The company itself plus its most similar active companies, most similar first."""
        try:
            number = parse_registration_number(registration_number)
        except InvalidEvent as error:
            raise ApiError(
                422, "invalid_parameter", f"registration_number must look like SYN-001550, got {registration_number!r}."
            ) from error

        async with pool.connection() as connection:
            company = await repository.get_company(connection, number)
            if company is None:
                raise ApiError(404, "company_not_found", f"No company with registration number {number}.")
            similar = await repository.find_similar(connection, number, limit) if company.has_embedding else []

        return SimilarCompaniesOut(
            company=CompanyOut.model_validate(company, from_attributes=True),
            similar=[SimilarCompanyOut.model_validate(card, from_attributes=True) for card in similar],
            reason=None if company.has_embedding else "no_description",
        )

    return app


def app() -> FastAPI:
    """Factory for uvicorn (`uvicorn app.api:app --factory`)."""
    logging.basicConfig(level=logging.INFO)
    return create_app(load_settings().database_url)
