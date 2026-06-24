"""
Telegram MTProto через Telethon.

Функции:
  - send_message / send_voice
  - read_last — последние сообщения контакта (текст + медиа-метки)
  - get_unread — непрочитанные с превью
  - list_dialogs — список чатов для GUI
  - send_code / sign_in — авторизация по телефону из GUI
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

_client = None
_loop: asyncio.AbstractEventLoop | None = None
_CONNECT_TIMEOUT = 30
_AUTH_TIMEOUT = 120


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


def _normalize_phone(phone: str) -> str:
    phone = re.sub(r"[^\d+]", "", (phone or "").strip())
    if phone and not phone.startswith("+"):
        if phone.startswith("8") and len(phone) == 11:
            phone = "+7" + phone[1:]
        elif len(phone) == 10:
            phone = "+7" + phone
        else:
            phone = "+" + phone
    return phone


async def _new_client():
    from telethon import TelegramClient
    return TelegramClient(
        str(config.TELEGRAM_SESSION_PATH),
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
    )


async def _client_get():
    global _client
    if not is_configured():
        raise RuntimeError("Заполни telegram_api_id и telegram_api_hash в настройках")
    if _client is None:
        _client = await _new_client()
    if not _client.is_connected():
        await _client.connect()
    if not await _client.is_user_authorized():
        raise RuntimeError(
            "Сессия не авторизована. В GUI → Telegram нажми «Отправить код» или: python telegram_client.py"
        )
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


def _msg_body(msg) -> str:
    """Текст сообщения или метка медиа."""
    if msg.message and str(msg.message).strip():
        return str(msg.message).strip()
    if msg.photo:
        return "[фото]"
    if msg.voice:
        return "[голосовое]"
    if msg.video:
        return "[видео]"
    if msg.document:
        return "[файл]"
    if msg.sticker:
        return "[стикер]"
    if msg.poll:
        return "[опрос]"
    return "[медиа]"


def _score_match(name: str, needle: str) -> int:
    """Чем выше — тем лучше совпадение имени диалога."""
    name_l, needle_l = name.lower(), needle.lower()
    if name_l == needle_l:
        return 100
    if needle_l in name_l:
        return 80 + min(15, len(needle_l))
    if name_l in needle_l:
        return 70
    # По словам
    words = [w for w in re.split(r"\s+", needle_l) if len(w) > 2]
    hits = sum(1 for w in words if w in name_l)
    return hits * 20


async def _resolve_entity(client, contact: str):
    """Найти чат/контакт по имени, @username, ID, alias или default."""
    contact = (contact or "").strip()
    if not contact:
        contact = config.TELEGRAM_DEFAULT_CONTACT.strip()

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

    needle = contact.lower().lstrip("@")
    best_score = 0
    best_entity = None
    best_name = ""

    async for dialog in client.iter_dialogs(limit=config.TELEGRAM_DIALOGS_LIMIT):
        name = dialog.name or ""
        score = _score_match(name, needle)
        uname = ""
        ent = dialog.entity
        if hasattr(ent, "username") and ent.username:
            uname = ent.username.lower()
            if needle == uname or needle in uname:
                score = max(score, 90)
        if score > best_score:
            best_score = score
            best_entity = ent
            best_name = name

    if best_entity and best_score >= 40:
        logger.debug("resolved %r → %s (score=%d)", contact, best_name, best_score)
        return best_entity

    handle = contact if contact.startswith("@") else f"@{contact}"
    try:
        return await client.get_entity(handle)
    except Exception as exc:
        raise ValueError(f"Контакт «{contact}» не найден") from exc


def _entity_title(entity, fallback: str = "") -> str:
    return (
        getattr(entity, "title", None)
        or getattr(entity, "first_name", None)
        or getattr(entity, "username", None)
        or fallback
    )


def _fmt_msg(m: dict[str, Any], *, index: int | None = None) -> str:
    dt = m.get("date", "")
    if isinstance(dt, datetime):
        dt = dt.strftime("%d.%m %H:%M")
    who = m.get("sender", "?")
    if m.get("out"):
        who = "Вы"
    text = m.get("text", "")
    prefix = f"{index}. " if index is not None else ""
    return f"{prefix}[{dt}] {who}: {text}"


def format_messages(msgs: list[dict[str, Any]], contact: str = "") -> str:
    """Форматирование для GUI и голосового ответа."""
    if not msgs:
        return f"Нет сообщений у «{contact or 'контакта'}»"
    title = f"«{contact}» — последние {len(msgs)}:\n" if contact else ""
    lines = [_fmt_msg(m, index=i + 1) for i, m in enumerate(msgs)]
    return title + "\n".join(lines)


def format_unread(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Непрочитанных сообщений нет"
    lines = []
    for i in items:
        line = f"• {i['name']} — {i['unread']} новых"
        if i.get("preview"):
            line += f"\n  {i['preview']}"
        lines.append(line)
    return "\n".join(lines)


# ── Async API ─────────────────────────────────────────────────────────────────

async def send_message_async(contact: str, message: str) -> str:
    client = await _client_get()
    entity = await _resolve_entity(client, contact)
    await client.send_message(entity, message)
    name = _entity_title(entity, contact)
    return f"Отправлено в «{name}»"


async def send_voice_async(contact: str, text: str, audio_path: Path) -> str:
    client = await _client_get()
    entity = await _resolve_entity(client, contact)
    if not audio_path.exists():
        raise FileNotFoundError(f"Аудиофайл не найден: {audio_path}")
    await client.send_file(entity, str(audio_path), voice_note=True)
    name = _entity_title(entity, contact)
    return f"Голосовое отправлено в «{name}»"


async def read_last_async(contact: str, count: int = 5) -> list[dict[str, Any]]:
    """Последние `count` сообщений (от старых к новым)."""
    client = await _client_get()
    entity = await _resolve_entity(client, contact)
    count = max(1, min(int(count), 50))
    messages = await client.get_messages(entity, limit=count)

    out: list[dict[str, Any]] = []
    for msg in reversed(messages):
        out.append({
            "sender": _sender_name(msg),
            "text": _msg_body(msg),
            "date": msg.date,
            "out": bool(msg.out),
            "id": msg.id,
        })
    return out


async def get_unread_async(limit: int = 10, with_preview: bool = True) -> list[dict[str, Any]]:
    client = await _client_get()
    limit = max(1, min(int(limit), 30))
    out: list[dict[str, Any]] = []

    async for dialog in client.iter_dialogs(limit=100):
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
                msgs = await client.get_messages(dialog.entity, limit=min(unread, 5))
                previews = []
                for m in reversed(msgs):
                    body = _msg_body(m)
                    who = "Вы" if m.out else _sender_name(m)
                    previews.append(f"{who}: {body[:100]}")
                item["preview"] = " | ".join(previews) if previews else ""
            except Exception as exc:
                logger.debug("unread preview %s: %s", dialog.name, exc)
                item["preview"] = ""

        out.append(item)
        if len(out) >= limit:
            break

    return out


async def list_dialogs_async(limit: int | None = None) -> list[dict[str, Any]]:
    """Список диалогов для выбора контакта в GUI."""
    client = await _client_get()
    lim = limit or config.TELEGRAM_DIALOGS_LIMIT
    out: list[dict[str, Any]] = []
    async for dialog in client.iter_dialogs(limit=lim):
        ent = dialog.entity
        username = getattr(ent, "username", None) or ""
        out.append({
            "id": dialog.id,
            "name": dialog.name or "?",
            "unread": dialog.unread_count or 0,
            "username": username,
            "label": f"{dialog.name}" + (f" (@{username})" if username else ""),
        })
    return out


async def send_code_async(phone: str) -> str:
    """Отправить SMS/Telegram-код на телефон."""
    global _client
    phone = _normalize_phone(phone)
    if not phone:
        raise ValueError("Укажи номер телефона (+7...)")
    if _client is None:
        _client = await _new_client()
    if not _client.is_connected():
        await _client.connect()
    await _client.send_code_request(phone)
    return f"Код отправлен на {phone}"


async def sign_in_async(phone: str, code: str, password: str = "") -> str:
    """Подтвердить код; password — если включена 2FA."""
    global _client
    from telethon.errors import SessionPasswordNeededError

    phone = _normalize_phone(phone)
    code = (code or "").strip()
    if not code:
        raise ValueError("Введи код из Telegram")

    if _client is None:
        _client = await _new_client()
    if not _client.is_connected():
        await _client.connect()

    try:
        await _client.sign_in(phone, code)
    except SessionPasswordNeededError:
        if not password:
            raise RuntimeError("Нужен пароль 2FA — введи в поле «Пароль 2FA»")
        await _client.sign_in(password=password)

    me = await _client.get_me()
    name = me.first_name or me.username or "user"
    uname = f"@{me.username}" if me.username else name
    return f"Авторизован: {uname}"


async def check_connection_async() -> tuple[bool, str]:
    if not is_configured():
        return False, "API ID/Hash не заданы"
    sess_file = Path(str(config.TELEGRAM_SESSION_PATH) + ".session")
    if not sess_file.exists():
        return False, "Нет сессии — авторизуйся в Telegram"
    try:
        client = await _client_get()
        me = await client.get_me()
        name = me.first_name or "user"
        return True, f"@{me.username}" if me.username else name
    except Exception as exc:
        logger.debug("tg check: %s", exc)
        return False, str(exc)[:80]


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


def read_last(contact: str, count: int | None = None) -> str:
    try:
        n = count if count is not None else config.TELEGRAM_READ_DEFAULT_COUNT
        msgs = _run(read_last_async(contact, n))
        return format_messages(msgs, contact)
    except Exception as exc:
        logger.error("read_last: %s", exc)
        return f"Ошибка чтения: {exc}"


def read_last_raw(contact: str, count: int = 5) -> list[dict[str, Any]]:
    """Структурированные сообщения для GUI."""
    return _run(read_last_async(contact, count))


def get_unread(limit: int = 10) -> str:
    try:
        items = _run(get_unread_async(limit, with_preview=True))
        return format_unread(items)
    except Exception as exc:
        logger.error("get_unread: %s", exc)
        return f"Ошибка непрочитанных: {exc}"


def get_unread_raw(limit: int = 10) -> list[dict[str, Any]]:
    return _run(get_unread_async(limit, with_preview=True))


def list_dialogs(limit: int | None = None) -> list[dict[str, Any]]:
    try:
        return _run(list_dialogs_async(limit), timeout=20)
    except Exception as exc:
        logger.error("list_dialogs: %s", exc)
        return []


def send_code(phone: str) -> tuple[bool, str]:
    try:
        msg = _run(send_code_async(phone), timeout=_AUTH_TIMEOUT)
        return True, msg
    except Exception as exc:
        logger.error("send_code: %s", exc)
        return False, str(exc)


def sign_in(phone: str, code: str, password: str = "") -> tuple[bool, str]:
    try:
        msg = _run(sign_in_async(phone, code, password), timeout=_AUTH_TIMEOUT)
        return True, msg
    except Exception as exc:
        logger.error("sign_in: %s", exc)
        return False, str(exc)


def check_connection() -> tuple[bool, str]:
    try:
        return _run(check_connection_async(), timeout=15)
    except Exception as exc:
        return False, str(exc)[:80]


def auth_cli() -> None:
    if not is_configured():
        print("Заполни telegram_api_id / telegram_api_hash в settings.json")
        return

    async def _auth():
        phone = _normalize_phone(config.TELEGRAM_PHONE or input("Телефон (+7...): "))
        client = await _new_client()
        await client.start(phone=phone)
        me = await client.get_me()
        print(f"Авторизован: {me.first_name} (@{me.username})")
        print(f"Сессия: {config.TELEGRAM_SESSION_PATH}")
        await client.disconnect()

    global _client
    _client = None
    _run(_auth(), timeout=_AUTH_TIMEOUT)


if __name__ == "__main__":
    auth_cli()