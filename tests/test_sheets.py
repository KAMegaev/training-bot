from datetime import date

import pytest

from training_bot.config import Settings
from training_bot.db import Database, Exercise, SetRecord, SheetSync, Workout
from training_bot.schemas import PrepareWorkout
from training_bot.service import Journal
from training_bot.sheets import HEADERS, SheetMirror, cell_rows, update_requests


@pytest.fixture
def journal(tmp_path):
    db = Database('sqlite:///' + str(tmp_path / 'mirror.db'))
    db.init()
    with db.session() as s:
        s.add(Exercise(code='SYN', name='Синтетическое упражнение'))
        s.flush()
        for account, weight in [('owner', None), ('owner', 0), ('owner', -20), ('telegram:99', 900)]:
            workout = Workout(account=account, day=date(2026, 10, 1), trainer='Тренер', week=2)
            s.add(workout)
            s.flush()
            s.add(SetRecord(workout_id=workout.id, exercise_code='SYN', ordinal=1,
                            weight=weight, reps=8, note='=SOME_FORMULA()'))
    yield Journal(db, 'owner')
    db.engine.dispose()


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.fail = False

    def sheet(self):
        return {'properties': {'sheetId': 12, 'title': 'Тренировки',
                              'gridProperties': {'rowCount': 100, 'columnCount': 16}},
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
    assert [row[3] for row in rows] == [-20, 0, None]
    assert all(row[8:10] == ['Тренер', 2] for row in rows)
    cells = cell_rows(rows)
    assert cells[1]['values'][0]['userEnteredValue']['numberValue'] == 46296
    assert cells[1]['values'][11]['userEnteredValue'] == {'stringValue': '=SOME_FORMULA()'}
    assert cells[2]['values'][3]['userEnteredValue'] == {'numberValue': 0}
    assert cells[3]['values'][3] == {}


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
    rows = [['2026-10-01', 'test', 1, 0, 8, None, None, None, None, None, None, None, '', 'SYN', 1, 1]]
    sheet = {'properties': {'sheetId': 2, 'gridProperties': {'rowCount': 1, 'columnCount': 5}}}
    requests = update_requests(rows, sheet)
    assert requests[0]['updateSheetProperties']['properties']['gridProperties'] == {'rowCount': 2, 'columnCount': 16}
    assert len(cell_rows(rows)[0]['values']) == len(HEADERS)
