from datetime import date

import pytest

from training_bot.db import Database, Exercise
from training_bot.notation import has_trainer_marker, parse_workout, previous_workout
from training_bot.schemas import ExerciseSelection, PrepareWorkout
from training_bot.service import Journal


@pytest.fixture
def journal():
    db = Database('sqlite:///:memory:')
    db.init()
    with db.session() as session:
        session.add_all([Exercise(code='PRESS', name='Жим'), Exercise(code='ROW', name='Тяга')])
    yield Journal(db, 'owner')
    db.engine.dispose()


def entry(header):
    return f'{header}\nТренировка 2 (неделя 3)\n1. Жим\n70х10 /1\n82.5х6 /5\n2. Тяга\n40х8 /2'


def selection(parsed):
    return parsed.prepare([ExerciseSelection(exercise_index=0, exercise_code='PRESS'),
                           ExerciseSelection(exercise_index=1, exercise_code='ROW')])


@pytest.mark.parametrize('header,gym', [
    ('27.10.2026 (2/5)', 'LikeGym'),
    ('27.10.2026 ( 2 / 5 )', 'LikeGym'),
    ('27.10.2026 (Орёл) (2/5)', 'ReZone'),
    ('27.10.2026 (2/5) в Орле', 'ReZone'),
])
def test_trainer_marker_covers_every_exercise_and_history_row(journal, header, gym):
    parsed = parse_workout(entry(header))
    assert parsed.gym == gym
    assert parsed.context()['trainer'] == 'да'
    draft = journal.prepare('append', selection(parsed), 'trainer:1', parsed.source)
    assert 'тренер: да' in draft['preview']
    assert journal.history_rows() == []
    journal.confirm(draft['draft_id'])
    rows = journal.history_rows()
    assert len(rows) == 8
    assert {row[14] for row in rows} == {'PRESS', 'ROW'}
    assert all(row[9] == 'да' and row[8] == gym for row in rows)


@pytest.mark.parametrize('text,expected', [
    ('2026-10-27 (2/5)\nЖим 70х10 /1', True),
    ('27/10/2026 (2/5)\nЖим 70х10 /1', True),
    ('27.10.2026 зал Особый (2/5)\nЖим 70х10 /1', True),
    ('27.10.2026\nЖим 70х10 /1 (2/5)', False),
    ('27.10.2026 (Орёл)\nЖим 70х10 /1', False),
    ('27.10.2026 (2.5)\nЖим 70х10 /1', False),
    ('Жим 70х10 /1 (2/5)', False),
])
def test_only_fraction_on_date_line_means_trainer(text, expected):
    assert has_trainer_marker(text) is expected


def test_conversational_fallback_cannot_omit_trainer_marker(journal):
    original = '27.10.2026 зал Особый (2/5)\nЖим 70 кг на 10'
    assert parse_workout(original) is None
    payload = PrepareWorkout(day=date(2026, 10, 27), workout_id=None, gym='Особый', trainer=None,
        week=None, program_session=None,
        sets=[dict(exercise_code='PRESS', weight=70, reps=10, minutes=None, rpe=None, note=None)])
    draft = journal.prepare('append', payload, 'fallback:1', original)
    assert journal.context()['pending_draft']['payload']['trainer'] == 'да'
    journal.confirm(draft['draft_id'])
    assert journal.history_rows()[0][9] == 'да'


def test_marker_survives_exercise_clarification():
    parsed = previous_workout([
        {'role': 'user', 'content': entry('27.10.2026 (2/5)')},
        {'role': 'assistant', 'content': 'Какой вариант жима?'},
        {'role': 'user', 'content': 'Со штангой'},
    ])
    assert selection(parsed).trainer == 'да'


def test_no_marker_leaves_trainer_empty(journal):
    parsed = parse_workout(entry('27.10.2026'))
    assert selection(parsed).trainer is None
    draft = journal.prepare('append', selection(parsed), 'without:1', parsed.source)
    journal.confirm(draft['draft_id'])
    assert all(row[9] is None for row in journal.history_rows())


def test_append_to_existing_workout_marks_all_sets_and_undo_restores(journal):
    original = parse_workout(entry('27.10.2026'))
    first = journal.prepare('append', selection(original), 'existing:1', original.source)
    journal.confirm(first['draft_id'])
    marked = parse_workout(entry('27.10.2026 (2/5)'))
    second = journal.prepare('append', selection(marked), 'existing:2', marked.source)
    # Preparing the proposal must not change already saved sets.
    assert all(row[9] is None for row in journal.history_rows())
    journal.confirm(second['draft_id'])
    assert len(journal.history_rows()) == 16
    assert all(row[9] == 'да' for row in journal.history_rows())
    journal.undo()
    assert len(journal.history_rows()) == 8
    assert all(row[9] is None for row in journal.history_rows())
