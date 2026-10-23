import csv
import io
import json
import uuid
from datetime import date, timedelta
from difflib import SequenceMatcher

from sqlalchemy import case, extract, func, select, update

from .db import (Alias, Conversation, Database, Draft, Exercise, Operation, SetRecord,
                 Usage, Workout, utcnow)
from .importer import normalize
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
            "exercise": exercise.name, "set_number": row.ordinal, "weight": row.weight,
            "reps": row.reps, "minutes": row.minutes, "rpe": row.rpe, "note": row.note,
            "uncertain_mapping": row.uncertain, "source_row": row.source_row,
            **metrics(row.weight, row.reps)}


class Journal:
    def __init__(self, db: Database, account: str):
        self.db, self.account = db, account

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
        if query.status != "all":
            q = q.where(Workout.status == query.status)
        q = q.with_only_columns(Workout.id, Workout.day, Workout.gym, Workout.status,
                                func.count(SetRecord.id).label("matching_sets")).group_by(
                                    Workout.id, Workout.day, Workout.gym, Workout.status)
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

    def prepare(self, kind: str, payload, source_message: str, original: str):
        data = payload.model_dump(mode="json")
        data["original"] = original
        with self.db.session() as s:
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
            return {"draft_id": draft.id, "status": "pending", "preview": self._preview(s, draft),
                    "instruction": "Это черновик. Изменения записываются только кнопкой пользователя."}

    def _preview(self, s, draft):
        data = draft.payload
        lines = [f"Тренировка {data['day']}" if draft.kind == "append" else f"Исправление подхода #{data['set_id']}"]
        items = data["sets"] if draft.kind == "append" else [data["replacement"]]
        for item in items:
            exercise = s.get(Exercise, item["exercise_code"])
            weight = "вес не указан" if item["weight"] is None else f"{item['weight']:g} кг"
            performance = f"{weight} × {item['reps']}" if item["reps"] else f"{item['minutes']:g} мин"
            lines.append(f"• {exercise.name}: {performance}" + (f"; {item['note']}" if item["note"] else "")
                         + (f"; RPE {item['rpe']:g}" if item["rpe"] else ""))
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
                    workout = s.get(Workout, payload.workout_id)
                    if not workout or workout.account != self.account or workout.day != payload.day:
                        raise ValueError("Тренировка недоступна")
                else:
                    q = select(Workout).where(Workout.account == self.account, Workout.day == payload.day,
                                               Workout.status == "open")
                    if payload.gym:
                        q = q.where(Workout.gym == payload.gym)
                    workout = s.scalar(q.order_by(Workout.id.desc()))
                    if workout is None:
                        workout = Workout(account=self.account, day=payload.day, gym=payload.gym,
                                          trainer=payload.trainer, week=payload.week,
                                          program_session=payload.program_session)
                        s.add(workout)
                        s.flush()
                created_ids = []
                for item in payload.sets:
                    ordinal = s.scalar(select(func.max(SetRecord.ordinal)).where(
                        SetRecord.workout_id == workout.id, SetRecord.exercise_code == item.exercise_code)) or 0
                    row = SetRecord(workout_id=workout.id, ordinal=ordinal + 1, original=original, mutation_tip=draft.id,
                                    **item.model_dump())
                    s.add(row)
                    s.flush()
                    created_ids.append(row.id)
                result = {"workout_id": workout.id, "set_ids": created_ids, "saved": len(created_ids)}
            else:
                payload = PrepareEdit.model_validate(data)
                row = self._owned_set(s, payload.set_id)
                if row.version != payload.expected_version:
                    raise ValueError("Подход уже изменён. Подготовьте исправление заново")
                fields = list(type(payload.replacement).model_fields)
                before = {k: getattr(row, k) for k in fields} | {"uncertain": row.uncertain}
                before_tip = row.mutation_tip
                for key, value in payload.replacement.model_dump().items():
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

    def finish(self):
        with self.db.session() as s:
            result = s.execute(update(Workout).where(Workout.account == self.account, Workout.status == "open")
                               .values(status="completed"))
            return {"completed_workouts": result.rowcount}

    def context(self):
        with self.db.session() as s:
            conversation = s.get(Conversation, self.account)
            pending = s.scalar(select(Draft).where(Draft.account == self.account, Draft.status == "pending")
                               .order_by(Draft.created.desc()))
            last = s.execute(select(SetRecord, Workout, Exercise).join(Workout).join(Exercise)
                             .where(Workout.account == self.account, SetRecord.deleted.is_(False))
                             .order_by(Workout.day.desc(), SetRecord.id.desc()).limit(1)).first()
            return {"recent_dialogue": conversation.turns if conversation else [],
                    "pending_draft": {"id": pending.id, "payload": pending.payload} if pending else None,
                    "latest_set": serialize_set(*last) if last else None}

    def remember(self, user: str, assistant: str):
        with self.db.session() as s:
            obj = s.get(Conversation, self.account)
            if not obj:
                obj = Conversation(account=self.account, turns=[])
                s.add(obj)
            # Dialogue is bounded; the complete training history remains queryable in SQL.
            obj.turns = (obj.turns + [{"role": "user", "content": user[:6000]},
                                      {"role": "assistant", "content": assistant[:6000]}])[-12:]

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
