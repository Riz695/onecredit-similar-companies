"""The database rules: latest-wins, idempotency, rejections, and the similarity query."""

from datetime import UTC, datetime, timedelta

from app.repository import ApplyResult, apply_event, find_similar, get_company, record_rejection
from psycopg import AsyncConnection

from app.events import CompanyStruckOff, CompanyUpserted, InvalidEvent
from tests.fakes import unit_vector

T0 = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)


def upsert(registration_number: str = "SYN-000001", at: datetime = T0, **fields: object) -> CompanyUpserted:
    values: dict = {
        "company_name": "Kopi Sdn. Bhd.",
        "description": "Coffee shops",
        "industry": "Food & Beverage",
        "state": "Selangor",
    }
    values.update(fields)
    return CompanyUpserted(registration_number=registration_number, updated_at=at, **values)


async def company_row(db: AsyncConnection, registration_number: str) -> dict:
    cursor = await db.execute(
        "SELECT company_name, description, status, source_updated_at, last_message_id,"
        " embedding IS NULL AS no_embedding"
        " FROM companies WHERE registration_number = %s",
        (registration_number,),
    )
    row = await cursor.fetchone()
    assert row is not None
    names = [column.name for column in cursor.description]
    return dict(zip(names, row, strict=True))


async def count(db: AsyncConnection, table: str) -> int:
    cursor = await db.execute(f"SELECT count(*) FROM {table}")  # table names are literals in these tests
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


class TestApplyEvent:
    async def test_inserts_a_new_company(self, db: AsyncConnection) -> None:
        result = await apply_event(db, "m1", upsert(), unit_vector(1))
        assert result is ApplyResult.APPLIED
        row = await company_row(db, "SYN-000001")
        assert row["company_name"] == "Kopi Sdn. Bhd."
        assert row["status"] == "active"
        assert row["last_message_id"] == "m1"
        assert not row["no_embedding"]

    async def test_newer_event_replaces_older(self, db: AsyncConnection) -> None:
        await apply_event(db, "m1", upsert(at=T0, description="Coffee shops"), unit_vector(1))
        result = await apply_event(db, "m2", upsert(at=T0 + timedelta(hours=1), description="Bakery"), unit_vector(2))
        assert result is ApplyResult.APPLIED
        assert (await company_row(db, "SYN-000001"))["description"] == "Bakery"

    async def test_older_event_never_overwrites_newer(self, db: AsyncConnection) -> None:
        await apply_event(db, "m2", upsert(at=T0 + timedelta(hours=1), description="Bakery"), unit_vector(2))
        result = await apply_event(db, "m1", upsert(at=T0, description="Coffee shops"), unit_vector(1))
        assert result is ApplyResult.STALE
        row = await company_row(db, "SYN-000001")
        assert (row["description"], row["last_message_id"]) == ("Bakery", "m2")

    async def test_same_timestamp_keeps_the_first_arrival(self, db: AsyncConnection) -> None:
        await apply_event(db, "m1", upsert(description="Coffee shops"), unit_vector(1))
        result = await apply_event(db, "m2", upsert(description="Bakery"), unit_vector(2))
        assert result is ApplyResult.STALE
        assert (await company_row(db, "SYN-000001"))["description"] == "Coffee shops"

    async def test_redelivered_message_is_a_no_op(self, db: AsyncConnection) -> None:
        await apply_event(db, "m1", upsert(), unit_vector(1))
        result = await apply_event(db, "m1", upsert(), unit_vector(1))
        assert result is ApplyResult.DUPLICATE
        assert await count(db, "companies") == 1
        assert await count(db, "processed_messages") == 1

    async def test_company_without_description_is_stored_without_embedding(self, db: AsyncConnection) -> None:
        await apply_event(db, "m1", upsert(description=None), None)
        row = await company_row(db, "SYN-000001")
        assert row["description"] is None
        assert row["no_embedding"]

    async def test_struck_off_keeps_details_and_changes_status(self, db: AsyncConnection) -> None:
        await apply_event(db, "m1", upsert(), unit_vector(1))
        result = await apply_event(db, "m2", CompanyStruckOff("SYN-000001", T0 + timedelta(hours=1)), None)
        assert result is ApplyResult.APPLIED
        row = await company_row(db, "SYN-000001")
        assert (row["status"], row["company_name"]) == ("struck_off", "Kopi Sdn. Bhd.")
        assert not row["no_embedding"]

    async def test_older_upsert_cannot_revive_a_struck_off_company(self, db: AsyncConnection) -> None:
        await apply_event(db, "m2", CompanyStruckOff("SYN-000001", T0 + timedelta(hours=1)), None)
        result = await apply_event(db, "m1", upsert(at=T0), unit_vector(1))
        assert result is ApplyResult.STALE
        assert (await company_row(db, "SYN-000001"))["status"] == "struck_off"

    async def test_newer_upsert_reactivates_a_struck_off_company(self, db: AsyncConnection) -> None:
        await apply_event(db, "m1", CompanyStruckOff("SYN-000001", T0), None)
        await apply_event(db, "m2", upsert(at=T0 + timedelta(hours=1)), unit_vector(1))
        assert (await company_row(db, "SYN-000001"))["status"] == "active"


