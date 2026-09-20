from __future__ import annotations
"""
Telegram-бот: присылает новые объявления OLX.ua (аренда квартир, Полтава).

Переменные окружения:
  BOT_TOKEN      - токен от @BotFather
  OLX_API_URL    - ссылка на запрос api/v1/offers/ из DevTools (см. README ниже)
  POLL_INTERVAL  - период опроса в секундах (по умолчанию 60)
  MAX_AGE_HOURS  - не присылать объявления старше N часов (по умолчанию 24)
  STATE_FILE     - файл состояния (по умолчанию state.json)
"""
import asyncio
import html
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from curl_cffi.requests import AsyncSession
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("olx-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OLX_API_URL = os.environ["OLX_API_URL"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "24"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
SEEN_LIMIT = 3000

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8",
    "Referer": "https://www.olx.ua/",
}


class OlxHTTPError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


# ---------- состояние ----------
class State:
    def __init__(self, path: Path):
        self.path = path
        self.subscribers: set[int] = set()
        self.seen: list[str] = []
        self.initialized = False
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.subscribers = set(data.get("subscribers", []))
            self.seen = data.get("seen", [])
            self.initialized = data.get("initialized", False)
        self._seen_set = set(self.seen)

    def save(self):
        self.seen = self.seen[-SEEN_LIMIT:]
        self._seen_set = set(self.seen)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "subscribers": sorted(self.subscribers),
                    "seen": self.seen,
                    "initialized": self.initialized,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def is_seen(self, offer_id: str) -> bool:
        return offer_id in self._seen_set

    def mark_seen(self, offer_id: str):
        if offer_id not in self._seen_set:
            self.seen.append(offer_id)
            self._seen_set.add(offer_id)


state = State(STATE_FILE)


# ---------- OLX ----------
def build_url() -> str:
    parts = urlparse(OLX_API_URL)
    query = dict(parse_qsl(parts.query))
    query.update({"offset": "0", "limit": "50", "sort_by": "created_at:desc"})
    return urlunparse(parts._replace(query=urlencode(query)))


async def fetch_offers(session: AsyncSession) -> list[dict]:
    r = await session.get(build_url(), headers=HEADERS, timeout=30, impersonate="chrome")
    if r.status_code != 200:
        raise OlxHTTPError(r.status_code)
    return r.json().get("data", [])


def is_fresh(offer: dict) -> bool:
    raw = offer.get("created_time")
    if not raw:
        return True
    try:
        created = datetime.fromisoformat(raw)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    return age <= MAX_AGE_HOURS


def format_offer(offer: dict) -> tuple[str, str | None]:
    title = html.escape(offer.get("title", "Без названия"))
    lines = [f"🏠 <b>{title}</b>"]

    extras = []
    for p in offer.get("params", []):
        value = p.get("value") or {}
        label = value.get("label") or value.get("key")
        if p.get("key") == "price":
            if label:
                lines.append(f"💰 <b>{html.escape(str(label))}</b>")
        elif label and len(extras) < 4:
            extras.append(f"{html.escape(str(p.get('name', '')))}: {html.escape(str(label))}")
    if extras:
        lines.append("📐 " + " • ".join(extras))

    loc = offer.get("location") or {}
    place = ", ".join(
        x for x in ((loc.get("city") or {}).get("name"), (loc.get("district") or {}).get("name")) if x
    )
    if place:
        lines.append(f"📍 {html.escape(place)}")

    desc = (offer.get("description") or "").replace("<br />", "\n").replace("<br>", "\n").strip()
    if desc:
        desc = desc[:300] + ("…" if len(desc) > 300 else "")
        lines.append("\n" + html.escape(desc))

    lines.append(f"\n🔗 {offer.get('url', '')}")

    photo = None
    photos = offer.get("photos") or []
    if photos and photos[0].get("link"):
        photo = photos[0]["link"].replace("{width}", "800").replace("{height}", "600")

    return "\n".join(lines), photo


# ---------- отправка ----------
async def send_to(bot: Bot, chat_id: int, text: str, photo: str | None) -> bool:
    for _ in range(3):
        try:
            if photo:
                try:
                    await bot.send_photo(chat_id, photo, caption=text[:1024])
                    return True
                except TelegramForbiddenError:
                    raise
                except TelegramRetryAfter:
                    raise
                except Exception:
                    pass  # не получилось с фото - шлём текстом
            await bot.send_message(chat_id, text, disable_web_page_preview=False)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            log.info("Пользователь %s заблокировал бота, удаляю", chat_id)
            state.subscribers.discard(chat_id)
            state.save()
            return False
        except Exception:
            log.exception("Ошибка отправки в %s", chat_id)
            return False
    return False


async def broadcast(bot: Bot, offer: dict):
    text, photo = format_offer(offer)
    ok = 0
    for chat_id in list(state.subscribers):
        if await send_to(bot, chat_id, text, photo):
            ok += 1
        await asyncio.sleep(0.05)
    log.info("Отправлено %d из %d подписчиков", ok, len(state.subscribers))


# ---------- цикл опроса ----------
async def poll_loop(bot: Bot):
    async with AsyncSession() as session:
        while True:
            try:
                offers = await fetch_offers(session)
                log.info("Получено объявлений: %d", len(offers))

                if not state.initialized:
                    # первый запуск: запоминаем текущие, ничего не шлём
                    for o in offers:
                        state.mark_seen(str(o["id"]))
                    state.initialized = True
                    state.save()
                    log.info("Первый запуск: %d объявлений записано как «уже виденные»", len(offers))
                else:
                    new = [o for o in offers if not state.is_seen(str(o["id"]))]
                    changed = False
                    for o in reversed(new):  # от старых к новым
                        fresh = is_fresh(o)
                        log.info(
                            "Новое: %s | создано %s | %s | подписчиков: %d",
                            o.get("title", "")[:60],
                            o.get("created_time"),
                            "свежее" if fresh else "старое (пропускаю)",
                            len(state.subscribers),
                        )
                        if fresh and not state.subscribers:
                            log.warning("Нет подписчиков — отправьте боту /start. Объявление не помечаю как виденное.")
                            continue
                        state.mark_seen(str(o["id"]))
                        changed = True
                        if fresh:
                            await broadcast(bot, o)
                    if changed:
                        state.save()
            except OlxHTTPError as e:
                log.warning("OLX ответил %s (возможно, блокировка/лимит)", e.status)
            except Exception:
                log.exception("Ошибка опроса")
            await asyncio.sleep(POLL_INTERVAL)


# ---------- команды ----------
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(m: Message):
    state.subscribers.add(m.chat.id)
    state.save()
    await m.answer(
        "✅ Подписка включена. Буду присылать новые объявления об аренде "
        "квартир в Полтаве, как только они появятся на OLX.\n\n/stop — отписаться"
    )


@dp.message(Command("stop"))
async def cmd_stop(m: Message):
    state.subscribers.discard(m.chat.id)
    state.save()
    await m.answer("🔕 Подписка отключена. /start — включить снова.")


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    poller = asyncio.create_task(poll_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        poller.cancel()


if __name__ == "__main__":
    asyncio.run(main())
