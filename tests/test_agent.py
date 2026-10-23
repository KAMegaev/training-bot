import json
import shutil
import wave
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from training_bot.agent import Agent, Reply, tool_definitions
from training_bot.audio import transcribe
from training_bot.config import Settings
from training_bot.db import Database, Exercise
from training_bot.schemas import PrepareWorkout, QuerySets
from training_bot.service import Journal
from training_bot.telegram import Application


@pytest.fixture
def journal(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "test.db"))
    db.init()
    with db.session() as s:
        s.add(Exercise(code="SYN", name="Синтетический жим"))
    yield Journal(db, "owner")
    db.engine.dispose()


def model_response(name=None, args=None, text=""):
    output = [NS(type="function_call", name=name, arguments=json.dumps(args), call_id="call_1")] if name else []
    return NS(output=output, output_text=text, usage=NS(input_tokens=100, output_tokens=20))


def fake_agent(responses):
    client = NS(responses=NS(create=AsyncMock(side_effect=responses)))
    settings = Settings(_env_file=None, owner_telegram_id=42)
    return Agent(settings, client), client


def workout():
    return {"day": "2026-10-23", "workout_id": None, "gym": None, "trainer": None,
            "week": None, "program_session": None,
            "sets": [{"exercise_code": "SYN", "weight": 60, "reps": 10,
                      "minutes": None, "rpe": None, "note": None}]}


def count_sets(journal):
    return journal.sets(QuerySets(exercise_code=None, start=None, end=None, exclude_uncertain=False,
                                  workout_id=None, min_weight=None, limit=40, offset=0))["total_matching_sets"]


async def test_tool_loop_searches_then_prepares_without_writing(journal):
    agent, client = fake_agent([model_response("search_exercises", {"query": "жим"}),
                                model_response("prepare_workout", workout())])
    reply = await agent.answer(journal, "жим 60 на 10", "message:42:1")
    assert reply.draft_id
    assert count_sets(journal) == 0
    second = client.responses.create.call_args_list[1].kwargs
    assert any(isinstance(item, dict) and item.get("type") == "function_call_output" for item in second["input"])
    assert second["store"] is False
    assert all(tool["strict"] is True for tool in tool_definitions())
    journal.confirm(reply.draft_id)
    assert count_sets(journal) == 1


async def test_invalid_model_write_cannot_create_unknown_exercise(journal):
    data = workout()
    data["sets"][0]["exercise_code"] = "INVENTED"
    agent, client = fake_agent([model_response("prepare_workout", data), model_response(text="Уточните упражнение")])
    reply = await agent.answer(journal, "неизвестное упражнение", "message:42:2")
    assert reply.draft_id is None
    assert journal.context()["pending_draft"] is None
    assert count_sets(journal) == 0


async def test_durable_draft_recovered_without_second_api_call(journal):
    agent, client = fake_agent([model_response("prepare_workout", workout())])
    app = Application(agent.settings, journal.db, agent)
    assert app.recovered_reply(journal, "message:42:1") is None
    original = await agent.answer(journal, "жим 60 на 10", "message:42:1")
    recovered = app.recovered_reply(journal, "message:42:1")
    assert recovered.draft_id == original.draft_id
    app.complete("message:42:1", original)
    assert app.recovered_reply(journal, "message:42:1") == original
    assert client.responses.create.call_count == 1


async def test_unconfigured_user_gets_id_without_database_or_model(journal):
    agent, client = fake_agent([])
    app = Application(agent.settings, journal.db, agent)
    message = NS(chat=NS(type="private", id=99), from_user=NS(id=99), text="Покажи историю", answer=AsyncMock())
    await app.handle_message(message)
    assert "99" in message.answer.call_args.args[0]
    assert client.responses.create.call_count == 0


async def test_daily_budget_prevents_model_call(journal):
    agent, client = fake_agent([])
    from datetime import datetime
    journal.log_usage(datetime.now(agent.settings.tz).date(), "test", input_tokens=150000)
    result = await agent.answer(journal, "покажи рекорд", "message:42:3")
    assert "лимит" in result.text
    assert client.responses.create.call_count == 0


async def test_voice_converts_audio_and_cleans_temporary_files(journal, tmp_path):
    source = tmp_path / "synthetic.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    seen = []
    async def download(voice, destination):
        shutil.copyfile(source, destination)
    async def create(**kwargs):
        path = Path(kwargs["file"].name)
        assert path.suffix == ".wav"
        assert path.stat().st_size > 16000
        seen.append(path)
        return NS(text="Жим 60 на 10")
    agent, _ = fake_agent([])
    agent.client.audio = NS(transcriptions=NS(create=create))
    result = await transcribe(NS(download=download), NS(duration=1, file_size=32000), agent, journal)
    assert result == "Жим 60 на 10"
    assert not seen[0].exists()
