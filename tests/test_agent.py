import json
import shutil
import wave
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from training_bot.agent import Agent, Reply, only_unweighted_notation, tool_definitions
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


async def test_batch_search_and_grouped_workout_expand_before_confirmation(journal):
    data = workout()
    data['gym'] = 'ReZone'
    first = dict(data['sets'][0], count=4, raw_name='Синтетический жим',
                 weight=15, reps=10, note='на каждую руку/ногу')
    second = dict(data['sets'][0], count=2, weight=20, reps=8)
    data['sets'] = [first, second]
    agent, client = fake_agent([
        model_response('search_exercises_batch', {'queries': ['Синтетический жим']}),
        model_response('prepare_workout', data),
    ])
    reply = await agent.answer(journal, 'Синтетический жим 15х(10х2) /4, 20х8 /2', 'group:1')
    assert reply.draft_id
    assert count_sets(journal) == 0
    payload = journal.context()['pending_draft']['payload']
    assert len(payload['sets']) == 6
    assert all('count' not in item for item in payload['sets'])
    assert [item['weight'] for item in payload['sets']] == [15] * 4 + [20] * 2
    assert all(item['note'] == 'на каждую руку/ногу' for item in payload['sets'][:4])
    assert journal.confirm(reply.draft_id)['saved'] == 6
    assert count_sets(journal) == 6


async def test_group_limit_applies_to_expanded_sets(journal):
    data = workout()
    data['sets'] = [dict(data['sets'][0], count=31)] * 2
    agent, client = fake_agent([model_response('prepare_workout', data),
                                model_response(text='Разделите тренировку')])
    reply = await agent.answer(journal, 'Жим, 62 подхода', 'group:limit')
    assert reply.draft_id is None
    assert journal.context()['pending_draft'] is None
    assert count_sets(journal) == 0


async def test_complex_rounds_without_weight_or_reps_can_be_previewed_and_saved(journal):
    with journal.db.session() as session:
        session.add(Exercise(code='AB06', name='Комплекс экстензия-пресс'))
    data = workout()
    data.update(week=2, program_session=3)
    data['sets'] = [dict(data['sets'][0], exercise_code='AB06',
                        raw_name='Комплекс экстензия-пресс', weight=None, reps=None, count=3)]
    agent, client = fake_agent([model_response('prepare_workout', data)])
    reply = await agent.answer(journal, 'Комплекс экстензия-пресс /3', 'complex:1')
    assert reply.draft_id
    assert 'Комплекс экстензия-пресс\n/3\n' in reply.text
    assert 'неделя 2' in reply.text and 'занятие 3' in reply.text
    journal.confirm(reply.draft_id)
    assert count_sets(journal) == 3
    last = Application(agent.settings, journal.db, agent).last(journal)
    assert last.text.count('без веса и повторений') == 3


async def test_other_exercises_still_require_reps_or_time(journal):
    data = workout()
    data['sets'][0].update(weight=None, reps=None)
    agent, client = fake_agent([model_response('prepare_workout', data),
                                model_response(text='Укажите повторения')])
    reply = await agent.answer(journal, 'Синтетический жим /3', 'empty:1')
    assert reply.draft_id is None
    assert count_sets(journal) == 0


async def test_truncated_model_response_never_executes_write(journal):
    response = model_response('prepare_workout', workout())
    response.status = 'incomplete'
    agent, client = fake_agent([response])
    reply = await agent.answer(journal, 'Тренировка', 'truncated:1')
    assert 'оборвался' in reply.text
    assert reply.draft_id is None
    assert journal.context()['pending_draft'] is None


async def test_parsed_workout_uses_only_model_exercise_selection(journal):
    text = '01.09.2026\nТренировка 2 (неделя 1)\n1. Синтетический жим\nх 6 х 5\n20х(10х2) /2'
    agent, client = fake_agent([model_response('prepare_parsed_workout', {
        'exercises': [{'exercise_index': 0, 'exercise_code': 'SYN'}]})])
    reply = await agent.answer(journal, text, 'parsed:1')
    assert reply.draft_id
    payload = journal.context()['pending_draft']['payload']
    assert len(payload['sets']) == 2
    assert all(row['reps'] == 10 for row in payload['sets'])
    assert all(row['note'] == 'на каждую руку/ногу' for row in payload['sets'])
    assert payload['original'] == text
    tools = client.responses.create.call_args.kwargs['tools']
    assert 'prepare_workout' not in {tool['name'] for tool in tools}


async def test_explicit_alternative_is_asked_before_model_then_whole_entry_is_recovered(journal):
    text = '01.09.2026\nТренировка 2 (неделя 1)\n1. Жим со штангой или гантелями\n20х10 /3'
    agent, client = fake_agent([model_response('prepare_parsed_workout', {
        'exercises': [{'exercise_index': 0, 'exercise_code': 'SYN'}]})])
    first = await agent.answer(journal, text, 'choice:1')
    assert first.draft_id is None
    client.responses.create.assert_not_called()
    reply = await agent.answer(journal, 'Со штангой', 'choice:2')
    assert reply.draft_id
    payload = journal.context()['pending_draft']['payload']
    assert len(payload['sets']) == 3
    assert text in payload['original'] and 'Со штангой' in payload['original']


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


async def test_allowed_secondary_user_cannot_get_owner_spreadsheet(journal):
    agent, client = fake_agent([])
    agent.settings.allowed_telegram_ids = '99'
    agent.settings.google_spreadsheet_id = 'private_owner_sheet_id'
    app = Application(agent.settings, journal.db, agent)
    message = NS(chat=NS(type='private', id=99), from_user=NS(id=99), message_id=1,
                 text='/table', answer=AsyncMock())
    await app.handle_message(message)
    assert 'private_owner_sheet_id' not in message.answer.call_args.args[0]
    assert 'владельца' in message.answer.call_args.args[0]


async def test_daily_budget_prevents_model_call(journal):
    agent, client = fake_agent([])
    from datetime import datetime
    journal.log_usage(datetime.now(agent.settings.tz).date(), "test", input_tokens=150000)
    result = await agent.answer(journal, "покажи рекорд", "message:42:3")
    assert "лимит" in result.text
    assert client.responses.create.call_count == 0


@pytest.mark.parametrize("text", [
    "Тестовый жим х 6 х 5", "26.10.2026\nТестовый жим х 6 х 5",
    "26.10.2026 в Орёл\nТестовый жим x6 /5\nДругой жим ×8×3",
])
async def test_only_unweighted_rows_skip_without_model_or_draft(journal, text):
    agent, client = fake_agent([])
    reply = await agent.answer(journal, text, "skip:1")
    assert "пропущены" in reply.text
    assert reply.draft_id is None
    assert journal.context()["pending_draft"] is None
    assert count_sets(journal) == 0
    client.responses.create.assert_not_called()


@pytest.mark.parametrize("text", [
    "", "26.10.2026", "Жим 0х6 /5", "Жим -20х6 /5", "Жим 82,5х6 /5",
    "Жим 15х(10х2) /4", "Ходьба 20 минут", "Почему пропущен жим х 6 х 5?",
    "Жим х 6 х 5\nЖим 40х8 /2", "Покажи тренировку\nЖим х 6 х 5",
])
def test_skip_fast_path_leaves_valid_or_conversational_input(text):
    assert not only_unweighted_notation(text)


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
