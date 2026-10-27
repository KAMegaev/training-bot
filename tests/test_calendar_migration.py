from datetime import date

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from training_bot.calendar_migration import migrate_calendar
from training_bot.db import Database, Draft, Exercise, Operation, SetRecord, Workout


@pytest.fixture
def legacy(tmp_path):
    db = Database('sqlite:///' + str(tmp_path / 'legacy.db'))
    db.init()
    with db.engine.begin() as connection:
        connection.execute(text('DROP INDEX workouts_account_day'))
        connection.execute(text('CREATE INDEX workouts_account_day ON workouts (account, day)'))
    with db.session() as s:
        s.add(Exercise(code='TEST', name='Тест'))
        s.flush()
        first = Workout(account='owner', day=date(2026, 10, 1), gym='Зал', status='completed')
        duplicate = Workout(account='owner', day=date(2026, 10, 1), gym='Зал', status='open')
        other = Workout(account='telegram:99', day=date(2026, 10, 1), gym='Другой зал')
        s.add_all([first, duplicate, other])
        s.flush()
        s.add_all([
            SetRecord(workout_id=first.id, exercise_code='TEST', ordinal=1, weight=70, reps=10, deleted=True),
            SetRecord(workout_id=duplicate.id, exercise_code='TEST', ordinal=1, weight=80, reps=8),
            SetRecord(workout_id=other.id, exercise_code='TEST', ordinal=1, weight=90, reps=6),
        ])
        s.add(Draft(id='draft', account='owner', source_message='legacy', kind='append', status='confirmed',
            payload={'workout_id': duplicate.id, 'day': '2026-10-01'}, result={'workout_id': duplicate.id, 'set_ids': [2]}))
    yield db
    db.engine.dispose()


def test_migration_preserves_sets_deletions_and_draft_references(legacy):
    result = migrate_calendar(legacy)
    assert result == {'merged_workouts': 1, 'total_sets_preserved': 3}
    with legacy.session() as s:
        assert s.scalar(select(func.count()).select_from(Workout)) == 2
        rows = list(s.scalars(select(SetRecord).order_by(SetRecord.id)))
        assert [(row.workout_id, row.ordinal, row.weight, row.reps, row.deleted, row.version) for row in rows] == [
            (1, 1, 70, 10, True, 1), (1, 2, 80, 8, False, 1), (3, 1, 90, 6, False, 1)]
        draft = s.get(Draft, 'draft')
        assert draft.payload['workout_id'] == draft.result['workout_id'] == 1
        assert draft.status == 'confirmed'
        audit = s.get(Operation, 'migration:calendar-workouts:v1').result
        assert audit['workout_id_mapping'] == {'2': 1}
        assert len(audit['sets_before']) == len(audit['workouts_before']) == 2
    assert any(index['name'] == 'workouts_account_day' and index['unique']
               for index in inspect(legacy.engine).get_indexes('workouts'))
    with pytest.raises(IntegrityError), legacy.session() as s:
        s.add(Workout(account='owner', day=date(2026, 10, 1)))
    assert migrate_calendar(legacy)['already_migrated'] is True


def test_metadata_conflict_rolls_back_without_losing_data(legacy):
    with legacy.session() as s:
        s.get(Workout, 2).gym = 'Несовпадающий зал'
    with pytest.raises(ValueError, match='Конфликт поля gym'):
        migrate_calendar(legacy)
    with legacy.session() as s:
        assert s.scalar(select(func.count()).select_from(Workout)) == 3
        assert s.get(SetRecord, 2).workout_id == 2
        assert s.get(Operation, 'migration:calendar-workouts:v1') is None
    assert all(not index['unique'] for index in inspect(legacy.engine).get_indexes('workouts'))
