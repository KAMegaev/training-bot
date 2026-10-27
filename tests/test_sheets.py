from datetime import date

import pytest

from training_bot.config import Settings
from training_bot.db import Database, Exercise, SetRecord, SheetSync, Workout
from training_bot.schemas import PrepareWorkout
from training_bot.service import Journal
from training_bot.sheets import EXERCISE_HEADERS, HEADERS, SheetMirror, cell_rows, update_requests


@pytest.fixture
def journal(tmp_path):
    db = Database('sqlite:///' + str(tmp_path / 'mirror.db'))
    db.init()
    with db.session() as s:
        s.add(Exercise(code='SYN', name='Синтетическое упражнение'))
        s.flush()
        workouts = {}
        for account, weight in [('owner', -20), ('owner', 0), ('owner', None), ('telegram:99', 900)]:
            if account not in workouts:
                workout = Workout(account=account, day=date(2026, 10, 1), trainer='Тренер', week=2)
                s.add(workout)
                s.flush()
                workouts[account] = workout
            workout = workouts[account]
            s.add(SetRecord(workout_id=workout.id, exercise_code='SYN', ordinal=1,
                            weight=weight, reps=8, note='=SOME_FORMULA()', raw_name='Как записал пользователь'))
    yield Journal(db, 'owner')
    db.engine.dispose()


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.fail = False

    def sheet(self, kind='history'):
        if kind == 'exercises':
            return {'properties': {'sheetId': 18, 'title': 'Справочник упражнений',
                                   'gridProperties': {'rowCount': 100, 'columnCount': 4}}}
        return {'properties': {'sheetId': 12, 'title': 'Тренировки',
                              'gridProperties': {'rowCount': 100, 'columnCount': 17}},
                'basicFilter': {'criteria': {'1': {'hiddenValues': ['Скрытое упражнение']}}}}

    def update(self, requests):
        self.calls.append(requests)
        if self.fail:
            raise RuntimeError('synthetic network error')


def mirror(journal, google):
    return SheetMirror(Settings(_env_file=None, google_spreadsheet_id='synthetic_sheet_id',
                                google_sheet_tab_id=12), journal.db, google)


def test_snapshot_keeps_null_zero_negative_and_excludes_other_users(journal):
    rows = journal.history_rows()
    assert len(rows) == 3
    assert [row[4] for row in rows] == [-20, 0, None]
    assert all(row[9:11] == ['Тренер', 2] for row in rows)
    assert all(row[2] == 'Как записал пользователь' for row in rows)
    cells = cell_rows(rows)
    assert cells[1]['values'][0]['userEnteredValue']['numberValue'] == 46296
    assert cells[1]['values'][12]['userEnteredValue'] == {'stringValue': '=SOME_FORMULA()'}
    assert cells[2]['values'][4]['userEnteredValue'] == {'numberValue': 0}
    assert cells[3]['values'][4] == {}


def test_unchanged_snapshot_uses_no_google_calls(journal):
    google = FakeGoogle()
    sync = mirror(journal, google)
    assert sync.sync_once()['changed']
    assert sync.sync_once()['changed'] is False
    assert len(google.calls) == 1
    assert sync.sync_once(force=True)['changed']


def test_failed_google_update_does_not_advance_state_and_retries(journal):
    google = FakeGoogle()
    google.fail = True
    sync = mirror(journal, google)
    with pytest.raises(RuntimeError):
        sync.sync_once()
    with journal.db.session() as s:
        assert s.get(SheetSync, sync.key) is None
    google.fail = False
    assert sync.sync_once()['rows'] == 3


