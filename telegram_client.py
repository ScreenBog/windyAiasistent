"""
Telegram MTProto через Telethon.

Функции:
  - send_message / send_voice
  - read_last — последние сообщения контакта
  - get_unread — непрочитанные с превью текста
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

_client = None
_loop: asyncio.AbstractEventLoop | None = None
_CONNECT_TIMEOUT = 25


def is_configured() -> bool:
    return bool(config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH)


def _loop_get() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def _run(coro, timeout: float = _CONNECT_TIMEOUT):
    return _loop_get().run_until_complete(asyncio.wait_for(coro, timeout=timeout))


def reset_client() -> None:
    global _client
    if _client and _client.is_connected():
        try:
            _run(_client.disconnect(), timeout=10)
        except Exception as exc:
            logger.debug("disconnect: %s", exc)
    _client = None


async def _client_get():
    global _client
    if not is_configured():
        raise RuntimeError("Заполни telegram_api_id и telegram_api_hash в settings.json")
    if _client is None:
        from telethon import TelegramClient
        _client = TelegramClient(
            str(config.TELEGRAM_SESSION_PATH),
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
        )
    if not _client.is_connected():
        await _client.connect()
    if not await _client.is_user_authorized():
        raise RuntimeError("Сессия не авторизована. Запусти: python telegram_client.py")
    return _client


def _sender_name(msg) -> str:
    sender = msg.sender
    if sender is None:
        return "?"
    return (
        getattr(sender, "first_name", None)
        or getattr(sender, "username", None)
        or getattr(sender, "title", None)
        or "?"
    )


async def _resolve_entity(client, contact: str):
    """Найти чат/контакт по имени, @username, ID или default."""
    contact = (contact or "").strip()
    if not contact or contact.lower() in ("default", "saved", "избранное"):
        if config.TELEGRAM_DEFAULT_CHAT_ID:
            return int(config.TELEGRAM_DEFAULT_CHAT_ID)
        dialogs = await client.get_dialogs(limit=1)
        if dialogs:
            return dialogs[0].entity
        raise ValueError("Контакт не указан и default чат не найден")

    if contact.lstrip("-").isdigit():
        return int(contact)

    mapped = config.TELEGRAM_CHATS.get(contact.lower())
    if mapped and mapped.lstrip("-").isdigit():
        return int(mapped)

    # Поиск по имени диалога
    needle = contact.lower()
    async for dialog in client.iter_dialogs(limit=400):
        name = (dialog.name or "").lower()
        if needle in name or name in needle:
            return dialog.entity

    # Прямой @username
    handle = contact if contact.startswith("@") else f"@{contact}"
    return await client.get_entity(handle)


def _fmt_msg(m: dict[str, Any]) -> str:
    dt = m.get("date", "")
    if isinstance(dt, datetime):
        dt = dt.strftime("%d.%m %H:%M")
    return f"[{dt}] {m.get('sender', '?')}: {m.get('text', '')}"


# ── Async API ─────────────────────────────────────────────────────────────────

async def send_message_async(contact: str, message: str) -> str:
    client = await _client_get()
    entity = await _resolve_entity(client, contact)
    await client.send_message(entity, message)
    name = getattr(entity, "title", None) or getattr(entity, "first_name", contact)
    return f"Отправлено в «{name}»"


async def send_voice_async(contact: str, text: str, audio_path: Path) -> str:
    """Отправить голосовое сообщение (OGG/MP3 через Telethon)."""
    client = await _client_get()
    entity = await _resolve_entity(client, contact)
    if not audio_path.exists():
        raise FileNotFoundError(f"Аудиофайл не найден: {audio_path}")
    await client.send_file(entity, str(audio_path), voice_note=True)
    name = getattr(entity, "title", None) or getattr(entity, "first_name", contact)
    return f"Голосовое отправлено в «{name}»"


async def read_last_async(contact: str, count: int = 5) -> list[dict[str, Any]]:
    """Последние `count` текстовых сообщений (от старых к новым)."""
    client = await _client_get()
    entity = await _resolve_entity(client, contact)
    count = max(1, min(count, 50))
    messages = await client.get_messages(entity, limit=count)

    out: list[dict[str, Any]] = []
    for msg in reversed(messages):
        if not msg.message:
            continue
        out.append({
            "sender": _sender_name(msg),
            "text": msg.message.strip(),
            "date": msg.date,
            "out": bool(msg.out),
        })
    return out


async def get_unread_async(limit: int = 10, with_preview: bool = True) -> list[dict[str, Any]]:
    """
    Непрочитанные диалоги.
    with_preview=True — подтягивает текст последнего непрочитанного сообщения.
    """
    client = await _client_get()
    limit = max(1, min(limit, 30))
    out: list[dict[str, Any]] = []

    async for dialog in client.iter_dialogs(limit=80):
        unread = dialog.unread_count or 0
        if unread <= 0:
            continue

        item: dict[str, Any] = {
            "name": dialog.name or "?",
            "unread": unread,
            "id": dialog.id,
        }

        if with_preview:
            try:
                msgs = await client.get_messages(dialog.entity, limit=min(unread, 3))
                previews = []
                for m in reversed(msgs):
                    if m.message:
                        previews.append(f"{_sender_name(m)}: {m.message[:120]}")
                item["preview"] = " | ".join(previews) if previews else "(медиа/без текста)"
            except Exception as exc:
                logger.debug("unread preview %s: %s", dialog.name, exc)
                item["preview"] = ""

        out.append(item)
        if len(out) >= limit:
            break

    return out


# ── Sync wrappers ─────────────────────────────────────────────────────────────

def send_message(contact: str, message: str) -> str:
    try:
        return _run(send_message_async(contact, message))
    except Exception as exc:
        logger.error("send_message: %s", exc)
        return f"Ошибка отправки: {exc}"


def send_voice(contact: str, text: str, audio_path: Path) -> str:
    try:
        return _run(send_voice_async(contact, text, audio_path))
    except Exception as exc:
        logger.error("send_voice: %s", exc)
        return f"Ошибка голосового: {exc}"


def read_last(contact: str, count: int = 5) -> str:
    try:
        msgs = _run(read_last_async(contact, count))
        if not msgs:
            return f"Нет текстовых сообщений у «{contact}»"
        return "\n".join(_fmt_msg(m) for m in msgs)
    except Exception as exc:
        logger.error("read_last: %s", exc)
        return f"Ошибка чтения: {exc}"


def get_unread(limit: int = 10) -> str:
    try:
        items = _run(get_unread_async(limit, with_preview=True))
        if not items:
            return "Непрочитанных сообщений нет"
        lines = []
        for i in items:
            line = f"{i['name']} ({i['unread']} новых)"
            if i.get("preview"):
                line += f" — {i['preview']}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_unread: %s", exc)
        return f"Ошибка непрочитанных: {exc}"


async def check_connection_async() -> tuple[bool, str]:
    """Проверка Telethon: настроен, авторизован, подключён."""
    if not is_configured():
        return False, "API ID/Hash не заданы"
    sess_file = Path(str(config.TELEGRAM_SESSION_PATH) + ".session")
    if not sess_file.exists():
        return False, "Нет сессии — python telegram_client.py"
    try:
        client = await _client_get()
        me = await client.get_me()
        name = me.first_name or me.username or "user"
        return True, f"@{me.username}" if me.username else name
    except Exception as exc:
        logger.debug("tg check: %s", exc)
        return False, str(exc)[:60]


def check_connection() -> tuple[bool, str]:
    try:
        return _run(check_connection_async(), timeout=15)
    except Exception as exc:
        return False, str(exc)[:60]


def auth_cli() -> None:
    if not is_configured():
        print("Заполни telegram_api_id / telegram_api_hash в settings.json")
        return
    from telethon import TelegramClient

    async def _auth():
        client = TelegramClient(
            str(config.TELEGRAM_SESSION_PATH),
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
        )
        await client.start()
        me = await client.get_me()
        print(f"Авторизован: {me.first_name} (@{me.username})")
        print(f"Сессия: {config.TELEGRAM_SESSION_PATH}")
        await client.disconnect()

    _run(_auth(), timeout=120)


if __name__ == "__main__":
    auth_cli()