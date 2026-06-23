"""
Telegram MTProto через Telethon.
Чтение истории, отправка, непрочитанные.
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


def is_configured() -> bool:
    return bool(config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH)


def _loop_get() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def _run(coro):
    return _loop_get().run_until_complete(coro)


def reset_client() -> None:
    global _client
    if _client and _client.is_connected():
        try:
            _run(_client.disconnect())
        except Exception:
            pass
    _client = None


async def _client_get():
    global _client
    if not is_configured():
        raise RuntimeError("telegram_api_id / telegram_api_hash не заданы")
    if _client is None:
        from telethon import TelegramClient
        _client = TelegramClient(str(config.TELEGRAM_SESSION_PATH), config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    if not _client.is_connected():
        await _client.connect()
    if not await _client.is_user_authorized():
        raise RuntimeError("Запусти: python telegram_client.py")
    return _client


async def _entity(client, contact: str):
    contact = (contact or "").strip()
    if not contact or contact.lower() == "default":
        if config.TELEGRAM_DEFAULT_CHAT_ID:
            return int(config.TELEGRAM_DEFAULT_CHAT_ID)
        dialogs = await client.get_dialogs(limit=1)
        if dialogs:
            return dialogs[0].entity
        raise ValueError("Контакт не указан")
    if contact.lstrip("-").isdigit():
        return int(contact)
    mapped = config.TELEGRAM_CHATS.get(contact.lower())
    if mapped and mapped.lstrip("-").isdigit():
        return int(mapped)
    async for d in client.iter_dialogs(limit=300):
        if contact.lower() in (d.name or "").lower():
            return d.entity
    return await client.get_entity(contact if contact.startswith("@") else contact)


async def send_message_async(contact: str, message: str) -> str:
    c = await _client_get()
    e = await _entity(c, contact)
    await c.send_message(e, message)
    return f"Отправлено: {contact}"


async def read_last_async(contact: str, limit: int = 5) -> list[dict[str, Any]]:
    c = await _client_get()
    e = await _entity(c, contact)
    msgs = await c.get_messages(e, limit=limit)
    out = []
    for m in reversed(msgs):
        if not m.message:
            continue
        sender = getattr(m.sender, "first_name", None) or getattr(m.sender, "username", "?") if m.sender else "?"
        out.append({"sender": str(sender), "text": m.message, "date": str(m.date)})
    return out


async def get_unread_async(limit: int = 10) -> list[dict[str, Any]]:
    """Непрочитанные диалоги (название + кол-во)."""
    c = await _client_get()
    out = []
    async for d in c.iter_dialogs(limit=50):
        if d.unread_count and d.unread_count > 0:
            out.append({"name": d.name or "?", "unread": d.unread_count})
            if len(out) >= limit:
                break
    return out


def send_message(contact: str, message: str) -> str:
    return _run(send_message_async(contact, message))


def read_last(contact: str, limit: int = 5) -> str:
    msgs = _run(read_last_async(contact, limit))
    if not msgs:
        return f"Нет сообщений: {contact}"
    return " | ".join(f"{m['sender']}: {m['text']}" for m in msgs)


def get_unread(limit: int = 10) -> str:
    items = _run(get_unread_async(limit))
    if not items:
        return "Непрочитанных нет"
    return " | ".join(f"{i['name']} ({i['unread']})" for i in items)


def auth_cli() -> None:
    if not is_configured():
        print("Заполни telegram_api_id/hash в settings.json")
        return
    from telethon import TelegramClient
    _run(TelegramClient(str(config.TELEGRAM_SESSION_PATH), config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH).start())
    print("Сессия OK:", config.TELEGRAM_SESSION_PATH)


if __name__ == "__main__":
    auth_cli()