from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchExercises(StrictModel):
    query: str = Field(min_length=1, max_length=200)


class SearchExercisesBatch(StrictModel):
    queries: list[str] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def valid_queries(self):
        if any(not query.strip() or len(query) > 200 for query in self.queries):
            raise ValueError("Каждый запрос должен содержать одно название длиной 1–200 символов")
        return self


class Period(StrictModel):
    exercise_code: str | None
    start: date | None
    end: date | None
    exclude_uncertain: bool

    @model_validator(mode="after")
    def dates_ordered(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("Начало периода позже конца")
        return self


class QuerySets(Period):
    workout_id: int | None
    min_weight: float | None
    limit: int = Field(ge=1, le=40)
    offset: int = Field(ge=0, le=100000)


class QueryWorkouts(Period):
    limit: int = Field(ge=1, le=10)


class RecordQuery(Period):
    exercise_code: str


class SummaryQuery(Period):
    group_by: Literal["month", "exercise"]


class SetInput(StrictModel):
    exercise_code: str = Field(min_length=1, max_length=24)
    raw_name: str | None = Field(default=None, max_length=500,
                                 description="Название упражнения дословно из сообщения пользователя; неизвестное — null")
    weight: float | None = Field(ge=-1000, le=2000)
    reps: int | None = Field(ge=1, le=1000)
    minutes: float | None = Field(gt=0, le=1440)
    rpe: float | None = Field(ge=1, le=10)
    note: str | None = Field(max_length=1000)

    @model_validator(mode="after")
    def meaningful(self):
        # AB06 is the catalog's "Комплекс экстензия-пресс": the user records
        # completed rounds without a weight or a repetition count.
        if self.reps is None and self.minutes is None and self.exercise_code != "AB06":
            raise ValueError("Укажите повторения или время; неизвестное число нельзя выдумывать")
        return self


class PrepareWorkout(StrictModel):
    day: date
    workout_id: int | None
    gym: str | None = Field(max_length=200)
    trainer: str | None = Field(max_length=100)
    week: int | None = Field(ge=1, le=52)
    program_session: int | None = Field(ge=1, le=20)
    sets: list[SetInput] = Field(min_length=1, max_length=60)


class SetGroup(SetInput):
    count: int = Field(default=1, ge=1, le=60,
                       description="Количество одинаковых подходов; программа создаст отдельную строку для каждого")


class PrepareWorkoutRequest(PrepareWorkout):
    """Compact model payload; the journal still accepts individual sets only."""
    continues_previous_request: bool = Field(default=False,
        description="true только при ответе на уточняющий вопрос по ещё не подготовленной записи; для новой записи false")
    sets: list[SetGroup] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def total_sets(self):
        if sum(group.count for group in self.sets) > 60:
            raise ValueError("За одно сохранение можно добавить не более 60 подходов")
        return self

    def expand(self) -> PrepareWorkout:
        return PrepareWorkout.model_validate({
            **self.model_dump(exclude={"sets", "continues_previous_request"}),
            "sets": [group.model_dump(exclude={"count"})
                     for group in self.sets for _ in range(group.count)],
        })


class PrepareEdit(StrictModel):
    set_id: int
    expected_version: int
    replacement: SetInput


class ExerciseSelection(StrictModel):
    exercise_index: int = Field(ge=0, description="Поле index из parsed_workout.exercises, НЕ номер упражнения в исходном тексте")
    exercise_code: str = Field(min_length=1, max_length=24)


class PrepareParsedWorkout(StrictModel):
    exercises: list[ExerciseSelection] = Field(min_length=1, max_length=30)


class Empty(StrictModel):
    pass