def test_undo_triggers_snapshot_update_and_clears_trailing_values(journal):
    google = FakeGoogle()
    sync = mirror(journal, google)
    payload = PrepareWorkout(day=date(2026, 10, 23), workout_id=None, gym=None, trainer=None,
        week=None, program_session=None, sets=[dict(exercise_code='SYN', weight=55, reps=8,
                                                   minutes=None, rpe=None, note=None)])
    draft = journal.prepare('append', payload, 'mirror:append', 'synthetic')
    journal.confirm(draft['draft_id'])
    assert sync.sync_once()['rows'] == 4
    journal.undo()
    assert sync.sync_once()['rows'] == 3
    requests = google.calls[-1]
    values = next(r['updateCells'] for r in requests if 'updateCells' in r)
    assert values['range']['endRowIndex'] == 100
    assert len(values['rows']) == 4  # The rest of the owned range is cleared atomically.
    basic_filter = next(r['setBasicFilter']['filter'] for r in requests if 'setBasicFilter' in r)
    assert basic_filter['criteria']['1']['hiddenValues'] == ['Скрытое упражнение']
    assert basic_filter['range']['endRowIndex'] == 4


def test_grid_expands_before_values_and_headers_are_single_row():
    rows = [['2026-10-01', 'test', 'as recorded', 1, 0, 8, None, None, None, None, None, None, None, '', 'SYN', 1, 1]]
    sheet = {'properties': {'sheetId': 2, 'gridProperties': {'rowCount': 1, 'columnCount': 5}}}
    requests = update_requests(rows, sheet)
    assert requests[0]['updateSheetProperties']['properties']['gridProperties'] == {'rowCount': 2, 'columnCount': 17}
    assert len(cell_rows(rows)[0]['values']) == len(HEADERS)


def test_catalog_syncs_all_exercises_and_detects_changes_without_new_sets(journal):
    google = FakeGoogle()
    settings = Settings(_env_file=None, google_spreadsheet_id='synthetic_sheet_id',
                        google_sheet_tab_id=12, google_exercises_tab_id=18)
    sync = SheetMirror(settings, journal.db, google)
    assert sync.sync_once()['exercises'] == 1
    assert {r['updateCells']['range']['sheetId'] for r in google.calls[-1] if 'updateCells' in r} == {12, 18}
    assert sync.sync_once()['changed'] is False
    with journal.db.session() as s:
        s.add(Exercise(code='UNUSED', name='Нет подходов', muscle='Группа', equipment='Оборудование'))
    result = sync.sync_once()
    assert result['changed'] and result['exercises'] == 2
    updates = [r['updateCells'] for r in google.calls[-1] if 'updateCells' in r]
    assert [r['range']['sheetId'] for r in updates] == [18]
    assert updates[0]['rows'][2]['values'][0]['userEnteredValue'] == {'stringValue': 'UNUSED'}
    assert not any('repeatCell' in r for r in google.calls[-1])  # No date format on codes.
    with journal.db.session() as s:
        s.get(Exercise, 'UNUSED').equipment = 'Изменено'
    assert sync.sync_once()['changed']


def test_original_name_change_is_detected_independently(journal):
    google = FakeGoogle()
    sync = mirror(journal, google)
    sync.sync_once()
    with journal.db.session() as s:
        s.get(SetRecord, 3).raw_name = 'Другое исходное название'
    assert sync.sync_once()['changed']
    rows = next(r['updateCells']['rows'] for r in google.calls[-1] if 'updateCells' in r)
    assert rows[-1]['values'][2]['userEnteredValue'] == {'stringValue': 'Другое исходное название'}


def test_failed_batch_advances_neither_history_nor_catalog_state(journal):
    google = FakeGoogle()
    google.fail = True
    settings = Settings(_env_file=None, google_spreadsheet_id='synthetic_sheet_id',
                        google_sheet_tab_id=12, google_exercises_tab_id=18)
    sync = SheetMirror(settings, journal.db, google)
    with pytest.raises(RuntimeError):
        sync.sync_once()
    with journal.db.session() as s:
        assert s.query(SheetSync).count() == 0
    google.fail = False
    assert sync.sync_once()['changed']
    with journal.db.session() as s:
        assert s.query(SheetSync).count() == 2
