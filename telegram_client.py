"""
Telegram через Telethon (MTProto).

Чтение истории чатов offline — то, что Bot API не умеет.
Получи api_id / api_hash на https://my.telegram.org
Первый вход: python -c "from telegram_client import auth_cli; auth_cli()"
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

_client = None
_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def _run(coro):
    return _get_loop().run_until_complete(coro)


def is_configured() -> bool:
    return bool(config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH)


async def _get_client():
    global _client
    if not is_configured():
        raise RuntimeError("Укажи telegram_api_id и telegram_api_hash в settings.json")
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
        raise RuntimeError(
            "Telegram не авторизован. Запусти: python telegram_client.py"
        )
    return _client


def reset_client() -> None:
    global _client
    if _client and _client.is_connected():
        try:
            _run(_client.disconnect())
        except Exception:
            pass
    _client = None


async def _resolve_entity(client, contact: str):
    """Контакт по имени, username или chat_id."""
    contact = (contact or "").strip()
    if not contact or contact.lower() == "default":
        if config.TELEGRAM_DEFAULT_CHAT_ID:
            return int(config.TELEGRAM_DEFAULT_CHAT_ID)
        dialogs = await client.get_dialogs(limit=1)
        if dialogs:
            return dialogs[0].entity
        raise ValueError("Контакт не указан и нет диалогов")

    if contact.lstrip("-").isdigit():
        return int(contact)

    mapped = config.TELEGRAM_CHATS.get(contact.lower())
    if mapped and mapped.lstrip("-").isdigit():
        return int(mapped)

    # Поиск по имени / username
    async for dialog in client.iter_dialogs(limit=200):
        title = (dialog.name or "").lower()
        if contact.lower() in title:
            return dialog.entity
    entity = await client.get_entity(contact if contact.startswith("@") else contact)
    return entity


async def send_message_async(contact: str, message: str) -> str:
    client = await _get_client()
    entity = await _resolve_entity(client, contact)
    await client.send_message(entity, message)
    return f"Отправлено: {contact}"


async def read_last_async(contact: str, limit: int = 5) -> list[dict[str, Any]]:
    client = await _get_client()
    entity = await _resolve_entity(client, contact)
    msgs = await client.get_messages(entity, limit=limit)
    out: list[dict[str, Any]] = []
    for m in reversed(msgs):
        if not m.message:
            continue
        sender = "?"
        if m.sender:
            sender = getattr(m.sender, "first_name", None) or getattr(m.sender, "username", "?")
        out.append({"sender": str(sender), "text": m.message, "date": str(m.date)})
    return out


def send_message(contact: str, message: str) -> str:
    try:
        return _run(send_message_async(contact, message))
    except Exception as exc:
        logger.error("TG send: %s", exc)
        raise


def read_last(contact: str, limit: int = 5) -> str:
    try:
        msgs = _run(read_last_async(contact, limit))
        if not msgs:
            return f"Нет сообщений от {contact or 'чата'}"
        return " | ".join(f"{m['sender']}: {m['text']}" for m in msgs)
    except Exception as exc:
        logger.error("TG read: %s", exc)
        raise


def auth_cli() -> None:
    """Интерактивная авторизация Telethon."""
    if not is_configured():
        print("Заполни telegram_api_id и telegram_api_hash в settings.json")
        return
    from telethon import TelegramClient
    _run(TelegramClient(str(config.TELEGRAM_SESSION_PATH), config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH).start())
    print("Сессия сохранена:", config.TELEGRAM_SESSION_PATH)


if __name__ == "__main__":
    auth_cli()