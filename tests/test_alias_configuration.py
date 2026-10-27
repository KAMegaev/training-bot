import pytest

from training_bot.configure_aliases import configure_aliases
from training_bot.db import Alias, Database, Exercise
from training_bot.service import Journal


def test_confirmed_aliases_are_exact_unambiguous_and_idempotent():
    db = Database('sqlite:///:memory:')
    db.init()
    try:
        with db.session() as session:
            session.add_all([
                Exercise(code='BK04', name='Тяга верхнего блока к груди'),
                Exercise(code='CH05', name='Жим штанги лёжа на наклонной скамье'),
            ])
            session.flush()
            session.add(Alias(name='Жим на наклонной скамье', normalized='жим на наклонной скамье',
                              exercise_code='CH05', note='снаряд не указан — принято как штанга'))
        assert len(configure_aliases(db)) == 2
        assert configure_aliases(db) == []
        result = Journal(db, 'owner').search_exercises_batch(['Вертикальная тяга', 'Жим на наклонной скамье'])
        assert all(item['exact_match'] and len(item['candidates']) == 1 for item in result)
        assert [item['candidates'][0]['code'] for item in result] == ['BK04', 'CH05']
        assert all(item['candidates'][0]['mapping_note'] is None for item in result)
    finally:
        db.engine.dispose()


def test_alias_configuration_rolls_back_on_conflicting_catalog():
    db = Database('sqlite:///:memory:')
    db.init()
    try:
        with db.session() as session:
            session.add_all([Exercise(code='BK04', name='Тяга верхнего блока к груди'),
                             Exercise(code='CH05', name='Другое упражнение')])
        with pytest.raises(ValueError):
            configure_aliases(db)
        assert Journal(db, 'owner').search_exercises_batch(['Вертикальная тяга'])[0]['exact_match'] is False
    finally:
        db.engine.dispose()
