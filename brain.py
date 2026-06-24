"""
Мозг Windy: Ollama + JSON-макросы + гибрид моделей.

Формат ответа LLM:
  {"speech": "...", "macros": [{"type": "OPEN_BROWSER", "query": "грок"}, ...]}

Поддерживается legacy: {"speech": "...", "actions": [{"tool": "...", "params": {}}]}
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT = re.compile(r"(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})", re.DOTALL)

_COMPLEX_MARKERS = (
    " и ", " потом ", " затем ", " после ", " сначала ",
    "прочитай", "напиши", "отправь", "найди", "поищи",
    "несколько", "пошаг", "макрос", "скрипт",
)


@dataclass
class Macro:
    type: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Action:
    tool: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class BrainResponse:
    speech: str = ""
    macros: list[Macro] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    raw: str = ""
    model_used: str = ""

    @property
    def has_macros(self) -> bool:
        return bool(self.macros)

    @property
    def has_actions(self) -> bool:
        return bool(self.macros or self.actions)

    def macros_as_dicts(self) -> list[dict[str, Any]]:
        return [{"type": m.type, **m.params} for m in self.macros]


def _load_prompt() -> str:
    try:
        text = config.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        hints = ""
        if config.LEARNING_ENABLED:
            try:
                import learning
                hints = learning.get_prompt_hints()
            except Exception:
                pass
        return (
            text.replace("{{APPS}}", ", ".join(sorted(config.APP_PATHS)) or "chrome, telegram")
            .replace("{{GAMES}}", ", ".join(f"{k}:{v}" for k, v in config.STEAM_GAMES.items()) or "-")
            .replace("{{MACROS}}", ", ".join(config.MACRO_TYPES))
            .replace("{{LEARNING}}", hints or "нет исправлений")
        )
    except FileNotFoundError:
        logger.warning("system prompt not found")
        return '{"speech":"...","macros":[]}'


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pattern in (_JSON_BLOCK, _JSON_OBJECT):
        for match in pattern.finditer(text):
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    return None


def is_complex_command(text: str) -> bool:
    """Эвристика: сложная команда → медленная модель."""
    t = (text or "").strip().lower()
    if not t:
        return False
    words = t.split()
    if len(words) > config.SIMPLE_COMMAND_MAX_WORDS:
        return True
    if any(m in t for m in _COMPLEX_MARKERS):
        return True
    if t.count(",") >= 2:
        return True
    return False


def _validate_response(data: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "root not object"

    speech = data.get("speech") or data.get("response")
    if speech is not None and not isinstance(speech, str):
        return False, "speech must be string"

    macros = data.get("macros")
    actions = data.get("actions")

    if macros is None and actions is None:
        return True, ""

    if macros is not None:
        if not isinstance(macros, list):
            return False, "macros must be array"
        for i, item in enumerate(macros):
            if not isinstance(item, dict):
                return False, f"macro[{i}] not object"
            mtype = item.get("type") or item.get("action") or item.get("macro")
            if not mtype and len(item) != 1:
                return False, f"macro[{i}] missing type"

    if actions is not None:
        if not isinstance(actions, list):
            return False, "actions must be array"
        for i, item in enumerate(actions):
            if not isinstance(item, dict):
                return False, f"action[{i}] not object"
            tool = item.get("tool") or item.get("name")
            if not tool:
                return False, f"action[{i}] missing tool"

    return True, ""


def _parse_macros(data: dict[str, Any]) -> list[Macro]:
    from tools import _parse_macro

    raw = data.get("macros") or []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[Macro] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mtype, params = _parse_macro(item)
        if mtype:
            out.append(Macro(type=mtype, params=params))
    return out


def _parse_actions(data: dict[str, Any]) -> list[Action]:
    raw = data.get("actions") or []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[Action] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or item.get("name") or "").strip().lower()
        if not tool:
            continue
        params = item.get("params") or item.get("arguments") or {}
        if not isinstance(params, dict):
            params = {"value": params}
        out.append(Action(tool=tool, params=params))
    return out


def _ollama_options() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "temperature": config.OLLAMA_TEMPERATURE,
        "num_ctx": config.OLLAMA_NUM_CTX,
        "top_p": config.OLLAMA_TOP_P,
        "repeat_penalty": config.OLLAMA_REPEAT_PENALTY,
    }
    if config.OLLAMA_NUM_GPU >= 0:
        opts["num_gpu"] = config.OLLAMA_NUM_GPU
    return opts


def _fast_path_macros(text: str) -> BrainResponse | None:
    """Мгновенный ответ без LLM для частых шаблонов."""
    import config as cfg

    t = cfg.normalize_browser_query(text)
    if not t:
        return None

    if t in ("вк", "вконтакте", "vk"):
        return BrainResponse(
            speech="Открываю ВКонтакте.",
            macros=[Macro("OPEN_VK", {})],
            model_used="fast-path",
        )
    if t in cfg.BROWSER_SITES and cfg.should_prefer_browser(t):
        return BrainResponse(
            speech=f"Открываю {t}.",
            macros=[Macro("OPEN_BROWSER", {"query": t})],
            model_used="fast-path",
        )
    for prefix, _engine in cfg._SEARCH_PREFIXES:
        if t.startswith(prefix + " "):
            term = t[len(prefix) + 1 :].strip()
            if term:
                return BrainResponse(
                    speech="Ищу в интернете.",
                    macros=[Macro("OPEN_BROWSER", {"query": text.strip()})],
                    model_used="fast-path",
                )
    return None


class Brain:
    def __init__(self) -> None:
        self.model = config.OLLAMA_MODEL
        self.host = config.OLLAMA_HOST
        self.system_prompt = _load_prompt()
        self._history: list[dict[str, str]] = []
        self._client = None
        self._last_command = ""
        self._last_response: BrainResponse | None = None

    def _client_get(self):
        if self._client is None:
            import ollama
            self._client = ollama.Client(host=self.host, timeout=config.OLLAMA_TIMEOUT)
        return self._client

    def reload_prompt(self) -> None:
        config.reload_settings()
        self.system_prompt = _load_prompt()
        self.model = config.OLLAMA_MODEL
        if self.host != config.OLLAMA_HOST:
            self.host = config.OLLAMA_HOST
            self._client = None

    @property
    def last_response(self) -> BrainResponse | None:
        return self._last_response

    def _chat_once(self, user_text: str, *, model: str, strict_hint: bool = False) -> str:
        hint = ""
        if strict_hint:
            hint = (
                '\n\nВАЖНО: ответь ТОЛЬКО валидным JSON '
                '{"speech":"...","macros":[{"type":"OPEN_BROWSER","query":"..."}]} без markdown.'
            )
        messages = [
            {"role": "system", "content": self.system_prompt},
            *self._history[-16:],
            {"role": "user", "content": user_text + hint},
        ]
        resp = self._client_get().chat(
            model=model,
            messages=messages,
            options=_ollama_options(),
            format="json",
        )
        return resp.get("message", {}).get("content", "") or ""

    def think(self, text: str) -> BrainResponse:
        text = (text or "").strip()
        if not text:
            return BrainResponse(speech=config.NO_SPEECH)

        self._last_command = text

        # Fast-path для простых команд (без Ollama)
        if config.HYBRID_MODELS_ENABLED:
            fp = _fast_path_macros(text)
            if fp and not is_complex_command(text):
                self._last_response = fp
                return fp

        complex_task = is_complex_command(text)
        model = config.resolve_ollama_model(complex_task=complex_task)
        logger.info("brain model=%s complex=%s", model, complex_task)

        last_raw = ""
        last_error = ""

        for attempt in range(config.OLLAMA_JSON_RETRIES + 1):
            try:
                raw = self._chat_once(text, model=model, strict_hint=(attempt > 0))
                last_raw = raw
                data = _extract_json(raw)
                if not data:
                    last_error = "json parse failed"
                    logger.warning("brain attempt %d: no json in %r", attempt, raw[:200])
                    continue

                ok, err = _validate_response(data)
                if not ok:
                    last_error = err
                    logger.warning("brain attempt %d: invalid schema: %s", attempt, err)
                    continue

                speech = str(data.get("speech") or data.get("response") or "").strip()
                macros = _parse_macros(data)
                actions = _parse_actions(data)

                self._history += [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": raw},
                ]
                self._history = self._history[-20:]

                resp = BrainResponse(
                    speech=speech,
                    macros=macros,
                    actions=actions,
                    raw=raw,
                    model_used=model,
                )
                self._last_response = resp
                return resp

            except Exception as exc:
                last_error = str(exc)
                logger.error("Ollama attempt %d: %s", attempt, exc)
                if attempt >= config.OLLAMA_JSON_RETRIES:
                    return BrainResponse(
                        speech="Ollama недоступна. Запусти ollama serve.",
                        raw=last_error,
                    )

        logger.error("brain failed after retries: %s | raw=%r", last_error, last_raw[:300])
        return BrainResponse(speech="Не понял. Повтори, пожалуйста.", raw=last_raw)

    def check_connection(self) -> bool:
        try:
            models = self._client_get().list().get("models", [])
            names = {m.get("model", m.get("name", "")) for m in models}
            for candidate in (config.OLLAMA_MODEL, config.OLLAMA_MODEL_FAST, config.OLLAMA_MODEL_SLOW):
                if not candidate:
                    continue
                target = candidate.split(":")[0]
                if any(target in n for n in names):
                    return True
            return False
        except Exception as exc:
            logger.error("Ollama check: %s", exc)
            return False