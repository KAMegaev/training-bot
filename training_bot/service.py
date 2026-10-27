import csv
import io
import json
import uuid
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from itertools import groupby
from zoneinfo import ZoneInfo

from sqlalchemy import case, extract, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from .db import (Alias, Conversation, Database, Draft, Exercise, Operation, SetRecord,
                 Usage, Workout, WriteContext, utcnow)
from .importer import normalize
from .notation import has_trainer_marker
from .schemas import (PrepareEdit, PrepareWorkout, QuerySets, QueryWorkouts,
                      RecordQuery, SummaryQuery)


def metrics(weight, reps):
    if weight is None or weight <= 0 or reps is None:
        return {"tonnage": None, "estimated_1rm": None}
    return {"tonnage": round(weight * reps, 2),
            "estimated_1rm": round(weight if reps == 1 else weight * (1 + reps / 30), 1)
            if 1 <= reps <= 12 else None}


def serialize_set(row, workout, exercise):
    return {"set_id": row.id, "version": row.version, "workout_id": workout.id,
            "date": str(workout.day), "gym": workout.gym, "exercise_code": exercise.code,
            "exercise": exercise.name, "raw_name": row.raw_name,
            "set_number": row.ordinal, "weight": row.weight,
            "reps": row.reps, "minutes": row.minutes, "rpe": row.rpe, "note": row.note,
            "uncertain_mapping": row.uncertain, "source_row": row.source_row,
            **metrics(row.weight, row.reps)}


