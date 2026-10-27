"""Opt-in prompt regression checks against Luna; never use the production journal."""
import os
import re
from datetime import date

import pytest

from training_bot.agent import Agent
from training_bot.config import Settings
from training_bot.db import Database, Exercise
from training_bot.schemas import PrepareWorkout, QuerySets
from training_bot.service import Journal


pytestmark = pytest.mark.skipif(
    os.environ.get("TRAINING_BOT_LIVE_EVAL") != "1", reason="Opt-in: consumes OpenAI API tokens")


@pytest.fixture
async def live(tmp_path):
    settings = Settings()
    assert settings.openai_api_key.get_secret_value(), "OPENAI_API_KEY is required"
    settings.openai_model = "gpt-6-luna"
    db = Database("sqlite:///" + str(tmp_path / "eval.sqlite3"))
    db.init()
    with db.session() as session:
        session.add_all([Exercise(code="EVAL", name="Тестовый жим"),
                         Exercise(code="CARDIO", name="Тестовая ходьба"),
                         Exercise(code="AB06", name="Комплекс экстензия-пресс")])
        session.add_all(Exercise(code=f"EVAL{i}", name=f"Тестовое упражнение {letter}")
                        for i, letter in enumerate("АБВГД", 1))
    journal = Journal(db, "synthetic-eval")
    agent = Agent(settings)
    calls = []
    create = agent.client.responses.create

    async def traced_create(**kwargs):
        response = await create(**kwargs)
        calls.extend(item.name for item in response.output if item.type == "function_call")
        return response

    agent.client.responses.create = traced_create
    try:
        yield agent, journal, calls
    finally:
        await agent.close()
        db.engine.dispose()


def all_sets(journal):
    return journal.sets(QuerySets(exercise_code=None, start=None, end=None,
                                 exclude_uncertain=False, workout_id=None, min_weight=None,
                                 limit=40, offset=0))["sets"]


@pytest.mark.parametrize("message,gym,weight,reps,count,note", [
    ("26.10.2026 в Орёл\nТестовый жим 82.5х6 /5", "ReZone", 82.5, 6, 5, None),
    ("26.10.2026\nТестовый жим 82,5x6 /5", "LikeGym", 82.5, 6, 5, None),
    ("26.10.2026 в Орле, зал ДругойЗал\nТестовый жим 20×8 /2", "ДругойЗал", 20, 8, 2, None),
    ("26.10.2026 в Орел\nТестовый жим 15х(10х2) /4, медленно", "ReZone", 15, 10, 4, "медленно"),
    ("26.10.2026\nТестовый жим х 6 х 5\nТестовый жим 40х8 /2", "LikeGym", 40, 8, 2, None),
    ("26.10.2026\nТестовый жим 0х6 /2", "LikeGym", 0, 6, 2, None),
    ("26.10.2026\nТестовый жим -20х6 /2", "LikeGym", -20, 6, 2, None),
], ids=["orel", "default-decimal-comma", "explicit-gym", "unilateral-note",
        "skip-missing-weight", "zero-is-valid", "negative-is-valid"])
async def test_notation_to_draft_and_database(live, message, gym, weight, reps, count, note):
    agent, journal, calls = live
    reply = await agent.answer(journal, message, "eval:write")
    assert reply.draft_id, reply.text
    assert min(calls.index(name) for name in ("search_exercises", "search_exercises_batch")
               if name in calls) < calls.index("prepare_workout")
    assert all_sets(journal) == []
    data = journal.context()["pending_draft"]["payload"]
    assert data["gym"] == gym
    assert data["day"] == "2026-10-26"
    assert gym in reply.text
    assert len(data["sets"]) == count
    for item in data["sets"]:
        assert item["exercise_code"] == "EVAL"
        assert item["raw_name"] == "Тестовый жим"
        assert (item["weight"], item["reps"]) == (weight, reps)
        if note:
            assert note in item["note"]
            assert "на каждую руку/ногу" in item["note"]
    journal.confirm(reply.draft_id)
    rows = all_sets(journal)
    assert len(rows) == count
    assert all((row["weight"], row["reps"], row["gym"]) == (weight, reps, gym) for row in rows)


