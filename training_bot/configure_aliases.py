"""Apply confirmed names via python -m training_bot.configure_aliases; history is unchanged."""
import json

from sqlalchemy import select

from training_bot.config import Settings
from training_bot.db import Alias, Database, Exercise
from training_bot.importer import normalize


CONFIRMED_ALIASES = (
    ("Вертикальная тяга", "BK04", "Тяга верхнего блока к груди"),
    ("Жим на наклонной скамье", "CH05", "Жим штанги лёжа на наклонной скамье"),
)


def configure_aliases(db):
    changed = []
    with db.session() as session:
        for name, code, expected_name in CONFIRMED_ALIASES:
            exercise = session.get(Exercise, code)
            if exercise is None or normalize(exercise.name) != normalize(expected_name):
                raise ValueError(f"Проверьте упражнение {code} в справочнике")
            aliases = list(session.scalars(select(Alias).where(Alias.normalized == normalize(name))))
            if any(alias.exercise_code != code for alias in aliases):
                raise ValueError(f"Конфликт существующего синонима: {name}")
            if not aliases:
                session.add(Alias(name=name, normalized=normalize(name), exercise_code=code, note=None))
                changed.append(name)
            else:
                for alias in aliases:
                    if alias.note is not None:
                        alias.note = None
                        changed.append(alias.name)
    return changed


if __name__ == "__main__":
    db = Database(Settings().database_url)
    try:
        print(json.dumps({"updated_aliases": configure_aliases(db)}, ensure_ascii=False))
    finally:
        db.engine.dispose()
