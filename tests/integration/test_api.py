"""The HTTP contract the portal relies on, including every error response."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from psycopg import AsyncConnection

from app.api import create_app
from app.events import CompanyStruckOff, CompanyUpserted
from app.repository import apply_event
from tests.fakes import unit_vector

T0 = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)


def company(number: str, name: str, description: str | None = "Coffee shops") -> CompanyUpserted:
    return CompanyUpserted(number, T0, name, description, "Food & Beverage", "Selangor")


@pytest.fixture
async def client(db: AsyncConnection, database_url: str) -> AsyncIterator[httpx.AsyncClient]:
    await apply_event(db, "m1", company("SYN-001550", "Sinar Teguh Bakery Sdn. Bhd."), unit_vector(1, 0))
    await apply_event(db, "m2", company("SYN-000539", "Seri Sejahtera Food Sdn. Bhd."), unit_vector(1, 0.2))
    await apply_event(db, "m3", company("SYN-000002", "Everbright Pacific Food Sdn. Bhd."), unit_vector(1, 0.4))
    await apply_event(db, "m4", company("SYN-000003", "Pacific Everbright Logistik PLT"), unit_vector(0, 1))
    await apply_event(db, "m5", company("SYN-000004", "No Description Sdn. Bhd.", None), None)
    app = create_app(database_url)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_returns_the_company_and_its_most_similar_cards(client: httpx.AsyncClient) -> None:
    response = await client.get("/companies/SYN-001550/similar", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["company"] == {
        "registrationNumber": "SYN-001550",
        "companyName": "Sinar Teguh Bakery Sdn. Bhd.",
        "description": "Coffee shops",
        "industry": "Food & Beverage",
        "state": "Selangor",
        "status": "active",
    }
    assert [card["registrationNumber"] for card in body["similar"]] == ["SYN-000539", "SYN-000002"]
    assert set(body["similar"][0]) == {"registrationNumber", "companyName", "industry", "state", "similarity"}
    assert body["similar"][0]["similarity"] > body["similar"][1]["similarity"]
    assert body["reason"] is None


async def test_default_limit_is_four(client: httpx.AsyncClient, db: AsyncConnection) -> None:
    for index in range(10):
        number = f"SYN-1000{index:02d}"
        await apply_event(db, f"x{index}", company(number, f"Kopi {index}"), unit_vector(1, 0.01 * index))
    response = await client.get("/companies/SYN-001550/similar")
    assert len(response.json()["similar"]) == 4


async def test_registration_number_is_normalized(client: httpx.AsyncClient) -> None:
    response = await client.get("/companies/syn-001550/similar")
    assert response.status_code == 200
    assert response.json()["company"]["registrationNumber"] == "SYN-001550"


async def test_struck_off_companies_are_not_recommended(client: httpx.AsyncClient, db: AsyncConnection) -> None:
    await apply_event(db, "x", CompanyStruckOff("SYN-000539", T0 + timedelta(hours=1)), None)
    numbers = [
        card["registrationNumber"] for card in (await client.get("/companies/SYN-001550/similar")).json()["similar"]
    ]
    assert "SYN-000539" not in numbers


async def test_company_without_description_gets_an_empty_list_and_a_reason(client: httpx.AsyncClient) -> None:
    response = await client.get("/companies/SYN-000004/similar")
    assert response.status_code == 200
    body = response.json()
    assert body["company"]["companyName"] == "No Description Sdn. Bhd."
    assert body["similar"] == []
    assert body["reason"] == "no_description"


async def test_unknown_company_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/companies/SYN-999999/similar")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "company_not_found", "message": "No company with registration number SYN-999999."}
    }


@pytest.mark.parametrize(
    "path",
    [
        "/companies/SYN-001550/similar?limit=0",
        "/companies/SYN-001550/similar?limit=21",
        "/companies/SYN-001550/similar?limit=four",
        "/companies/not-a-number/similar",
    ],
)
async def test_invalid_parameters_are_422(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_parameter"
    assert error["message"]


async def test_unknown_route_uses_the_same_error_body(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
