import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import imageio_ffmpeg


async def transcribe(bot, voice, agent, journal):
    settings = agent.settings
    if voice.duration > settings.max_voice_seconds:
        raise ValueError(f"Пришлите голосовое короче {settings.max_voice_seconds} секунд")
    if voice.file_size and voice.file_size > 15 * 1024 * 1024:
        raise ValueError("Голосовое слишком большое: максимум 15 МБ")
    with TemporaryDirectory(prefix="training-voice-") as folder:
        source, target = Path(folder) / "voice.ogg", Path(folder) / "voice.wav"
        await bot.download(voice, destination=source)
        process = await asyncio.create_subprocess_exec(
            imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-t", str(settings.max_voice_seconds), "-ac", "1", "-ar", "16000", str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("Не удалось обработать аудио за отведённое время") from None
        if process.returncode or not target.exists():
            raise ValueError("Не удалось прочитать аудио. Пришлите сообщение текстом")
        with target.open("rb") as audio:
            result = await agent.client.audio.transcriptions.create(
                model=settings.transcription_model, file=audio,
                prompt="Запись тренировки: упражнения, веса в килограммах, подходы и повторения.")
        from datetime import datetime
        journal.log_usage(datetime.now(settings.tz).date(), settings.transcription_model,
                          audio_seconds=voice.duration)
        if not result.text.strip():
            raise ValueError("Речь не распознана. Пришлите сообщение текстом")
        return result.text.strip()
