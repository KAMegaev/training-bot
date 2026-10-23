import argparse
import json
from pathlib import Path

from sqlalchemy import func, select

from .config import Settings, secure_local_files
from .db import Database, Exercise, SetRecord, Workout
from .importer import import_workbook


def main():
    parser = argparse.ArgumentParser(description="База тренировок: локальные команды подготовки")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Создать таблицы базы данных")
    commands.add_parser("status", help="Показать количество записей владельца")
    importer = commands.add_parser("import-xlsx", help="Импортировать историю владельца из Excel")
    importer.add_argument("path", type=Path)
    args = parser.parse_args()
    settings = Settings()
    db = Database(settings.database_url)
    try:
        db.init()
        if args.command == "import-xlsx":
            result = import_workbook(db, args.path)
        elif args.command == "status":
            with db.session() as s:
                result = {
                    "exercises": s.scalar(select(func.count()).select_from(Exercise)),
                    "workouts": s.scalar(select(func.count()).select_from(Workout).where(Workout.account == "owner")),
                    "sets": s.scalar(select(func.count()).select_from(SetRecord).join(Workout).where(
                        Workout.account == "owner", SetRecord.deleted.is_(False))),
                    "telegram_connected": False,
                    "note": "Это локальная подготовка; Telegram-обработчик ещё не реализован",
                }
        else:
            result = {"initialized": True}
        secure_local_files()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, FileNotFoundError, KeyError) as error:
        parser.exit(1, f"Ошибка: {error}\n")
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    main()
