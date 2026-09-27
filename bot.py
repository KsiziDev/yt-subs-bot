import asyncio
import json
import os
import re
import tempfile
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command

import yt_dlp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip().isdigit()
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

URL_RE = re.compile(r"https?://(www\.)?(youtube\.com|youtu\.be|m\.youtube\.com)/\S+")

TG_MSG_LIMIT = 4000  # с запасом от 4096


def check_user(message: Message) -> bool:
    if not ALLOWED_USERS:
        return True
    return message.from_user and message.from_user.id in ALLOWED_USERS


def _fmt_ts(seconds: float, comma: bool = False) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if comma:
        ms = int((seconds - int(seconds)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def _json3_to_plain_and_srt(subs: dict):
    """Возвращает (plain_text, srt_text) из авто-субтитров json3."""
    plain_lines = []
    srt_lines = []
    idx = 1
    for event in subs.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        start = event.get("tStartMs", 0) / 1000
        dur = event.get("dDurationMs", 2000) / 1000
        plain_lines.append(text)
        srt_lines.append(
            f"{idx}\n{_fmt_ts(start, comma=True)} --> {_fmt_ts(start + dur, comma=True)}\n{text}\n"
        )
        idx += 1
    return "\n".join(plain_lines), "\n".join(srt_lines)


def _srt_to_plain(srt_text: str) -> str:
    """Убирает таймкоды и индексы из SRT, оставляя чистый текст."""
    out = []
    for block in srt_text.split("\n\n"):
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        # пропускаем строку с индексом и строку с таймкодом
        text_lines = [l for l in lines if not l.strip().isdigit() and "-->" not in l]
        if text_lines:
            out.append(" ".join(text_lines))
    return "\n".join(out)


async def fetch_subtitles(url: str, workdir: Path):
    """Пробует сначала ручные субтитры, потом авто. Возвращает (plain, srt)."""
    for auto in (False, True):
        opts = {
            "skip_download": True,
            "writesubtitles": not auto,
            "writeautomaticsub": auto,
            "subtitleslangs": ["ru", "en"],
            "subtitlesformat": "srt/json3/best",
            "outtmpl": str(workdir / "subs"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

        def _dl():
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        try:
            await asyncio.to_thread(_dl)
        except Exception as e:
            log.warning("subs download failed (auto=%s): %s", auto, e)

        # .srt
        srt_files = list(workdir.glob("subs*.srt"))
        if srt_files:
            srt_text = srt_files[0].read_text(encoding="utf-8", errors="ignore")
            return _srt_to_plain(srt_text), srt_text

        # .json3
        json_files = list(workdir.glob("subs*.json3"))
        if json_files:
            data = json.loads(json_files[0].read_text(encoding="utf-8", errors="ignore"))
            return _json3_to_plain_and_srt(data)

    return None, None


async def send_long_text(message: Message, text: str):
    """Отправляет длинный текст несколькими сообщениями."""
    for i in range(0, len(text), TG_MSG_LIMIT):
        await message.answer(text[i:i + TG_MSG_LIMIT])


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not check_user(message):
        await message.answer("Доступ запрещён.")
        return
    await message.answer(
        "Пришли ссылку на YouTube — пришлю субтитры текстом и файлом .txt.\n"
        "⚠️ Только для личного использования."
    )


@dp.message(F.text)
async def handle_url(message: Message):
    if not check_user(message):
        await message.answer("Доступ запрещён.")
        return

    url = message.text.strip()
    if not URL_RE.match(url):
        await message.answer("Это не похоже на ссылку YouTube.")
        return

    status = await message.answer("⏳ Достаю субтитры...")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            plain, srt = await fetch_subtitles(url, Path(tmp))

        if not plain:
            await message.answer("⚠️ Субтитры не найдены (ни ручные, ни авто).")
            return

        # 1) текстом
        await send_long_text(message, plain)

        # 2) файлом .txt
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(plain)
            txt_path = f.name

        await message.answer_document(
            FSInputFile(txt_path, filename="subtitles.txt"),
            caption="Субтитры файлом",
        )
        os.unlink(txt_path)

    except Exception as e:
        log.exception("error")
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
