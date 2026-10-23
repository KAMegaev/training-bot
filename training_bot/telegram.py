import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from openai import OpenAIError
from sqlalchemy import select

from .agent import Agent, Reply, api_error_reply
from .audio import transcribe
from .config import Settings
from .db import Database, Draft, Incoming
from .schemas import QuerySets, QueryWorkouts
from .service import Journal
from .sheets import SheetMirror


HELP = """Записывайте тренировку текстом или голосом, например:
«Жим штанги лёжа: 60 кг на 10, три подхода».
Проверьте черновик и нажмите «Сохранить».

Можно спросить: «Какой мой рекорд в жиме за всё время?»

/last — последняя тренировка
/pending — текущий черновик
/cancel — отменить черновик
/undo — отменить последнее сохранение бота
/finish — завершить тренировку
/export — выгрузить все подходы в CSV
/table — история в Google-таблице
/usage — расход ИИ за сегодня
/id — ваш Telegram ID
"""


def keyboard(draft_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Сохранить", callback_data="save:" + draft_id),
        InlineKeyboardButton(text="Отменить", callback_data="cancel:" + draft_id)]])


async def send_reply(message, reply):
    # Telegram limits text to 4096 characters; the confirmation belongs to the last part.
    parts = [reply.text[i:i + 3800] for i in range(0, len(reply.text), 3800)] or ["Готово"]
    for index, part in enumerate(parts):
        await message.answer(part, reply_markup=keyboard(reply.draft_id)
                             if reply.draft_id and index == len(parts) - 1 else None)


