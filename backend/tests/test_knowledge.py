import asyncio
from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.knowledge import Knowledge, build_records, load_kit
from app.main import app

DATASETS = Path(settings.datasets_dir)


def test_loader_counts():
    kinds = Counter(r["kind"] for r in build_records(DATASETS))
    assert kinds["scenario"] == 40
    assert kinds["system_intent"] == 3
    assert kinds["action"] == 31
    assert kinds["slot"] == 43
    assert kinds["client"] == 11
    assert kinds["claim"] == 4
    assert kinds["kb"] > 0


def test_records_and_search():
    async def run():
        engine = create_async_engine(settings.database_url)
        try:
            async with async_sessionmaker(engine)() as session:
                await load_kit(session, DATASETS, reset=True)
                kb = Knowledge(session)

                client = await kb.records.find_client(phone="8 701 000 00 04")
                assert client and client.client_id == "C004"
                claims = await kb.records.claims(client.client_id)
                assert [c.claim_number for c in claims] == ["CL-500311"]

                exact = await kb.search.kb_lookup("claims.submission")
                assert (
                    exact and exact.source == "kb_lookup" and exact.source_id == "claims.submission"
                )
                fuzzy = await kb.search.kb_lookup("claim document submission")
                assert fuzzy and fuzzy.source_id == "claims.submission"

                hits = await kb.search.query("Өтінішім қандай күйде", kinds=["scenario"], limit=3)
                assert "SC17" in [h.key for h in hits]

                assert len(await kb.catalog.scenario_ids()) == 43
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_crud_scenario_roundtrip():
    with TestClient(app) as client:
        card = client.get("/kit/scenario/SC17").json()

        broken = {**card, "scenario_id": "SC41", "actions": ["no_such_action"]}
        assert client.put("/kit/scenario/SC41", json=broken).status_code == 422
        assert client.put("/kit/scenario/SC41", json={"scenario_id": "SC41"}).status_code == 422

        new = {**card, "scenario_id": "SC41", "slug": "claim_status_copy"}
        assert client.put("/kit/scenario/SC41", json=new).status_code == 200
        ids = [s["scenario_id"] for s in client.get("/kit/scenario").json()]
        assert "SC41" in ids and len(ids) == 41

        assert client.post("/kit/reload?reset=true").status_code == 200
        assert client.get("/kit/scenario/SC41").status_code == 404
