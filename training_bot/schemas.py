from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchExercises(StrictModel):
    query: str = Field(min_length=1, max_length=200)


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
    status: Literal["all", "completed", "open"]
    limit: int = Field(ge=1, le=10)


class RecordQuery(Period):
    exercise_code: str


class SummaryQuery(Period):
    group_by: Literal["month", "exercise"]


class SetInput(StrictModel):
    exercise_code: str = Field(min_length=1, max_length=24)
    weight: float | None = Field(ge=-1000, le=2000)
    reps: int | None = Field(ge=1, le=1000)
    minutes: float | None = Field(gt=0, le=1440)
    rpe: float | None = Field(ge=1, le=10)
    note: str | None = Field(max_length=1000)

    @model_validator(mode="after")
    def meaningful(self):
        if self.reps is None and self.minutes is None:
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


class PrepareEdit(StrictModel):
    set_id: int
    expected_version: int
    replacement: SetInput


class Empty(StrictModel):
    pass