class Application:
    def __init__(self, settings, db, agent):
        self.settings, self.db, self.agent = settings, db, agent

    def journal(self, user_id):
        account = self.settings.account(user_id)
        return Journal(self.db, account) if account else None

    def recovered_reply(self, journal, source):
        """Recover after a process restart between preparing a draft and sending it."""
        with self.db.session() as s:
            receipt = s.get(Incoming, source)
            if receipt and receipt.status == "done" and receipt.answer:
                return Reply.loads(receipt.answer)
            draft = s.scalar(select(Draft).where(Draft.account == journal.account,
                                                  Draft.source_message == source))
            if draft and draft.status == "pending":
                return Reply(journal._preview(s, draft) + "\n\nПроверьте запись и нажмите «Сохранить».", draft.id)
            if draft and draft.status == "confirmed":
                return Reply("Эта запись уже сохранена.")
            if receipt is None:
                s.add(Incoming(key=source, status="processing"))
        return None

    def complete(self, source, reply):
        with self.db.session() as s:
            receipt = s.get(Incoming, source)
            if receipt is None:
                receipt = Incoming(key=source, status="done")
                s.add(receipt)
            receipt.status, receipt.answer = "done", reply.dumps()

    def pending(self, journal):
        with self.db.session() as s:
            draft = s.scalar(select(Draft).where(Draft.account == journal.account, Draft.status == "pending")
                             .order_by(Draft.created.desc()))
            return Reply(journal._preview(s, draft) + "\n\nПроверьте запись и нажмите «Сохранить».", draft.id) if draft else Reply("Ожидающего черновика нет.")

    def last(self, journal):
        period = dict(exercise_code=None, start=None, end=None, exclude_uncertain=False)
        workouts = journal.workouts(QueryWorkouts(**period, status="all", limit=1))
        if not workouts:
            return Reply("Тренировок пока нет. Пришлите упражнения, веса и повторения.")
        workout = workouts[0]
        lines = [f"Последняя тренировка: {workout['day']}" + (f", {workout['gym']}" if workout["gym"] else "")]
        offset = 0
        while True:
            result = journal.sets(QuerySets(**period, workout_id=workout["id"], min_weight=None, limit=40, offset=offset))
            for row in result["sets"]:
                weight = "вес не указан" if row["weight"] is None else f"{row['weight']:g} кг"
                performance = f"{weight} × {row['reps']}" if row["reps"] else f"{row['minutes']:g} мин" if row["minutes"] else weight
                lines.append(f"• {row['exercise']}: {performance}" + (" [сопоставление требует проверки]" if row["uncertain_mapping"] else ""))
            if not result["has_more"]:
                break
            offset += 40
        return Reply("\n".join(lines))

    async def handle_message(self, message: Message):
        if message.chat.type != ChatType.PRIVATE or not message.from_user:
            return
        command = (message.text or "").split(maxsplit=1)[0].split("@")[0].lower() if message.text else ""
        if command == "/id":
            await message.answer(f"Ваш Telegram ID: {message.from_user.id}")
            return
        journal = self.journal(message.from_user.id)
        if journal is None:
            await message.answer(f"Доступ ещё не настроен. Ваш Telegram ID: {message.from_user.id}. Передайте его владельцу для подключения.")
            return
        source = f"message:{message.chat.id}:{message.message_id}"
        recovered = self.recovered_reply(journal, source)
        if recovered:
            await send_reply(message, recovered)
            return
        try:
            if command in {"/start", "/help"}:
                reply = Reply(HELP)
            elif command == "/last":
                reply = self.last(journal)
            elif command == "/pending":
                reply = self.pending(journal)
            elif command == "/cancel":
                result = journal.cancel()
                reply = Reply("Черновик отменён." if result["cancelled_drafts"] else "Ожидающего черновика нет.")
            elif command == "/undo":
                journal.undo(source=source)
                reply = Reply("Последнее сохранённое действие бота отменено.")
            elif command == "/finish":
                result = journal.finish()
                reply = Reply(f"Завершено тренировок: {result['completed_workouts']}.")
            elif command == "/usage":
                usage = journal.token_usage(datetime.now(self.settings.tz).date())
                reply = Reply(f"Сегодня: входных токенов {usage['input_tokens']}, выходных {usage['output_tokens']}, аудио {usage['audio_seconds']} сек. Лимит токенов: {self.settings.daily_token_limit}.")
            elif command == "/export":
                await message.answer_document(BufferedInputFile(journal.csv_export(), filename="workouts.csv"))
                reply = Reply("Выгрузка всех подходов готова.")
            elif command == "/table":
                if journal.account != "owner":
                    reply = Reply("Google-таблица пока подключена только для владельца.")
                elif not self.settings.google_spreadsheet_id:
                    reply = Reply("Google-таблица ещё не подключена.")
                else:
                    status = ("Изменения в журнале появляются примерно в течение минуты."
                              if self.settings.google_credentials_file.is_file()
                              else "Автообновление ещё не подключено.")
                    reply = Reply("История тренировок: https://docs.google.com/spreadsheets/d/" + self.settings.google_spreadsheet_id
                                  + "/edit#gid=" + str(self.settings.google_sheet_tab_id)
                                  + "\n" + status)
            elif command.startswith("/"):
                reply = Reply("Неизвестная команда. Список команд: /help")
            else:
                text = message.text
                prefix = ""
                if message.voice:
                    today = datetime.now(self.settings.tz).date()
                    usage = journal.token_usage(today)
                    if usage["input_tokens"] + usage["output_tokens"] >= self.settings.daily_token_limit or usage["audio_seconds"] + message.voice.duration > self.settings.daily_audio_seconds:
                        raise ValueError("Достигнут дневной лимит ИИ или аудио. Пришлите сообщение завтра.")
                    text = await transcribe(message.bot, message.voice, self.agent, journal)
                    prefix = "Распознано: «" + text[:1500] + "»\n\n"
                if not text:
                    reply = Reply("Пришлите текст или голосовое сообщение.")
                elif len(text) > 6000:
                    reply = Reply("Сообщение слишком длинное. Разделите его на части до 6000 символов.")
                else:
                    await message.bot.send_chat_action(message.chat.id, "typing")
                    reply = await self.agent.answer(journal, text, source)
                    reply.text = prefix + reply.text
            self.complete(source, reply)
        except OpenAIError as error:
            reply = api_error_reply(error)
            # A failed request may be retried after a restart or by a new message.
        except ValueError as error:
            reply = Reply(str(error))
            self.complete(source, reply)
        except Exception as error:
            # Only exception type: API URLs may contain bot tokens; never log full errors.
            logging.getLogger(__name__).error("Message failed: %s", type(error).__name__)
            reply = Reply("Не удалось обработать сообщение. Проверьте /pending и попробуйте ещё раз.")
        await send_reply(message, reply)

    async def handle_callback(self, callback: CallbackQuery):
        if not isinstance(callback.message, Message) or callback.message.chat.type != ChatType.PRIVATE:
            return
        journal = self.journal(callback.from_user.id)
        if journal is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        action, draft_id = (callback.data or "").split(":", 1)
        try:
            if action == "save":
                result = journal.confirm(draft_id)
                text = "Уже сохранено." if result.get("already_saved") else f"Сохранено подходов: {result['saved']}."
            else:
                journal.cancel(draft_id)
                text = "Черновик отменён."
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            await callback.message.answer(text)
        except ValueError as error:
            await callback.message.answer(str(error))


async def run(settings: Settings, db: Database):
    settings.require_secrets()
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    agent = Agent(settings)
    application = Application(settings, db, agent)
    router = Router()
    router.message.register(application.handle_message)
    router.callback_query.register(application.handle_callback, F.data.startswith("save:") | F.data.startswith("cancel:"))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    stop = asyncio.Event()
    mirror = asyncio.create_task(SheetMirror(settings, db).run(stop))
    try:
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ValueError("Настроен старый webhook. Для переключения выполните training-bot disconnect-webhook")
        logging.getLogger(__name__).warning("Polling started; owner configured: %s", settings.owner_telegram_id > 0)
        # Sequential handling: Telegram acknowledges the update on the next getUpdates.
        # No task per update, no concurrent writes for the same account.
        await dispatcher.start_polling(bot, handle_as_tasks=False, close_bot_session=False,
                                       allowed_updates=["message", "callback_query"])
    finally:
        stop.set()
        mirror.cancel()
        await asyncio.gather(mirror, return_exceptions=True)
        await agent.close()
        await bot.session.close()


async def disconnect_webhook(settings):
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    finally:
        await bot.session.close()
