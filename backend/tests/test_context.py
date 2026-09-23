import asyncio

import pytest

from app.context import ContextPatch, Contexts, Fact, MemoryStore


def run(coro):
    return asyncio.run(coro)


async def new_call(client_id: str | None = "C004"):
    ctx = Contexts(MemoryStore())
    s = await ctx.start_call("s1")
    turn = await ctx.begin_turn(s.session_id, "что с заявлением?", "ru")
    if client_id:
        async with ctx.mutate("s1", turn_id=turn) as m:
            m.set_client(client_id)
            m.switch_topic("SC17")
    return ctx, turn


def fact(key="claim.status", source="get_claim"):
    return Fact(key=key, value="documents_requested", source=source, source_id="CL-500311")


def patch(snap, **kw):
    data = {
        "session_id": snap.session_id,
        "generation": snap.generation,
        "based_on_turn_id": snap.turn_id,
        "base_context_version": snap.context_version,
        "client_id": snap.client_id,
        "facts": [fact("claim.document_submission", "kb_lookup")],
    }
    return ContextPatch(**(data | kw))


def test_version_grows_only_on_significant_changes():
    async def go():
        ctx, turn = await new_call()
        v = (await ctx.snapshot("s1")).context_version
        await ctx.log("s1", turn, "timing", "router", {"router_ms": 420})
        await ctx.add_reply("s1", turn, "Назовите телефон", "ru")
        await ctx.begin_turn("s1", "+77010000004", "ru")
        async with ctx.mutate("s1") as m:
            m.record_routing({"scenarios": ["SC17"]}, 0.9)
        assert (await ctx.snapshot("s1")).context_version == v

        async with ctx.mutate("s1") as m:
            m.set_slots("SC17", {"claim_number": "CL-500311", "phone": None})
            m.add_facts([fact()])
        snap = await ctx.snapshot("s1")
        assert snap.context_version == v + 1  # одна транзакция — одна версия
        assert snap.facts[0].context_version == v + 1
        board = await ctx.board("s1")
        assert all(e.context_version <= v + 1 for e in board)

    run(go())


def test_mutation_rolls_back_on_error():
    async def go():
        ctx, _ = await new_call()
        before = await ctx.snapshot("s1")
        with pytest.raises(RuntimeError):
            async with ctx.mutate("s1") as m:
                m.add_facts([fact()])
                raise RuntimeError
        assert await ctx.snapshot("s1") == before

    run(go())


def test_patch_applied_stale_rejected():
    async def go():
        ctx, _ = await new_call()
        snap = await ctx.snapshot("s1")

        assert (await ctx.apply_patch(patch(snap, client_id="C005"))).status == "rejected"
        assert (await ctx.apply_patch(patch(snap, generation=99))).status == "rejected"

        res = await ctx.apply_patch(patch(snap))
        assert res.status == "applied" and res.context_version == snap.context_version + 1
        assert (await ctx.snapshot("s1")).facts[-1].origin == "background"

        # тот же патч по старой версии уже устарел
        assert (await ctx.apply_patch(patch(snap))).status == "stale"
        statuses = [e.payload["status"] for e in await ctx.board("s1") if e.type == "patch"]
        assert statuses == ["rejected", "rejected", "applied", "stale"]

    run(go())


def test_confirmation_bound_to_params_and_consumed_once():
    async def go():
        ctx, _ = await new_call()
        params = {"client_id": "C004", "email": "a@b.kz"}

        async with ctx.mutate("s1") as m:
            m.request_confirmation("update_contact", params)
            m.set_slots("SC17", {"email": "other@b.kz"})  # параметры изменились
        assert (await ctx.snapshot("s1")).pending_confirmation is None
        assert not await ctx.consume_confirmation("s1", "update_contact", params)

        async with ctx.mutate("s1") as m:
            m.request_confirmation("update_contact", params)
            m.cancel_confirmation()  # «пока не меняйте»
        assert not await ctx.consume_confirmation("s1", "update_contact", params)

        async with ctx.mutate("s1") as m:
            m.request_confirmation("update_contact", params)
        assert not await ctx.consume_confirmation("s1", "update_contact", params | {"email": "x"})
        results = await asyncio.gather(
            *[ctx.consume_confirmation("s1", "update_contact", params) for _ in range(3)]
        )
        assert results.count(True) == 1

    run(go())


def test_new_call_and_client_change_do_not_leak():
    async def go():
        ctx, _ = await new_call()
        resets = []
        ctx.on_reset(lambda sid, gen: resets.append(gen))
        async with ctx.mutate("s1") as m:
            m.add_facts([fact()])
        gen = (await ctx.snapshot("s1")).generation

        async with ctx.mutate("s1") as m:
            m.set_client("C005")
        snap = await ctx.snapshot("s1")
        assert snap.generation == gen + 1 and snap.client_id == "C005"
        assert snap.facts == [] and snap.active_scenario is None

        snap = await ctx.start_call("s1")
        assert snap.generation == gen + 2 and snap.client_id is None and snap.history == []
        assert resets == [gen + 1, gen + 2]

    run(go())


def test_topic_stack_and_cancelled_turn():
    async def go():
        ctx, turn = await new_call()
        async with ctx.mutate("s1") as m:
            m.switch_topic("SC33")
        snap = await ctx.snapshot("s1")
        assert (snap.active_scenario, snap.pending_topics) == ("SC33", ["SC17"])
        async with ctx.mutate("s1") as m:
            assert m.finish_topic() == "SC17"
            assert m.resume_topic() == "SC17"
        assert (await ctx.snapshot("s1")).pending_topics == []

        assert ctx.is_current_turn("s1", turn)
        await ctx.cancel_turn("s1", turn)
        assert not ctx.is_current_turn("s1", turn)

    run(go())
