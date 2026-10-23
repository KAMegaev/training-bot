"""Read-only XLSX import. Source file is never saved or modified."""
import hashlib
import json
import re
import warnings
from datetime import datetime
from pathlib import Path

import openpyxl
from sqlalchemy import func, select

from .db import Alias, Database, Exercise, ImportBatch, SetRecord, Workout


def normalize(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.lower().replace("ё", "е")).strip()


def import_workbook(db: Database, path: Path, account: str = "owner") -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    batch_key = f"{account}:{digest}"
    with db.session() as s:
        prior = s.get(ImportBatch, batch_key)
        if prior:
            return {**prior.report, "already_imported": True}
        # An edited full-history file must be reconciled, never appended blindly.
        if s.scalar(select(func.count()).select_from(ImportBatch).where(ImportBatch.key.like(account + ":%"))):
            raise ValueError("Для этого пользователя история уже импортирована из другого файла. Нужна сверка различий.")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                names = {}
                for row in workbook["Справочник"].iter_rows(min_row=2, values_only=True):
                    code, name = row[:2]
                    if not code:
                        continue
                    names[name] = code
                    existing = s.get(Exercise, code)
                    if existing and existing.name != name:
                        raise ValueError(f"Конфликт справочника: {code}")
                    if not existing:
                        s.add(Exercise(code=code, name=name, muscle=row[3], equipment=row[5]))
                s.flush()
                uncertain_names = set()
                alias_count = 0
                for row in workbook["Синонимы"].iter_rows(min_row=2, values_only=True):
                    name, _, code, note = row[:4]
                    if not name:
                        continue
                    if not s.get(Exercise, code):
                        raise ValueError(f"Неизвестный код в синонимах: {code}")
                    if note:
                        uncertain_names.add(name)
                    if not s.scalar(select(Alias).where(Alias.name == name, Alias.exercise_code == code)):
                        s.add(Alias(name=name, normalized=normalize(name), exercise_code=code, note=note))
                    alias_count += 1
                workouts = {}
                count = uncertain = missing_weight = missing_reps = 0
                all_dates = []
                for index, row in enumerate(workbook["Тренировки"].iter_rows(min_row=2, values_only=True), 2):
                    if row[1] is None and row[5] is None and row[6] is None:
                        continue  # trailing prefilled formulas are not workouts
                    if not isinstance(row[1], datetime) or row[6] not in names:
                        raise ValueError(f"Нельзя импортировать строку {index}: дата или упражнение")
                    day = row[1].date()
                    # Historic session number is missing; retain gym/program metadata.
                    key = (day, row[2], row[3], row[4], row[18])
                    if key not in workouts:
                        workout = Workout(account=account, day=day, gym=row[2], trainer=row[3],
                                          week=row[4], program_session=row[18], status="completed")
                        s.add(workout)
                        s.flush()
                        workouts[key] = workout.id
                    flagged = row[5] in uncertain_names
                    s.add(SetRecord(workout_id=workouts[key], exercise_code=names[row[6]],
                                    ordinal=row[9] or 1, weight=row[7], reps=row[8], rpe=row[10],
                                    note=row[11], minutes=row[12], raw_name=row[5], original=row[16],
                                    uncertain=flagged, source_row=index, source_key=f"{batch_key}:{index}"))
                    count += 1
                    uncertain += flagged
                    missing_weight += row[7] is None
                    missing_reps += row[8] is None
                    all_dates.append(day)
                report = {"sets": count, "workouts": len(workouts), "distinct_dates": len(set(all_dates)),
                          "exercises": len(names), "aliases": alias_count, "flagged_sets": uncertain,
                          "missing_weight": missing_weight, "missing_reps": missing_reps,
                          "first_date": str(min(all_dates)) if all_dates else None,
                          "last_date": str(max(all_dates)) if all_dates else None,
                          "sha256": digest,
                          "session_grouping": "date + gym + trainer + program week + session number",
                          "uncertainty_rule": "any alias mapping note is conservatively flagged"}
                s.add(ImportBatch(key=batch_key, report=report))
                return report
            finally:
                workbook.close()
