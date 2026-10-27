import argparse
import asyncio
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
    commands.add_parser("migrate-calendar", help="Объединить тренировки по дате и запретить дубли")
    commands.add_parser("status", help="Показать количество записей владельца")
    commands.add_parser("run", help="Запустить Telegram long polling")
    commands.add_parser("disconnect-webhook", help="Отключить прежний webhook для переключения бота")
    syncer = commands.add_parser("sync-sheets", help="Разово обновить Google-таблицу владельца")
    syncer.add_argument("--force", action="store_true")
    importer = commands.add_parser("import-xlsx", help="Импортировать историю владельца из Excel")
    importer.add_argument("path", type=Path)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "disconnect-webhook":
        from .telegram import disconnect_webhook
        asyncio.run(disconnect_webhook(settings))
        print('{"webhook_disconnected": true}')
        return
    db = Database(settings.database_url)
    try:
        if args.command not in {"run", "sync-sheets"}:
            db.init()
        if args.command == "run":
            from .telegram import run
            asyncio.run(run(settings, db))
            return
        if args.command == "sync-sheets":
            from .sheets import SheetMirror
            result = SheetMirror(settings, db).sync_once(force=args.force)
            print(json.dumps(result, ensure_ascii=False))
            return
        if args.command == "migrate-calendar":
            from .calendar_migration import migrate_calendar
            result = migrate_calendar(db, settings.timezone)
        elif args.command == "import-xlsx":
            result = import_workbook(db, args.path)
        elif args.command == "status":
            with db.session() as s:
                result = {
                    "exercises": s.scalar(select(func.count()).select_from(Exercise)),
                    "workouts": s.scalar(select(func.count()).select_from(Workout).where(Workout.account == "owner")),
                    "sets": s.scalar(select(func.count()).select_from(SetRecord).join(Workout).where(
                        Workout.account == "owner", SetRecord.deleted.is_(False))),
                    "owner_configured": settings.owner_telegram_id > 0,
                    "note": "Количество записей в базе; состояние процесса проверяется отдельно",
                }
        else:
            result = {"initialized": True}
        if settings.database_url.startswith("sqlite:"):
            secure_local_files()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, FileNotFoundError, KeyError) as error:
        parser.exit(1, f"Ошибка: {error}\n")
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    main()
