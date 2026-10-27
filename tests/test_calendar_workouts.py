import json
from datetime import date, datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from training_bot.agent import Agent
from training_bot.config import Settings
from training_bot.db import Conversation, Database, Exercise, Workout
from training_bot.notation import workout_day_in_text
from training_bot.schemas import PrepareWorkout
from training_bot.service import Journal
from training_bot.telegram import Application, HELP


class Clock:
    value = None

    @classmethod
    def now(cls, tz):
        return cls.value.astimezone(tz)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    Clock.value = datetime(2026, 10, 27, 12, tzinfo=ZoneInfo('Europe/Moscow'))
    monkeypatch.setattr('training_bot.agent.datetime', Clock)
    db = Database('sqlite:///' + str(tmp_path / 'calendar.db'))
    db.init()
    with db.session() as s:
        s.add(Exercise(code='TEST', name='Тестовый жим'))
    yield Journal(db, 'owner')
    db.engine.dispose()


def payload(**changes):
    return dict(day='2000-01-01', workout_id=None, gym='LikeGym', trainer=None,
        week=None, program_session=None,
        sets=[dict(exercise_code='TEST', weight=70, reps=10, minutes=None, rpe=None, note=None)]) | changes


def response(name='prepare_workout', args=None, text=''):
    return NS(output=[NS(type='function_call', name=name, arguments=json.dumps(args or payload()), call_id='call')]
              if name else [], output_text=text, usage=None)


async def write(journal, text, source, **changes):
    client = NS(responses=NS(create=AsyncMock(return_value=response(args=payload(**changes)))))
    agent = Agent(Settings(_env_file=None, owner_telegram_id=42), client)
    reply = await agent.answer(journal, text, source)
    assert reply.draft_id, reply.text
    draft = journal.context(today=Clock.value.date())['pending_draft']['payload']
    saved = journal.confirm(reply.draft_id)
    return draft, saved


async def test_first_undated_message_uses_today_not_model_date(journal):
    draft, saved = await write(journal, 'Тестовый жим 70х10 /1', 'first')
    assert draft['day'] == '2026-10-27'
    assert journal.history_rows()[0][0] == '2026-10-27'


async def test_explicit_date_then_undated_append_reuses_same_workout(journal):
    first, saved = await write(journal, '01.10.2026\nТестовый жим 70х10 /1', 'dated', gym='ReZone', trainer='да')
    with journal.db.session() as s:
        s.get(Workout, saved['workout_id']).status = 'completed'
    second, appended = await write(journal, 'Тестовый жим 80х8 /1', 'append', gym='Другой зал')
    assert first['day'] == second['day'] == '2026-10-01'
    assert appended['workout_id'] == saved['workout_id']
    assert second['gym'] == 'ReZone' and second['trainer'] == 'да'
    with journal.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Workout)) == 1
    assert len(journal.history_rows()) == 2


async def test_new_explicit_date_changes_default_for_same_day(journal):
    await write(journal, '01.10.2026\nТестовый жим 70х10 /1', 'first')
    _, second = await write(journal, '04.10.2026\nТестовый жим 70х10 /1', 'second')
    third, saved = await write(journal, 'Тестовый жим 70х10 /1', 'third')
    assert third['day'] == '2026-10-04' and saved['workout_id'] == second['workout_id']
    assert {row[0] for row in journal.history_rows()} == {'2026-10-01', '2026-10-04'}


async def test_default_persists_after_restart_and_dialogue_is_cleared(journal):
    await write(journal, '01.10.2026\nТестовый жим 70х10 /1', 'first')
    with journal.db.session() as s:
        s.delete(s.get(Conversation, 'owner'))
    restarted_db = Database(str(journal.db.engine.url))
    try:
        restarted = Journal(restarted_db, 'owner')
        draft, _ = await write(restarted, 'Тестовый жим 70х10 /1', 'after-restart')
        assert draft['day'] == '2026-10-01'
    finally:
        restarted_db.engine.dispose()


async def test_next_calendar_day_resets_default_and_old_dialogue(journal):
    await write(journal, '01.10.2026\nТестовый жим 70х10 /1', 'day-one')
    Clock.value = datetime(2026, 10, 28, 0, 1, tzinfo=ZoneInfo('Europe/Moscow'))
    assert journal.context(today=date(2026, 10, 28))['recent_dialogue'] == []
    draft, _ = await write(journal, 'Тестовый жим 70х10 /1', 'day-two')
    assert draft['day'] == '2026-10-28'


