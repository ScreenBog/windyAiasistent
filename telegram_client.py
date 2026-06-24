"""
Telegram MTProto через Telethon.

Потокобезопасно для GUI: все async-операции в одном lock + изолированный event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

# ── Состояние клиента ─────────────────────────────────────────────────────────
_client = None
_client_credentials: tuple[int, str] | None = None
_pending_phone: str = ""
_phone_code_hash: str = ""
_tg_lock = threading.RLock()
_tg_thread: threading.Thread | None = None
_tg_loop: asyncio.AbstractEventLoop | None = None
_tg_loop_ready = threading.Event()
_CONNECT_TIMEOUT = 30
_AUTH_TIMEOUT = 120

AuthState = Literal["not_configured", "ready", "code_sent", "authorized", "error"]


def is_configured() -> bool:
    return bool(config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH)


def _credentials() -> tuple[int, str]:
    api_id = int(config.TELEGRAM_API_ID or 0)
    api_hash = str(config.TELEGRAM_API_HASH or "").strip()
    return api_id, api_hash


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


def _start_tg_loop() -> asyncio.AbstractEventLoop:
    """Фоновый поток с постоянным event loop — Telethon требует один loop на клиент."""
    global _tg_thread, _tg_loop

    if _tg_loop is not None and _tg_thread and _tg_thread.is_alive():
        return _tg_loop

    _tg_loop_ready.clear()

    def _worker() -> None:
        global _tg_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _tg_loop = loop
        _tg_loop_ready.set()
        loop.run_forever()

    _tg_thread = threading.Thread(target=_worker, daemon=True, name="telethon-loop")
    _tg_thread.start()
    if not _tg_loop_ready.wait(timeout=10):
        raise RuntimeError("Не удалось запустить Telethon event loop")
    return _tg_loop


def _run(coro, timeout: float = _CONNECT_TIMEOUT):
    """Выполнить coroutine в выделенном Telethon-потоке (безопасно из GUI)."""
    with _tg_lock:
        loop = _start_tg_loop()
        future = asyncio.run_coroutine_threadsafe(asyncio.wait_for(coro, timeout=timeout), loop)
        return future.result(timeout=timeout + 5)


def _create_client():
    """Синхронное создание TelegramClient с валидацией."""
    if not is_configured():
        raise RuntimeError("Заполни telegram_api_id и telegram_api_hash в настройках")

    api_id, api_hash = _credentials()
    if api_id <= 0:
        raise RuntimeError(f"Некорректный API ID: {api_id}")
    if len(api_hash) < 8:
        raise RuntimeError("API Hash пустой или слишком короткий")

    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise RuntimeError("Пакет telethon не установлен: pip install telethon") from exc

    client = TelegramClient(
        str(config.TELEGRAM_SESSION_PATH),
        api_id,
        api_hash,
    )
    if client is None:
        raise RuntimeError("TelegramClient вернул None — переустанови telethon")
    logger.info("TelegramClient created: api_id=%s session=%s", api_id, config.TELEGRAM_SESSION_PATH)
    return client


async def _disconnect_instance(client) -> None:
    if client is None:
        return
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception as exc:
        logger.debug("disconnect: %s", exc)


def reset_client() -> None:
    """Сброс клиента (после смены API или logout)."""
    global _client, _client_credentials, _pending_phone, _phone_code_hash

    with _tg_lock:
        old = _client
        _client = None
        _client_credentials = None
        _pending_phone = ""
        _phone_code_hash = ""

    if old is not None:
        try:
            _run(_disconnect_instance(old), timeout=10)
        except Exception as exc:
            logger.debug("reset_client: %s", exc)


async def _ensure_client(*, require_auth: bool = True):
    """
    Получить подключённый клиент.
    require_auth=False — для send_code / sign_in до авторизации.
    """
    global _client, _client_credentials

    creds = _credentials()
    if creds[0] <= 0 or not creds[1]:
        raise RuntimeError("API ID/Hash не настроены — сохрани в GUI → Telegram")

    if _client is not None and _client_credentials != creds:
        logger.info("Telegram credentials changed — reconnecting")
        old = _client
        _client = None
        _client_credentials = None
        await _disconnect_instance(old)

    if _client is None:
        _client = _create_client()
        _client_credentials = creds

    if _client is None:
        raise RuntimeError("Не удалось создать TelegramClient")

    if not _client.is_connected():
        await _client.connect()

    if require_auth and not await _client.is_user_authorized():
        raise RuntimeError(
            "Сессия не авторизована. Нажми «Отправить код» → «Войти» в GUI → Telegram"
        )
    return _client


# ── Форматирование ────────────────────────────────────────────────────────────

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
    name_l, needle_l = name.lower(), needle.lower()
    if name_l == needle_l:
        return 100
    if needle_l in name_l:
        return 80 + min(15, len(needle_l))
    if name_l in needle_l:
        return 70
    words = [w for w in re.split(r"\s+", needle_l) if len(w) > 2]
    return sum(1 for w in words if w in name_l) * 20


async def _resolve_entity(client, contact: str):
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
    best_score, best_entity, best_name = 0, None, ""

    async for dialog in client.iter_dialogs(limit=config.TELEGRAM_DIALOGS_LIMIT):
        name = dialog.name or ""
        score = _score_match(name, needle)
        ent = dialog.entity
        if hasattr(ent, "username") and ent.username:
            uname = ent.username.lower()
            if needle == uname or needle in uname:
                score = max(score, 90)
        if score > best_score:
            best_score, best_entity, best_name = score, ent, name

    if best_entity and best_score >= 40:
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
    who = "Вы" if m.get("out") else m.get("sender", "?")
    prefix = f"{index}. " if index is not None else ""
    return f"{prefix}[{dt}] {who}: {m.get('text', '')}"


def format_messages(msgs: list[dict[str, Any]], contact: str = "") -> str:
    if not msgs:
        return f"Нет сообщений у «{contact or 'контакта'}»"
    title = f"«{contact}» — последние {len(msgs)}:\n" if contact else ""
    return title + "\n".join(_fmt_msg(m, index=i + 1) for i, m in enumerate(msgs))


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


# ── Auth ──────────────────────────────────────────────────────────────────────

async def send_code_async(phone: str) -> str:
    """Отправить код на телефон. Создаёт клиент при необходимости."""
    global _pending_phone, _phone_code_hash

    phone = _normalize_phone(phone)
    if not phone:
        raise ValueError("Укажи номер телефона (+7999…)")

    client = await _ensure_client(require_auth=False)
    if client is None:
        raise RuntimeError("Клиент не создан")

    logger.info("send_code_request → %s", phone)
    result = await client.send_code_request(phone)
    if result is None or not getattr(result, "phone_code_hash", None):
        raise RuntimeError("Telegram не вернул phone_code_hash — попробуй ещё раз")

    _pending_phone = phone
    _phone_code_hash = result.phone_code_hash
    return f"Код отправлен на {phone}"


async def sign_in_async(phone: str, code: str, password: str = "") -> str:
    """Войти по коду (+ 2FA при необходимости)."""
    global _pending_phone, _phone_code_hash

    from telethon.errors import (
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        SessionPasswordNeededError,
    )

    phone = _normalize_phone(phone)
    code = (code or "").strip().replace(" ", "")
    if not phone:
        raise ValueError("Укажи телефон")
    if not code:
        raise ValueError("Введи код из Telegram")

    client = await _ensure_client(require_auth=False)
    if client is None:
        raise RuntimeError("Клиент не создан")

    # Используем hash от последнего send_code; иначе Telethon может не найти сессию
    hash_val = _phone_code_hash
    if not hash_val:
        logger.warning("phone_code_hash пуст — sign_in без hash (может не сработать)")

    try:
        if hash_val and phone == _pending_phone:
            await client.sign_in(phone=phone, code=code, phone_code_hash=hash_val)
        else:
            await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        pwd = (password or "").strip()
        if not pwd:
            raise RuntimeError("Нужен пароль 2FA — введи в поле «Пароль 2FA»")
        await client.sign_in(password=pwd)
    except PhoneCodeInvalidError:
        raise RuntimeError("Неверный код — запроси новый через «Отправить код»")
    except PhoneCodeExpiredError:
        raise RuntimeError("Код истёк — нажми «Отправить код» снова")

    if not await client.is_user_authorized():
        raise RuntimeError("Авторизация не завершена")

    me = await client.get_me()
    _pending_phone = ""
    _phone_code_hash = ""
    uname = f"@{me.username}" if me.username else (me.first_name or "user")
    logger.info("Telegram authorized: %s", uname)
    return f"Авторизован: {uname}"


async def get_auth_status_async() -> dict[str, Any]:
    """Статус для GUI: not_configured | ready | code_sent | authorized | error."""
    if not is_configured():
        return {"state": "not_configured", "message": "API ID/Hash не заданы", "ok": False}

    sess = Path(str(config.TELEGRAM_SESSION_PATH) + ".session")
    try:
        client = await _ensure_client(require_auth=False)
        if await client.is_user_authorized():
            me = await client.get_me()
            name = f"@{me.username}" if me.username else (me.first_name or "OK")
            return {"state": "authorized", "message": name, "ok": True}
        if _phone_code_hash:
            return {
                "state": "code_sent",
                "message": f"Код отправлен на {_pending_phone} — введи и нажми «Войти»",
                "ok": False,
            }
        if sess.exists():
            return {"state": "ready", "message": "Сессия есть, но не авторизован — отправь код", "ok": False}
        return {"state": "ready", "message": "Готов к авторизации — отправь код", "ok": False}
    except Exception as exc:
        return {"state": "error", "message": str(exc)[:100], "ok": False}


async def check_connection_async() -> tuple[bool, str]:
    st = await get_auth_status_async()
    return st["ok"], st["message"]


# ── Messaging API ─────────────────────────────────────────────────────────────

async def send_message_async(contact: str, message: str) -> str:
    client = await _ensure_client(require_auth=True)
    entity = await _resolve_entity(client, contact)
    await client.send_message(entity, message)
    return f"Отправлено в «{_entity_title(entity, contact)}»"


async def send_voice_async(contact: str, text: str, audio_path: Path) -> str:
    client = await _ensure_client(require_auth=True)
    entity = await _resolve_entity(client, contact)
    if not audio_path.exists():
        raise FileNotFoundError(f"Аудиофайл не найден: {audio_path}")
    await client.send_file(entity, str(audio_path), voice_note=True)
    return f"Голосовое отправлено в «{_entity_title(entity, contact)}»"


async def read_last_async(contact: str, count: int = 5) -> list[dict[str, Any]]:
    client = await _ensure_client(require_auth=True)
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
    client = await _ensure_client(require_auth=True)
    limit = max(1, min(int(limit), 30))
    out: list[dict[str, Any]] = []

    async for dialog in client.iter_dialogs(limit=100):
        unread = dialog.unread_count or 0
        if unread <= 0:
            continue
        item: dict[str, Any] = {"name": dialog.name or "?", "unread": unread, "id": dialog.id}
        if with_preview:
            try:
                msgs = await client.get_messages(dialog.entity, limit=min(unread, 5))
                previews = []
                for m in reversed(msgs):
                    who = "Вы" if m.out else _sender_name(m)
                    previews.append(f"{who}: {_msg_body(m)[:100]}")
                item["preview"] = " | ".join(previews)
            except Exception as exc:
                logger.debug("preview %s: %s", dialog.name, exc)
                item["preview"] = ""
        out.append(item)
        if len(out) >= limit:
            break
    return out


async def list_dialogs_async(limit: int | None = None) -> list[dict[str, Any]]:
    client = await _ensure_client(require_auth=True)
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


# ── Sync wrappers ─────────────────────────────────────────────────────────────

def get_auth_status() -> dict[str, Any]:
    try:
        return _run(get_auth_status_async(), timeout=15)
    except Exception as exc:
        return {"state": "error", "message": str(exc)[:100], "ok": False}


def send_code(phone: str) -> tuple[bool, str]:
    try:
        msg = _run(send_code_async(phone), timeout=_AUTH_TIMEOUT)
        return True, msg
    except Exception as exc:
        logger.error("send_code: %s", exc, exc_info=True)
        return False, str(exc)


def sign_in(phone: str, code: str, password: str = "") -> tuple[bool, str]:
    try:
        msg = _run(sign_in_async(phone, code, password), timeout=_AUTH_TIMEOUT)
        return True, msg
    except Exception as exc:
        logger.error("sign_in: %s", exc, exc_info=True)
        return False, str(exc)


def check_connection() -> tuple[bool, str]:
    try:
        return _run(check_connection_async(), timeout=15)
    except Exception as exc:
        return False, str(exc)[:80]


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
    return _run(read_last_async(contact, count))


def get_unread(limit: int = 10) -> str:
    try:
        items = _run(get_unread_async(limit, with_preview=True))
        return format_unread(items)
    except Exception as exc:
        logger.error("get_unread: %s", exc)
        return f"Ошибка непрочитанных: {exc}"


def list_dialogs(limit: int | None = None) -> list[dict[str, Any]]:
    try:
        return _run(list_dialogs_async(limit), timeout=20)
    except Exception as exc:
        logger.error("list_dialogs: %s", exc)
        return []


def auth_cli() -> None:
    if not is_configured():
        print("Заполни telegram_api_id / telegram_api_hash в settings.json")
        return

    async def _auth():
        phone = _normalize_phone(config.TELEGRAM_PHONE or input("Телефон (+7...): "))
        client = _create_client()
        await client.connect()
        await client.start(phone=phone)
        me = await client.get_me()
        print(f"Авторизован: {me.first_name} (@{me.username})")
        await client.disconnect()

    reset_client()
    _run(_auth(), timeout=_AUTH_TIMEOUT)


if __name__ == "__main__":
    auth_cli()