from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint, create_engine, event, inspect, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Exercise(Base):
    __tablename__ = "exercises"
    code: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), unique=True)
    muscle: Mapped[str | None] = mapped_column(String(200))
    equipment: Mapped[str | None] = mapped_column(String(100))


class Alias(Base):
    __tablename__ = "aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    normalized: Mapped[str] = mapped_column(String(500), index=True)
    exercise_code: Mapped[str] = mapped_column(ForeignKey("exercises.code"))
    note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("name", "exercise_code"),)


class Workout(Base):
    __tablename__ = "workouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(80), index=True)
    day: Mapped[object] = mapped_column(Date, index=True)
    gym: Mapped[str | None] = mapped_column(String(200))
    trainer: Mapped[str | None] = mapped_column(String(100))
    week: Mapped[int | None] = mapped_column(Integer)
    program_session: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SetRecord(Base):
    __tablename__ = "sets"
    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(ForeignKey("workouts.id"), index=True)
    exercise_code: Mapped[str] = mapped_column(ForeignKey("exercises.code"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    weight: Mapped[float | None] = mapped_column(Float)
    reps: Mapped[int | None] = mapped_column(Integer)
    minutes: Mapped[float | None] = mapped_column(Float)
    rpe: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    raw_name: Mapped[str | None] = mapped_column(Text)
    original: Mapped[str | None] = mapped_column(Text)
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    source_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    source_row: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    mutation_tip: Mapped[str | None] = mapped_column(String(32))


class ImportBatch(Base):
    __tablename__ = "imports"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    report: Mapped[dict] = mapped_column(JSON)


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account: Mapped[str] = mapped_column(String(80), index=True)
    source_message: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    result: Mapped[dict | None] = mapped_column(JSON)
    created: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("account", "source_message"),)


class Conversation(Base):
    __tablename__ = "conversations"
    account: Mapped[str] = mapped_column(String(80), primary_key=True)
    turns: Mapped[list] = mapped_column(JSON, default=list)


class Incoming(Base):
    __tablename__ = "incoming"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    answer: Mapped[str | None] = mapped_column(Text)
    created: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Operation(Base):
    __tablename__ = "operations"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    account: Mapped[str] = mapped_column(String(80))
    result: Mapped[dict] = mapped_column(JSON)


class Usage(Base):
    __tablename__ = "usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(80), index=True)
    day: Mapped[object] = mapped_column(Date, index=True)
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    audio_seconds: Mapped[int] = mapped_column(Integer, default=0)


Index("workouts_account_day", Workout.account, Workout.day)


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite:///") and not url.endswith(":memory:"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, pool_pre_ping=True)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def pragmas(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=10000")
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def init(self):
        Base.metadata.create_all(self.engine)
        # Additive migration for the first deployed schema. No data is rewritten.
        if "mutation_tip" not in {c["name"] for c in inspect(self.engine).get_columns("sets")}:
            with self.engine.begin() as connection:
                connection.execute(text("ALTER TABLE sets ADD COLUMN mutation_tip VARCHAR(32)"))

    @contextmanager
    def session(self):
        with self.sessions.begin() as session:
            yield session