class TestRecordRejection:
    async def test_stores_reason_and_body(self, db: AsyncConnection) -> None:
        await record_rejection(db, "m1", b"null", InvalidEvent("not_an_object", "NoneType"))
        cursor = await db.execute("SELECT message_id, reason, detail, body FROM rejected_events")
        assert await cursor.fetchall() == [("m1", "not_an_object", "not_an_object: NoneType", "null")]

    async def test_redelivered_rejection_is_stored_once(self, db: AsyncConnection) -> None:
        for _ in range(2):
            await record_rejection(db, "m1", b"null", InvalidEvent("not_an_object"))
        assert await count(db, "rejected_events") == 1

    async def test_undecodable_body_is_still_stored(self, db: AsyncConnection) -> None:
        await record_rejection(db, "m1", b"\xff\xfe", InvalidEvent("invalid_json"))
        assert await count(db, "rejected_events") == 1


class TestGetCompany:
    async def test_returns_the_card_fields(self, db: AsyncConnection) -> None:
        await apply_event(db, "m1", upsert(), unit_vector(1))
        company = await get_company(db, "SYN-000001")
        assert company is not None
        assert (company.company_name, company.industry, company.state) == (
            "Kopi Sdn. Bhd.",
            "Food & Beverage",
            "Selangor",
        )
        assert company.has_embedding

    async def test_unknown_company_is_none(self, db: AsyncConnection) -> None:
        assert await get_company(db, "SYN-999999") is None


class TestFindSimilar:
    async def seed(self, db: AsyncConnection) -> None:
        companies = {
            "SYN-000001": unit_vector(1, 0),  # the company we ask about
            "SYN-000002": unit_vector(1, 0.1),  # nearest
            "SYN-000003": unit_vector(1, 0.5),
            "SYN-000004": unit_vector(1, 2),
            "SYN-000005": unit_vector(0, 1),  # farthest
        }
        for number, vector in companies.items():
            await apply_event(db, f"m-{number}", upsert(number, company_name=f"Company {number}"), vector)

    async def test_most_similar_first_and_excludes_itself(self, db: AsyncConnection) -> None:
        await self.seed(db)
        similar = await find_similar(db, "SYN-000001", limit=10)
        assert [company.registration_number for company in similar] == [
            "SYN-000002",
            "SYN-000003",
            "SYN-000004",
            "SYN-000005",
        ]
        assert similar[0].similarity > similar[-1].similarity
        assert similar[0].company_name == "Company SYN-000002"

    async def test_respects_the_limit(self, db: AsyncConnection) -> None:
        await self.seed(db)
        assert len(await find_similar(db, "SYN-000001", limit=2)) == 2

    async def test_skips_struck_off_and_companies_without_embedding(self, db: AsyncConnection) -> None:
        await self.seed(db)
        await apply_event(db, "x1", CompanyStruckOff("SYN-000002", T0 + timedelta(hours=1)), None)
        await apply_event(db, "x2", upsert("SYN-000006", description=None), None)
        numbers = [company.registration_number for company in await find_similar(db, "SYN-000001", limit=10)]
        assert numbers == ["SYN-000003", "SYN-000004", "SYN-000005"]

    async def test_ties_are_broken_by_registration_number(self, db: AsyncConnection) -> None:
        for number in ["SYN-000009", "SYN-000001", "SYN-000007", "SYN-000008"]:
            await apply_event(db, f"m-{number}", upsert(number), unit_vector(1))
        numbers = [company.registration_number for company in await find_similar(db, "SYN-000001", limit=10)]
        assert numbers == ["SYN-000007", "SYN-000008", "SYN-000009"]

    async def test_company_without_embedding_has_no_similar(self, db: AsyncConnection) -> None:
        await self.seed(db)
        await apply_event(db, "x", upsert("SYN-000006", description=None), None)
        assert await find_similar(db, "SYN-000006", limit=4) == []
