from datetime import date

import pytest

from training_bot.config import Settings
from training_bot.db import Database, Exercise, SetRecord, Workout
from training_bot.schemas import PrepareEdit, PrepareWorkout, QuerySets, QueryWorkouts, RecordQuery, SummaryQuery
from training_bot.service import Journal, metrics


@pytest.fixture
def journal():
    db = Database("sqlite:///:memory:")
    db.init()
    with db.session() as s:
        s.add(Exercise(code="TEST", name="Тестовое упражнение", muscle=None, equipment=None))
        s.flush()
        for account, day, weight in [("owner", date(2020, 1, 1), 120),
                                     ("owner", date(2026, 10, 1), 90),
                                     ("telegram:2", date(2026, 10, 2), 300)]:
            w = Workout(account=account, day=day, status="completed")
            s.add(w)
            s.flush()
            s.add(SetRecord(workout_id=w.id, exercise_code="TEST", weight=weight, reps=5, ordinal=1))
    yield Journal(db, "owner")
    db.engine.dispose()


def period(**extra):
    return {"exercise_code": "TEST", "start": None, "end": None, "exclude_uncertain": False, **extra}


def set_data(**extra):
    return {"exercise_code": "TEST", "weight": 75, "reps": 8, "minutes": None,
            "rpe": None, "note": None, **extra}


def draft(journal, source="test:1"):
    payload = PrepareWorkout(day=date(2026, 10, 23), workout_id=None, gym=None,
                             trainer=None, week=None, program_session=None, sets=[set_data()])
    return journal.prepare("append", payload, source, "Тестовая запись")


def test_record_searches_old_history_without_other_users(journal):
    result = journal.record(RecordQuery(**period()))
    assert result["maximum_weight"] == 120
    assert result["sets"][0]["date"] == "2020-01-01"
    recent = journal.record(RecordQuery(**period(start=date(2026, 1, 1))))
    assert recent["maximum_weight"] == 90


def test_workout_scope_and_pagination(journal):
    query = QuerySets(**period(), workout_id=None, min_weight=None, limit=1, offset=0)
    first = journal.sets(query)
    assert first["total_matching_sets"] == 2
    assert first["has_more"] is True
    assert first["sets"][0]["weight"] == 90
    assert journal.sets(query.model_copy(update={"workout_id": 3}))["sets"] == []
    assert len(journal.workouts(QueryWorkouts(**period(), status="completed", limit=10))) == 2


def test_summary_uses_all_matching_rows(journal):
    result = journal.summary(SummaryQuery(**period(), group_by="exercise"))
    group = result["groups"][0]
    assert group["sets"] == 2
    assert group["recorded_tonnage"] == 1050
    assert group["workouts"] == 2


def test_draft_is_not_saved_until_confirm_and_confirm_is_idempotent(journal):
    pending = draft(journal)
    assert journal.record(RecordQuery(**period()))["matching_record_sets"] == 1
    assert journal.context()["pending_draft"]["id"] == pending["draft_id"]
    result = journal.confirm(pending["draft_id"])
    assert result["saved"] == 1
    assert journal.confirm(pending["draft_id"])["already_saved"] is True
    rows = journal.sets(QuerySets(**period(), workout_id=None, min_weight=None, limit=40, offset=0))
    assert rows["total_matching_sets"] == 3
    journal.undo()
    assert journal.sets(QuerySets(**period(), workout_id=None, min_weight=None, limit=40, offset=0))["total_matching_sets"] == 2


def test_foreign_draft_and_set_cannot_be_changed(journal):
    pending = draft(journal)
    outsider = Journal(journal.db, "telegram:2")
    with pytest.raises(ValueError):
        outsider.confirm(pending["draft_id"])
    with pytest.raises(ValueError):
        journal.prepare("edit", PrepareEdit(set_id=3, expected_version=1, replacement=set_data()), "edit:1", "edit")


def test_changed_set_rejects_stale_edit(journal):
    pending = journal.prepare("edit", PrepareEdit(set_id=1, expected_version=1, replacement=set_data()), "edit:2", "edit")
    with journal.db.session() as s:
        s.get(SetRecord, 1).version = 2
    with pytest.raises(ValueError, match="уже изменён"):
        journal.confirm(pending["draft_id"])


def test_unknown_zero_assistance_and_estimate_are_distinct():
    assert metrics(None, 5)["tonnage"] is None
    assert metrics(0, 5)["tonnage"] is None
    assert metrics(-30, 5)["estimated_1rm"] is None
    assert metrics(80, 1)["estimated_1rm"] == 80
    assert metrics(80, 13)["estimated_1rm"] is None


def test_unconfigured_owner_is_not_claimed_by_first_user():
    settings = Settings(_env_file=None)
    assert settings.account(123456) is None


def test_date_range_validation():
    with pytest.raises(ValueError):
        RecordQuery(**period(start=date(2026, 2, 1), end=date(2026, 1, 1)))


def test_undo_multiple_edits_then_append_preserves_revisions(journal):
    added = journal.confirm(draft(journal)["draft_id"])
    set_id = added["set_ids"][0]
    for number, weight in enumerate([80, 85], start=1):
        pending = journal.prepare("edit", PrepareEdit(set_id=set_id, expected_version=number,
                                  replacement=set_data(weight=weight)), f"edit:{number}", "edit")
        journal.confirm(pending["draft_id"])
    journal.undo(source="undo:1")
    # Redelivery does not undo a second action.
    journal.undo(source="undo:1")
    with journal.db.session() as s:
        row = s.get(SetRecord, set_id)
        assert row.weight == 80
        assert row.version == 4
    journal.undo(source="undo:2")
    with journal.db.session() as s:
        row = s.get(SetRecord, set_id)
        assert row.weight == 75
        assert row.version == 5
    journal.undo(source="undo:3")
    with journal.db.session() as s:
        assert s.get(SetRecord, set_id).deleted is True


def test_old_cancel_button_does_not_cancel_new_draft(journal):
    old = draft(journal)
    new = draft(journal, "test:2")
    assert journal.cancel(old["draft_id"])["cancelled_drafts"] == 0
    assert journal.context()["pending_draft"]["id"] == new["draft_id"]
