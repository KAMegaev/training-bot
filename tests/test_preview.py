from datetime import date

import pytest

from training_bot.db import Database, Exercise
from training_bot.schemas import PrepareEdit, PrepareWorkout, QuerySets
from training_bot.service import Journal
from training_bot.telegram import Application


@pytest.fixture
def journal():
    db = Database("sqlite:///:memory:")
    db.init()
    with db.session() as session:
        session.add_all([
            Exercise(code="PRESS", name="Жим штанги лёжа"),
            Exercise(code="PULL", name="Подтягивания"),
            Exercise(code="AB06", name="Комплекс экстензия-пресс"),
        ])
    yield Journal(db, "preview-test")
    db.engine.dispose()


def approach(**changes):
    return dict(exercise_code="PRESS", raw_name=None, weight=82.5, reps=6,
                minutes=None, rpe=None, note=None) | changes


def prepare(journal, items):
    return journal.prepare("append", PrepareWorkout(
        day=date(2026, 10, 27), workout_id=None, gym="LikeGym", trainer=None,
        week=4, program_session=2, sets=items), "preview:1", "Synthetic workout")


def test_preview_groups_sets_without_changing_saved_rows(journal):
    draft = prepare(journal, [approach(weight=70, reps=10)] + [approach() for _ in range(5)])
    expected = ("Тренировка 2026-10-27, LikeGym, занятие 2, неделя 4\n\n"
                "Жим штанги лёжа\n70х10 /1\n82.5х6 /5")
    assert draft["preview"] == expected
    assert Application(None, journal.db, None).pending(journal).text.startswith(expected + "\n\n")
    saved = journal.confirm(draft["draft_id"])
    assert saved["saved"] == 6
    result = journal.sets(QuerySets(exercise_code=None, start=None, end=None,
        exclude_uncertain=False, workout_id=saved["workout_id"], min_weight=None, limit=40, offset=0))
    assert [(row["weight"], row["reps"]) for row in result["sets"]] == [(70, 10)] + [(82.5, 6)] * 5


def test_preview_preserves_weight_and_exercise_order(journal):
    draft = prepare(journal, [approach(), approach(), approach(weight=70), approach(),
        approach(exercise_code="PULL", weight=0), approach()])
    assert draft["preview"].split("\n\n")[1:] == [
        "Жим штанги лёжа\n82.5х6 /2\n70х6 /1\n82.5х6 /1",
        "Подтягивания\n0х6 /1", "Жим штанги лёжа\n82.5х6 /1"]


def test_preview_keeps_different_notes_rpe_and_duration(journal):
    draft = prepare(journal, [approach(note="на каждую руку/ногу"), approach(rpe=8),
        approach(rpe=8), approach(minutes=1), approach()])
    assert draft["preview"].splitlines()[3:] == [
        "82.5х6 /1; на каждую руку/ногу", "82.5х6 /2; RPE 8",
        "82.5х6; 1 мин /1", "82.5х6 /1"]


def test_preview_distinguishes_missing_zero_and_negative_weights(journal):
    draft = prepare(journal, [approach(weight=None), approach(weight=0), approach(weight=-30),
        approach(weight=None, reps=None, minutes=12.5),
        approach(exercise_code="AB06", weight=None, reps=None),
        approach(exercise_code="AB06", weight=None, reps=None)])
    assert draft["preview"].split("\n\n")[1:] == [
        "Жим штанги лёжа\nвес не указан × 6 /1\n0х6 /1\n-30х6 /1\n12.5 мин /1",
        "Комплекс экстензия-пресс\n/2"]


def test_edit_preview_uses_the_same_notation(journal):
    saved = journal.confirm(prepare(journal, [approach()])["draft_id"])
    draft = journal.prepare("edit", PrepareEdit(set_id=saved["set_ids"][0], expected_version=1,
        replacement=approach(weight=70, reps=10)), "preview:edit", "Synthetic edit")
    assert draft["preview"] == (f"Исправление подхода #{saved['set_ids'][0]}\n\n"
                                "Жим штанги лёжа\n70х10 /1")