class Journal:
    def __init__(self, db: Database, account: str, timezone="Europe/Moscow"):
        self.db, self.account = db, account
        self.tz = ZoneInfo(timezone)

    def today(self):
        return datetime.now(self.tz).date()

    def default_workout_day(self, today=None):
        today = today or self.today()
        with self.db.session() as s:
            context = s.get(WriteContext, self.account)
            return context.workout_day if context and context.message_day == today else today

    def search_exercises(self, query: str):
        target = normalize(query)
        with self.db.session() as s:
            exercises = list(s.scalars(select(Exercise)))
            aliases = list(s.scalars(select(Alias)))
        candidates = {}
        for item in exercises:
            candidates[item.code] = {"code": item.code, "name": item.name, "score": 0,
                                     "matched_alias": None, "mapping_note": None}
        entries = [(x.code, x.name, None) for x in exercises] + [(x.exercise_code, x.name, x.note) for x in aliases]
        for code, name, note in entries:
            norm = normalize(name)
            score = (1.0 if norm == target else
                     max(0.8 if target in norm else 0, SequenceMatcher(None, target, norm).ratio()))
            if score > candidates[code]["score"]:
                candidates[code].update(score=round(score, 3), matched_alias=name, mapping_note=note)
        return sorted(candidates.values(), key=lambda x: x["score"], reverse=True)[:8]

    def search_exercises_batch(self, queries):
        results = []
        for query in dict.fromkeys(queries):
            candidates = self.search_exercises(query)
            # Check the actual normalized name, not a rounded fuzzy score.
            exact = [item for item in candidates
                     if item["matched_alias"] and normalize(item["matched_alias"]) == normalize(query)]
            results.append({"query": query, "exact_match": bool(exact),
                            "candidates": exact or candidates})
        return results

    def _base(self, query):
        q = select(SetRecord, Workout, Exercise).join(Workout, SetRecord.workout_id == Workout.id).join(Exercise)
        q = q.where(Workout.account == self.account, SetRecord.deleted.is_(False))
        if query.exercise_code:
            q = q.where(SetRecord.exercise_code == query.exercise_code)
        if query.start:
            q = q.where(Workout.day >= query.start)
        if query.end:
            q = q.where(Workout.day <= query.end)
        if query.exclude_uncertain:
            q = q.where(SetRecord.uncertain.is_(False))
        return q

    def sets(self, query: QuerySets):
        q = self._base(query)
        if query.workout_id is not None:
            q = q.where(Workout.id == query.workout_id)
        if query.min_weight is not None:
            q = q.where(SetRecord.weight >= query.min_weight)
        with self.db.session() as s:
            count = s.scalar(select(func.count()).select_from(q.subquery()))
            rows = s.execute(q.order_by(Workout.day.desc(), Workout.id.desc(), SetRecord.id)
                             .offset(query.offset).limit(query.limit)).all()
            return {"total_matching_sets": count, "offset": query.offset,
                    "has_more": query.offset + len(rows) < count,
                    "sets": [serialize_set(*row) for row in rows]}

    def workouts(self, query: QueryWorkouts):
        q = self._base(query)
        q = q.with_only_columns(Workout.id, Workout.day, Workout.gym,
                                func.count(SetRecord.id).label("matching_sets")).group_by(
                                    Workout.id, Workout.day, Workout.gym)
        with self.db.session() as s:
            return [dict(row._mapping) | {"day": str(row.day)} for row in s.execute(
                q.order_by(Workout.day.desc(), Workout.id.desc()).limit(query.limit))]

    def record(self, query: RecordQuery):
        q = self._base(query)
        with self.db.session() as s:
            ex = s.get(Exercise, query.exercise_code)
            if ex is None:
                raise ValueError("Неизвестное упражнение; сначала найдите его в справочнике")
            maximum = s.scalar(select(func.max(q.subquery().c.weight)))
            if maximum is None:
                return {"exercise": ex.name, "maximum_weight": None, "reason": "Нет записанного веса за этот период"}
            ties = q.where(SetRecord.weight == maximum)
            count = s.scalar(select(func.count()).select_from(ties.subquery()))
            rows = s.execute(ties.order_by(Workout.day.desc(), SetRecord.id).limit(40)).all()
            return {"exercise": ex.name, "maximum_weight": maximum, "matching_record_sets": count,
                    "has_more": count > len(rows), "sets": [serialize_set(*r) for r in rows],
                    "interpretation": "Отрицательный вес — помощь гравитрона: ближе к нулю означает меньше помощи. Это не поднятый вес." if maximum < 0 else
                    "Вес из журнала; для гантелей — одной гантели, для собственного веса — 0. Это не расчётный 1ПМ."}

    def summary(self, query: SummaryQuery):
        q = self._base(query)
        tonnage = case(( (SetRecord.weight > 0) & (SetRecord.reps.is_not(None)),
                         SetRecord.weight * SetRecord.reps), else_=None)
        columns = [func.count(SetRecord.id).label("sets"),
                   func.count(func.distinct(Workout.id)).label("workouts"),
                   func.max(SetRecord.weight).label("max_weight"),
                   func.sum(tonnage).label("recorded_tonnage"),
                   func.count(tonnage).label("sets_with_tonnage"),
                   func.sum(SetRecord.minutes).label("minutes"),
                   func.sum(case((SetRecord.uncertain.is_(True), 1), else_=0)).label("uncertain_sets")]
        if query.group_by == "month":
            groups = [extract("year", Workout.day).label("year"), extract("month", Workout.day).label("month")]
        else:
            groups = [Exercise.code, Exercise.name]
        q = q.with_only_columns(*groups, *columns).group_by(*groups).order_by(*groups)
        with self.db.session() as s:
            rows = s.execute(q).all()
            return {"groups": [dict(r._mapping) for r in rows[:240]], "truncated": len(rows) > 240,
                    "tonnage_method": "Сумма положительного записанного веса × повторения. Гантели не удваиваются. Пропуски, собственный вес и помощь исключены; сравнивайте одинаковые варианты упражнений."}

    def _validate_codes(self, s, sets):
        for item in sets:
            if s.get(Exercise, item.exercise_code) is None:
                raise ValueError("Упражнение отсутствует в справочнике: " + item.exercise_code)

    def prepare(self, kind: str, payload, source_message: str, original: str, *, received_day=None):
        if kind == "append" and has_trainer_marker(original):
            payload = payload.model_copy(update={"trainer": "да"})
        with self.db.session() as s:
            if kind == "append":
                # Continuations inherit existing workout metadata, regardless of
                # legacy status or the gym guessed by the model.
                workout = s.scalar(select(Workout).where(
                    Workout.account == self.account, Workout.day == payload.day))
                if workout:
                    inherited = {key: getattr(workout, key) for key in
                                 ("gym", "trainer", "week", "program_session")
                                 if getattr(workout, key) is not None}
                    if payload.trainer == "да":
                        inherited["trainer"] = "да"
                    payload = payload.model_copy(update=inherited)
            data = payload.model_dump(mode="json")
            data["original"] = original
            old = s.scalar(select(Draft).where(Draft.account == self.account, Draft.source_message == source_message))
            if old:
                if old.kind != kind or old.payload != data:
                    raise ValueError("В этом сообщении уже есть черновик. Для изменения отправьте отдельное сообщение.")
                return {"draft_id": old.id, "status": old.status, "preview": self._preview(s, old)}
            if kind == "append":
                self._validate_codes(s, payload.sets)
                if payload.workout_id:
                    workout = s.get(Workout, payload.workout_id)
                    if not workout or workout.account != self.account or workout.day != payload.day:
                        raise ValueError("Тренировка не найдена или дата не совпадает")
            elif kind == "edit":
                self._validate_codes(s, [payload.replacement])
                row = self._owned_set(s, payload.set_id)
                if row.version != payload.expected_version:
                    raise ValueError("Подход изменился. Получите его актуальную версию")
            else:
                raise ValueError("Неподдерживаемое действие")
            # A new write proposal supersedes previous unconfirmed proposals.
            s.execute(update(Draft).where(Draft.account == self.account, Draft.status == "pending")
                      .values(status="superseded"))
            draft = Draft(id=uuid.uuid4().hex[:20], account=self.account, source_message=source_message,
                          kind=kind, payload=data)
            s.add(draft)
            s.flush()
            if kind == "append":
                values = dict(account=self.account, message_day=received_day or self.today(), workout_day=payload.day)
                insert = pg_insert if self.db.engine.dialect.name == "postgresql" else sqlite_insert
                s.execute(insert(WriteContext).values(**values).on_conflict_do_update(
                    index_elements=[WriteContext.account],
                    set_={key: values[key] for key in ("message_day", "workout_day")}))
            return {"draft_id": draft.id, "status": "pending", "preview": self._preview(s, draft),
                    "instruction": "Это черновик. Изменения записываются только кнопкой пользователя."}

    def _preview(self, s, draft):
        data = draft.payload
        lines = [f"Тренировка {data['day']}" if draft.kind == "append" else f"Исправление подхода #{data['set_id']}"]
        if draft.kind == "append" and data.get("gym"):
            lines[0] += f", {data['gym']}"
        if draft.kind == "append" and data.get("trainer"):
            lines[0] += f", тренер: {data['trainer']}"
        if draft.kind == "append":
            if data.get("program_session") is not None:
                lines[0] += f", занятие {data['program_session']}"
            if data.get("week") is not None:
                lines[0] += f", неделя {data['week']}"
        items = data["sets"] if draft.kind == "append" else [data["replacement"]]
        # Group consecutive runs only: a return to an earlier weight or exercise
        # must stay in its original position. Distinct notes/RPE remain visible.
        for code, exercise_items in groupby(items, key=lambda item: item["exercise_code"]):
            exercise = s.get(Exercise, code)
            lines.extend(["", exercise.name])
            fields = ("weight", "reps", "minutes", "rpe", "note")
            for values, repeated in groupby(exercise_items, key=lambda item: tuple(item.get(k) for k in fields)):
                weight, reps, minutes, rpe, note = values
                count = sum(1 for _ in repeated)
                if reps is not None:
                    performance = (f"{weight:g}х{reps}" if weight is not None else
                                   f"вес не указан × {reps}")
                    if minutes is not None:
                        performance += f"; {minutes:g} мин"
                elif minutes is not None:
                    performance = f"{minutes:g} мин"
                    if weight is not None:
                        performance += f"; {weight:g} кг"
                else:
                    performance = "" if weight is None else f"{weight:g} кг"
                line = f"{performance} /{count}".strip()
                if note:
                    line += f"; {note}"
                if rpe is not None:
                    line += f"; RPE {rpe:g}"
                lines.append(line)
        return "\n".join(lines)

    def _owned_set(self, s, set_id):
        row = s.scalar(select(SetRecord).join(Workout).where(
            SetRecord.id == set_id, Workout.account == self.account, SetRecord.deleted.is_(False)))
        if not row:
            raise ValueError("Подход не найден")
        return row

    def confirm(self, draft_id: str):
        with self.db.session() as s:
            # Conditional UPDATE serializes competing confirms in PostgreSQL and SQLite.
            claimed = s.execute(update(Draft).where(Draft.id == draft_id, Draft.account == self.account,
                                                     Draft.status == "pending").values(status="committing"))
            if claimed.rowcount != 1:
                draft = s.scalar(select(Draft).where(Draft.id == draft_id, Draft.account == self.account))
                if draft and draft.status == "confirmed":
                    return draft.result | {"already_saved": True}
                raise ValueError("Черновик недоступен или заменён новым")
            draft = s.get(Draft, draft_id)
            if draft.created < utcnow() - timedelta(days=1):
                raise ValueError("Черновик старше суток. Отправьте запись заново")
            data = dict(draft.payload)
            original = data.pop("original")
            if draft.kind == "append":
                payload = PrepareWorkout.model_validate(data)
                self._validate_codes(s, payload.sets)
                if payload.workout_id:
                    requested = s.get(Workout, payload.workout_id)
                    if not requested or requested.account != self.account or requested.day != payload.day:
                        raise ValueError("Тренировка недоступна")
                q = select(Workout).where(Workout.account == self.account, Workout.day == payload.day)
                workout = s.scalar(q.with_for_update())
                if workout is None:
                    try:
                        with s.begin_nested():
                            workout = Workout(account=self.account, day=payload.day, gym=payload.gym,
                                              trainer=payload.trainer, week=payload.week,
                                              program_session=payload.program_session)
                            s.add(workout)
                            s.flush()
                    except IntegrityError:
                        # Another writer may have created this date while we waited.
                        workout = s.scalar(q.with_for_update())
                        if workout is None:
                            raise
                trainer_change = {}
                if payload.trainer == "да" and workout.trainer != "да":
                    trainer_change = {"trainer_before": workout.trainer, "trainer_after": "да"}
                    workout.trainer = "да"
                created_ids = []
                for item in payload.sets:
                    ordinal = s.scalar(select(func.max(SetRecord.ordinal)).where(
                        SetRecord.workout_id == workout.id, SetRecord.exercise_code == item.exercise_code)) or 0
                    row = SetRecord(workout_id=workout.id, ordinal=ordinal + 1, original=original, mutation_tip=draft.id,
                                    **item.model_dump())
                    s.add(row)
                    s.flush()
                    created_ids.append(row.id)
                result = {"workout_id": workout.id, "set_ids": created_ids, "saved": len(created_ids),
                          **trainer_change}
            else:
                payload = PrepareEdit.model_validate(data)
                row = self._owned_set(s, payload.set_id)
                if row.version != payload.expected_version:
                    raise ValueError("Подход уже изменён. Подготовьте исправление заново")
                changes = payload.replacement.model_dump()
                if changes.get("raw_name") is None:
                    changes.pop("raw_name", None)
                fields = list(changes)
                before = {k: getattr(row, k) for k in fields} | {"uncertain": row.uncertain}
                before_tip = row.mutation_tip
                for key, value in changes.items():
                    setattr(row, key, value)
                row.uncertain = False
                row.version += 1
                row.mutation_tip = draft.id
                result = {"set_id": row.id, "before": before, "before_tip": before_tip,
                          "new_version": row.version, "saved": 1}
            draft.status, draft.result = "confirmed", result
            return result

    def cancel(self, draft_id=None):
        with self.db.session() as s:
            q = update(Draft).where(Draft.account == self.account, Draft.status == "pending")
            if draft_id is not None:
                q = q.where(Draft.id == draft_id)
            result = s.execute(q.values(status="cancelled"))
            return {"cancelled_drafts": result.rowcount}

    def undo(self, source=None):
        with self.db.session() as s:
            if source:
                previous = s.get(Operation, source)
                if previous:
                    if previous.account != self.account:
                        raise ValueError("Действие недоступно")
                    return previous.result
            draft = s.scalar(select(Draft).where(Draft.account == self.account, Draft.status == "confirmed")
                             .order_by(Draft.created.desc()).with_for_update())
            if not draft:
                raise ValueError("Нет действий бота для отмены. Импортированная история не удаляется")
            result = draft.result
            if draft.kind == "append":
                if "trainer_before" in result:
                    workout = s.get(Workout, result["workout_id"])
                    if workout.trainer != result["trainer_after"]:
                        raise ValueError("Отметка тренера изменилась после этой операции")
                    workout.trainer = result["trainer_before"]
                for set_id in result["set_ids"]:
                    row = self._owned_set(s, set_id)
                    if row.mutation_tip != draft.id:
                        raise ValueError("Сначала отмените исправления этих подходов")
                    row.deleted = True
                    row.version += 1
                    row.mutation_tip = None
            else:
                row = self._owned_set(s, result["set_id"])
                if row.mutation_tip != draft.id:
                    raise ValueError("Подход изменился после этой операции")
                for key, value in result["before"].items():
                    setattr(row, key, value)
                # Keep monotonic revisions; earlier edits cannot accidentally match stale drafts.
                row.version += 1
                row.mutation_tip = result.get("before_tip")
            draft.status = "undone"
            s.execute(update(Draft).where(Draft.account == self.account, Draft.status == "pending").values(status="superseded"))
            answer = {"undone": draft.kind, "note": "Отменено последнее сохранённое действие бота"}
            if source:
                s.add(Operation(key=source, account=self.account, result=answer))
            return answer

    def context(self, today=None):
        today = today or self.today()
        with self.db.session() as s:
            conversation = s.get(Conversation, self.account)
            pending = s.scalar(select(Draft).where(Draft.account == self.account, Draft.status == "pending")
                               .order_by(Draft.created.desc()))
            last = s.execute(select(SetRecord, Workout, Exercise).join(Workout).join(Exercise)
                             .where(Workout.account == self.account, SetRecord.deleted.is_(False))
                             .order_by(Workout.day.desc(), SetRecord.id.desc()).limit(1)).first()
            history = [{"role": turn["role"], "content": turn["content"]}
                       for turn in conversation.turns if turn.get("day") == str(today)] if conversation else []
            writing = s.get(WriteContext, self.account)
            default_day = writing.workout_day if writing and writing.message_day == today else today
            return {"recent_dialogue": history, "default_workout_day": str(default_day),
                    "pending_draft": {"id": pending.id, "payload": pending.payload} if pending else None,
                    "latest_set": serialize_set(*last) if last else None}

    def remember(self, user: str, assistant: str, *, today=None):
        with self.db.session() as s:
            obj = s.get(Conversation, self.account)
            if not obj:
                obj = Conversation(account=self.account, turns=[])
                s.add(obj)
            # Dialogue is bounded; the complete training history remains queryable in SQL.
            day = str(today or self.today())
            obj.turns = (obj.turns + [{"role": "user", "content": user[:6000], "day": day},
                                      {"role": "assistant", "content": assistant[:6000], "day": day}])[-12:]

    def token_usage(self, day: date):
        with self.db.session() as s:
            row = s.execute(select(func.coalesce(func.sum(Usage.input_tokens), 0),
                                   func.coalesce(func.sum(Usage.output_tokens), 0),
                                   func.coalesce(func.sum(Usage.audio_seconds), 0)).where(
                                       Usage.account == self.account, Usage.day == day)).one()
            return {"input_tokens": row[0], "output_tokens": row[1], "audio_seconds": row[2]}

    def log_usage(self, day, model, input_tokens=0, output_tokens=0, audio_seconds=0):
        with self.db.session() as s:
            s.add(Usage(account=self.account, day=day, model=model, input_tokens=input_tokens,
                        output_tokens=output_tokens, audio_seconds=audio_seconds))

    def csv_export(self):
        with self.db.session() as s:
            query = QuerySets(exercise_code=None, start=None, end=None, exclude_uncertain=False,
                              workout_id=None, min_weight=None, limit=40, offset=0)
            rows = s.execute(self._base(query).order_by(Workout.day, Workout.id, SetRecord.id)).all()
            output = io.StringIO(newline="")
            fields = ["date", "gym", "workout_id", "exercise", "exercise_code", "set_number", "weight",
                      "reps", "minutes", "rpe", "note", "uncertain_mapping", "tonnage", "estimated_1rm", "source_row", "set_id", "version"]
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", delimiter=";")
            writer.writeheader()
            for row in rows:
                data = serialize_set(*row)
                # Prevent spreadsheet formula execution for user-authored text fields.
                for k, v in data.items():
                    if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")):
                        data[k] = "'" + v
                writer.writerow(data)
            return output.getvalue().encode("utf-8-sig")

    def history_rows(self):
        """One row per live set. Dates stay ISO until the presentation layer."""
        query = QuerySets(exercise_code=None, start=None, end=None, exclude_uncertain=False,
                          workout_id=None, min_weight=None, limit=40, offset=0)
        with self.db.session() as s:
            rows = s.execute(self._base(query).order_by(Workout.day.desc(), Workout.id.desc(), SetRecord.id)).all()
            return [[str(w.day), e.name, r.raw_name, r.ordinal, r.weight, r.reps, r.minutes, r.rpe,
                     w.gym, w.trainer, w.week, w.program_session, r.note,
                     "Да" if r.uncertain else "", e.code, w.id, r.id] for r, w, e in rows]

    def exercise_rows(self):
        """Complete shared exercise catalog, including exercises with no recorded sets."""
        with self.db.session() as s:
            return [[e.code, e.name, e.muscle, e.equipment]
                    for e in s.scalars(select(Exercise).order_by(Exercise.code))]