async def test_read_query_does_not_change_date_for_later_exercises(journal):
    await write(journal, '01.10.2026\nТестовый жим 70х10 /1', 'dated')
    query = dict(exercise_code=None, start='2020-01-01', end='2020-12-31', exclude_uncertain=False, limit=1)
    client = NS(responses=NS(create=AsyncMock(side_effect=[response('query_workouts', query),
                                                         response(None, text='За 2020 год записей нет')])) )
    agent = Agent(Settings(_env_file=None), client)
    reply = await agent.answer(journal, 'Покажи тренировку за 01.01.2020', 'read')
    assert reply.draft_id is None
    draft, _ = await write(journal, 'Тестовый жим 70х10 /1', 'after-read')
    assert draft['day'] == '2026-10-01'


async def test_pending_confirmation_after_midnight_keeps_preview_date(journal):
    client = NS(responses=NS(create=AsyncMock(return_value=response())))
    agent = Agent(Settings(_env_file=None), client)
    reply = await agent.answer(journal, '01.10.2026\nТестовый жим 70х10 /1', 'late')
    Clock.value = datetime(2026, 10, 28, 0, 1, tzinfo=ZoneInfo('Europe/Moscow'))
    journal.confirm(reply.draft_id)
    assert journal.history_rows()[0][0] == '2026-10-01'
    assert journal.default_workout_day(date(2026, 10, 28)) == date(2026, 10, 28)


async def test_unstructured_date_survives_exercise_clarification(journal):
    client = NS(responses=NS(create=AsyncMock(side_effect=[
        response(None, text='Уточните, жим со штангой или гантелями?'),
        response(args=payload(continues_previous_request=True)),
    ])))
    agent = Agent(Settings(_env_file=None), client)
    first = await agent.answer(journal, '01.10.2026 (2/5)\nЖим 70х10 /1', 'clarify:1')
    assert first.draft_id is None
    reply = await agent.answer(journal, 'Со штангой', 'clarify:2')
    assert reply.draft_id
    draft = journal.context(today=Clock.value.date())['pending_draft']['payload']
    assert draft['day'] == '2026-10-01' and draft['trainer'] == 'да'
    assert 'continues_previous_request' not in draft


def test_database_prevents_duplicate_dates_but_keeps_accounts_separate(journal):
    with journal.db.session() as s:
        s.add_all([Workout(account='owner', day=date(2026, 10, 27)),
                   Workout(account='telegram:99', day=date(2026, 10, 27))])
    with pytest.raises(IntegrityError), journal.db.session() as s:
        s.add(Workout(account='owner', day=date(2026, 10, 27)))
    assert Journal(journal.db, 'telegram:99').default_workout_day(date(2026, 10, 27)) == date(2026, 10, 27)


async def test_legacy_finish_command_does_not_change_workout(journal):
    _, saved = await write(journal, '01.10.2026\nТестовый жим 70х10 /1', 'dated')
    agent = NS(settings=Settings(_env_file=None, owner_telegram_id=42))
    app = Application(agent.settings, journal.db, agent)
    message = NS(chat=NS(type='private', id=42), from_user=NS(id=42), message_id=700,
                 text='/finish', answer=AsyncMock())
    await app.handle_message(message)
    assert '/finish' not in HELP
    assert 'Завершать тренировку не нужно' in message.answer.call_args.args[0]
    with journal.db.session() as s:
        assert s.get(Workout, saved['workout_id']).status == 'recorded'


@pytest.mark.parametrize('text,expected', [
    ('01.10.2026 (2/5)\nЖим 82.5х6 /5', date(2026, 10, 1)),
    ('Запиши за 2026-10-04: жим 70х10 /1', date(2026, 10, 4)),
    ('Запиши за 4 октября: жим 70х10 /1', date(2026, 10, 4)),
    ('Вчера жим 70х10 /1', date(2026, 10, 26)),
    ('Сегодня жим 70х10 /1', date(2026, 10, 27)),
    ('01.10 жим 70х10 /1', date(2026, 10, 1)),
    ('Жим 82.5х6 /5', None),
])
def test_explicit_dates_and_shorthand_are_not_confused(text, expected):
    assert workout_day_in_text(text, date(2026, 10, 27)) == expected


@pytest.mark.parametrize('text', ['31.02.2026\nЖим 70х10 /1', '01.10.2026 и 04.10.2026 Жим 70х10 /1'])
def test_invalid_and_multiple_dates_are_rejected(text):
    with pytest.raises(ValueError):
        workout_day_in_text(text, date(2026, 10, 27))