async def test_only_missing_weight_creates_no_draft_or_search(live):
    agent, journal, calls = live
    reply = await agent.answer(journal, "26.10.2026\nТестовый жим х 6 х 5", "eval:skip")
    assert reply.draft_id is None, reply.text
    assert journal.context()["pending_draft"] is None
    assert all_sets(journal) == []
    assert calls == []


async def test_cardio_without_weight_is_retained(live):
    agent, journal, calls = live
    reply = await agent.answer(journal, "26.10.2026\nТестовая ходьба 20 минут", "eval:cardio")
    assert reply.draft_id, reply.text
    data = journal.context()["pending_draft"]["payload"]
    assert data["gym"] == "LikeGym"
    assert len(data["sets"]) == 1
    assert data["sets"][0]["minutes"] == 20
    assert data["sets"][0]["exercise_code"] == "CARDIO"


async def test_read_preserves_gym_and_unilateral_reps(live):
    agent, journal, calls = live
    item = dict(exercise_code="EVAL", raw_name="Тестовый жим", weight=15, reps=10,
                minutes=None, rpe=None, note="на каждую руку/ногу")
    draft = journal.prepare("append", PrepareWorkout(day=date(2026, 10, 20), workout_id=None,
                            gym="ИсторическийЗал", trainer=None, week=None, program_session=None,
                            sets=[item] * 4), "eval:seed", "Синтетическая история")
    journal.confirm(draft["draft_id"])
    reply = await agent.answer(journal, "Покажи прошлую тренировку", "eval:read")
    assert reply.draft_id is None
    assert "query_workouts" in calls and "query_sets" in calls
    assert "ИсторическийЗал" in reply.text
    assert "на каждую" in reply.text
    # All four stored sets must be displayed, without doubling reps to 20.
    lines = [line for line in reply.text.splitlines()
             if re.search(r"\b15\b", line) and re.search(r"\b10\b", line)]
    assert len(lines) == 4, reply.text
    assert all(len(re.findall(r"\b10\b", line)) == 1 for line in lines)
    assert len(all_sets(journal)) == 4


async def test_full_multiline_workout_uses_batch_search_and_actual_sets(live):
    agent, journal, calls = live
    message = """01.09.2026 (Орёл)
Тренировка 2 (неделя 3)
0. Комплекс экстензия-пресс
/3
1. Тестовое упражнение А
х 6 х 5
22.5х6 /3
2. Тестовое упражнение Б
х 10 х 4
35х10 /4
3. Тестовое упражнение В
х 8 х 4
16х(8х2) /2
3. Тестовое упражнение Г
х 10 х 4
12х10 /1
14х8 /3 (медленно)
4. Тестовое упражнение Д
х 6 х 4
"""
    reply = await agent.answer(journal, message, 'eval:multiline')
    assert reply.draft_id, reply.text
    assert 'search_exercises_batch' in calls
    assert len(calls) <= 4, calls
    data = journal.context()['pending_draft']['payload']
    assert (data['day'], data['gym'], data['week'], data['program_session']) == ('2026-09-01', 'ReZone', 3, 2)
    assert len(data['sets']) == 16
    assert [s['exercise_code'] for s in data['sets']].count('AB06') == 3
    assert [s['exercise_code'] for s in data['sets']].count('EVAL1') == 3
    assert [s['exercise_code'] for s in data['sets']].count('EVAL2') == 4
    assert [s['exercise_code'] for s in data['sets']].count('EVAL3') == 2
    assert [s['exercise_code'] for s in data['sets']].count('EVAL4') == 4
    assert not any(s['exercise_code'] == 'EVAL5' for s in data['sets'])
    assert all(s['reps'] == 8 and 'на каждую' in s['note']
               for s in data['sets'] if s['exercise_code'] == 'EVAL3')
    assert all(s['note'] == 'медленно' for s in data['sets'] if s['weight'] == 14)
    assert journal.confirm(reply.draft_id)['saved'] == 16
