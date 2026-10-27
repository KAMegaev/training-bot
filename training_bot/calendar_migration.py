"""Explicit, transactional upgrade from sessions to one workout per account/date."""
from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from .db import Draft, Operation, SetRecord, Workout, WriteContext


def migrate_calendar(db, timezone_name="Europe/Moscow"):
    key = "migration:calendar-workouts:v1"
    with db.engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("LOCK TABLE workouts, sets, drafts IN ACCESS EXCLUSIVE MODE"))
        elif connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        with Session(connection, expire_on_commit=False) as s, s.begin():
            previous = s.get(Operation, key)
            indexes = inspect(connection).get_indexes("workouts")
            unique = any(index["name"] == "workouts_account_day" and index["unique"] for index in indexes)
            if previous and unique:
                return {"merged_workouts": previous.result["merged_workouts"],
                        "total_sets_preserved": previous.result["total_sets_preserved"], "already_migrated": True}
            count_before = s.scalar(select(func.count()).select_from(SetRecord))
            grouped = defaultdict(list)
            for workout in s.scalars(select(Workout).order_by(Workout.id)):
                grouped[(workout.account, workout.day)].append(workout)
            mapping, before_workouts, before_sets = {}, [], []
            metadata = ("gym", "trainer", "week", "program_session")
            for (account, day), workouts in grouped.items():
                if len(workouts) == 1:
                    continue
                keep = workouts[0]
                for field in metadata:
                    # Refuse conflicting historical metadata rather than lose it.
                    values = {getattr(w, field) for w in workouts if getattr(w, field) is not None}
                    if len(values) > 1:
                        raise ValueError(f"Конфликт поля {field} за {day}; требуется сверка перед объединением")
                before_workouts.extend({"id": w.id, "account": account, "day": str(day),
                    "status": w.status, **{field: getattr(w, field) for field in metadata}} for w in workouts)
                for field in metadata:
                    if getattr(keep, field) is None:
                        setattr(keep, field, next((getattr(w, field) for w in workouts
                                                  if getattr(w, field) is not None), None))
                ids = [w.id for w in workouts]
                ordinals = defaultdict(int)
                for row in s.scalars(select(SetRecord).where(SetRecord.workout_id.in_(ids)).order_by(SetRecord.id)):
                    before_sets.append({"id": row.id, "workout_id": row.workout_id, "ordinal": row.ordinal})
                    row.workout_id = keep.id
                    ordinals[row.exercise_code] += 1
                    row.ordinal = ordinals[row.exercise_code]
                for duplicate in workouts[1:]:
                    mapping[duplicate.id] = keep.id
                s.flush()
                for duplicate in workouts[1:]:
                    s.delete(duplicate)
                s.flush()
            if mapping:
                for draft in s.scalars(select(Draft)):
                    for field in ("payload", "result"):
                        value = getattr(draft, field)
                        if value and value.get("workout_id") in mapping:
                            setattr(draft, field, value | {"workout_id": mapping[value["workout_id"]]})
            # Preserve today's latest valid write when upgrading a running bot.
            tz = ZoneInfo(timezone_name)
            today = datetime.now(tz).date()
            seeded = set()
            for draft in s.scalars(select(Draft).where(Draft.kind == "append",
                    Draft.status.in_(["pending", "confirmed"])).order_by(Draft.created.desc(), Draft.id.desc())):
                if draft.account in seeded or draft.created.replace(tzinfo=timezone.utc).astimezone(tz).date() != today:
                    continue
                if not s.get(WriteContext, draft.account) and draft.payload.get("day"):
                    s.add(WriteContext(account=draft.account, message_day=today,
                                       workout_day=date.fromisoformat(draft.payload["day"])))
                seeded.add(draft.account)
            s.flush()
            if not unique:
                connection.execute(text("DROP INDEX IF EXISTS workouts_account_day"))
                connection.execute(text("CREATE UNIQUE INDEX workouts_account_day ON workouts (account, day)"))
            assert s.scalar(select(func.count()).select_from(SetRecord)) == count_before
            result = {"merged_workouts": len(mapping), "workout_id_mapping": mapping,
                      "workouts_before": before_workouts, "sets_before": before_sets,
                      "total_sets_preserved": count_before}
            if not previous:
                s.add(Operation(key=key, account="system", result=result))
            return {"merged_workouts": len(mapping), "total_sets_preserved": count_before}
