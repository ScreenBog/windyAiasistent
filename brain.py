"""
Мозг Windy: Ollama + JSON-макросы + гибридный ModelRouter.

Маршрутизация:
  1. fast-path (без LLM) — шаблоны «открой вк», «открой грок»
  2. windy-fast — простые команды, 1–2 макроса
  3. qwen2.5:3b-windy (slow) — цепочки, Telegram, сложный JSON

Если fast ломает JSON, галлюцинирует или не хватает макросов → escalate на slow.
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

_SLOW_ONLY_MARKERS = (
    "телеграм", "telegram", "телега", "напиши", "отправь", "прочитай",
    "сообщен", "диалог", "непрочитан",
)

_ACTION_VERBS = (
    "открой", "запусти", "включи", "найди", "поищи", "загугли",
    "напиши", "отправь", "прочитай", "откройте",
)

# Макросы, которые fast-модель не должна обрабатывать одна
_SLOW_MACRO_TYPES = frozenset({
    "TELEGRAM_SEND", "TELEGRAM_READ", "SHELL_CMD", "FOCUS",
})

_VALID_MACRO_TYPES = frozenset(config.MACRO_TYPES) | frozenset({
    "TOOL", "LAUNCH_APP", "OPEN_BROWSER", "OPEN_VK", "TYPE", "KEY", "SLEEP",
})


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
    route_tier: str = ""      # fast-path | fast | slow | single
    route_reason: str = ""

    @property
    def has_macros(self) -> bool:
        return bool(self.macros)

    @property
    def has_actions(self) -> bool:
        return bool(self.macros or self.actions)

    def macros_as_dicts(self) -> list[dict[str, Any]]:
        return [{"type": m.type, **m.params} for m in self.macros]


@dataclass
class _ParsedLLM:
    speech: str
    macros: list[Macro]
    actions: list[Action]
    raw: str
    data: dict[str, Any]


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
    """Эвристика: сложная команда → сразу slow (если включено)."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if len(t.split()) > config.SIMPLE_COMMAND_MAX_WORDS:
        return True
    if any(m in t for m in _COMPLEX_MARKERS):
        return True
    if any(m in t for m in _SLOW_ONLY_MARKERS):
        return True
    if t.count(",") >= 2:
        return True
    return False


def _command_expects_actions(text: str) -> bool:
    """Команда явно требует макросов (открой, найди…)."""
    t = (text or "").strip().lower()
    return any(v in t for v in _ACTION_VERBS)


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
    t = config.normalize_browser_query(text)
    if not t:
        return None
    if t in ("вк", "вконтакте", "vk"):
        return BrainResponse(
            speech="Открываю ВКонтакте.",
            macros=[Macro("OPEN_VK", {})],
            model_used="fast-path",
            route_tier="fast-path",
            route_reason="template_vk",
        )
    if t in config.BROWSER_SITES and config.should_prefer_browser(t):
        return BrainResponse(
            speech=f"Открываю {t}.",
            macros=[Macro("OPEN_BROWSER", {"query": t})],
            model_used="fast-path",
            route_tier="fast-path",
            route_reason="template_browser",
        )
    for prefix, _engine in config._SEARCH_PREFIXES:
        if t.startswith(prefix + " "):
            term = t[len(prefix) + 1 :].strip()
            if term:
                return BrainResponse(
                    speech="Ищу в интернете.",
                    macros=[Macro("OPEN_BROWSER", {"query": text.strip()})],
                    model_used="fast-path",
                    route_tier="fast-path",
                    route_reason="template_search",
                )
    return None


# ── ModelRouter ───────────────────────────────────────────────────────────────

class ModelRouter:
    """
    Гибридный роутер Ollama: windy-fast → qwen slow при ошибках.

    Правила:
      - complex / Telegram → slow сразу (опционально)
      - простые → fast, проверка JSON + качества макросов
      - fail fast → escalate slow с логированием причины
    """

    def should_skip_fast(self, text: str) -> tuple[bool, str]:
        if not config.is_hybrid_enabled():
            return True, "hybrid_disabled"
        if config.HYBRID_FORCE_SLOW_ON_COMPLEX and is_complex_command(text):
            return True, "complex_command"
        return False, ""

    def evaluate_fast_acceptance(
        self,
        text: str,
        parsed: _ParsedLLM | None,
        *,
        parse_error: str = "",
    ) -> tuple[bool, str]:
        """Проверить, можно ли принять ответ fast-модели без escalate."""
        if parse_error:
            return False, parse_error
        if parsed is None:
            return False, "no_parse"

        if "```" in parsed.raw or parsed.raw.count("{") > parsed.raw.count("}"):
            return False, "hallucination_markdown"

        if not parsed.speech.strip():
            return False, "empty_speech"

        macros = parsed.macros
        if _command_expects_actions(text) and not macros and not parsed.actions:
            return False, "missing_macros"

        if len(macros) > config.HYBRID_FAST_MAX_MACROS:
            return False, f"too_many_macros>{config.HYBRID_FAST_MAX_MACROS}"

        for m in macros:
            mtype = m.type.upper()
            if mtype not in _VALID_MACRO_TYPES:
                return False, f"unknown_macro:{mtype}"
            if mtype in _SLOW_MACRO_TYPES:
                return False, f"slow_macro:{mtype}"
            if mtype == "LAUNCH_APP" and not (m.params.get("name") or m.params.get("app")):
                return False, "launch_app_no_name"
            if mtype == "OPEN_BROWSER" and not (
                m.params.get("query") or m.params.get("q") or m.params.get("url")
            ):
                return False, "open_browser_no_query"

        return True, "ok"


class Brain:
    def __init__(self) -> None:
        self.model = config.OLLAMA_MODEL
        self.host = config.OLLAMA_HOST
        self.system_prompt = _load_prompt()
        self._history: list[dict[str, str]] = []
        self._client = None
        self._router = ModelRouter()
        self._last_command = ""
        self._last_response: BrainResponse | None = None
        self._last_route_log = ""

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

    @property
    def last_route_log(self) -> str:
        return self._last_route_log

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

    def _call_model(
        self,
        text: str,
        *,
        model: str,
        tier: str,
    ) -> tuple[BrainResponse | None, str]:
        """
        Вызов одной модели с retry. Возвращает (response|None, error_reason).
        Не пишет в history — это делает вызывающий после финального ответа.
        """
        last_raw = ""
        last_error = ""

        for attempt in range(config.OLLAMA_JSON_RETRIES + 1):
            try:
                raw = self._chat_once(text, model=model, strict_hint=(attempt > 0))
                last_raw = raw
                data = _extract_json(raw)
                if not data:
                    last_error = "json_parse_failed"
                    logger.warning("[%s/%s] attempt %d: no json %r", tier, model, attempt, raw[:180])
                    continue

                ok, err = _validate_response(data)
                if not ok:
                    last_error = f"schema_invalid:{err}"
                    logger.warning("[%s/%s] attempt %d: %s", tier, model, attempt, err)
                    continue

                speech = str(data.get("speech") or data.get("response") or "").strip()
                macros = _parse_macros(data)
                actions = _parse_actions(data)

                parsed = _ParsedLLM(
                    speech=speech,
                    macros=macros,
                    actions=actions,
                    raw=raw,
                    data=data,
                )
                resp = BrainResponse(
                    speech=speech,
                    macros=macros,
                    actions=actions,
                    raw=raw,
                    model_used=model,
                    route_tier=tier,
                )
                return resp, ""

            except Exception as exc:
                last_error = f"exception:{exc}"
                logger.error("[%s/%s] attempt %d: %s", tier, model, attempt, exc)

        return None, last_error or "unknown_error"

    def _finalize_response(self, text: str, resp: BrainResponse) -> BrainResponse:
        """Сохранить в history и залогировать маршрут."""
        self._history += [
            {"role": "user", "content": text},
            {"role": "assistant", "content": resp.raw},
        ]
        self._history = self._history[-20:]
        self._last_route_log = (
            f"tier={resp.route_tier} model={resp.model_used} reason={resp.route_reason}"
        )
        logger.info(
            "hybrid route: tier=%s model=%s reason=%s macros=%d speech=%r",
            resp.route_tier,
            resp.model_used,
            resp.route_reason or "ok",
            len(resp.macros),
            resp.speech[:60],
        )
        self._last_response = resp
        return resp

    def think(self, text: str) -> BrainResponse:
        text = (text or "").strip()
        if not text:
            return BrainResponse(speech=config.NO_SPEECH)

        self._last_command = text
        router = self._router

        # ── 1) Fast-path без LLM ──
        if config.is_hybrid_enabled():
            fp = _fast_path_macros(text)
            if fp and not is_complex_command(text):
                return self._finalize_response(text, fp)

        # ── 2) Гибрид выключен → одна модель ──
        if not config.is_hybrid_enabled():
            resp, err = self._call_model(text, model=config.OLLAMA_MODEL, tier="single")
            if resp:
                resp.route_reason = "hybrid_off"
                return self._finalize_response(text, resp)
            return BrainResponse(
                speech="Не понял. Повтори, пожалуйста.",
                model_used=config.OLLAMA_MODEL,
                route_tier="single",
                route_reason=err,
            )

        fast_model = config.get_ollama_model_fast()
        slow_model = config.get_ollama_model_slow()

        # ── 3) Сложные → сразу slow ──
        skip_fast, skip_reason = router.should_skip_fast(text)
        if skip_fast and skip_reason != "hybrid_disabled":
            logger.info("hybrid: skip fast → slow (%s)", skip_reason)
            resp, err = self._call_model(text, model=slow_model, tier="slow")
            if resp:
                resp.route_reason = skip_reason
                return self._finalize_response(text, resp)
            return BrainResponse(
                speech="Ollama недоступна. Запусти ollama serve.",
                model_used=slow_model,
                route_tier="slow",
                route_reason=err or skip_reason,
            )

        # ── 4) Попытка fast-модели ──
        logger.info("hybrid: trying fast model=%s", fast_model)
        fast_resp, fast_err = self._call_model(text, model=fast_model, tier="fast")

        if fast_resp:
            parsed = _ParsedLLM(
                speech=fast_resp.speech,
                macros=fast_resp.macros,
                actions=fast_resp.actions,
                raw=fast_resp.raw,
                data=_extract_json(fast_resp.raw) or {},
            )
            accept, reason = router.evaluate_fast_acceptance(text, parsed)
            if accept:
                fast_resp.route_reason = "fast_ok"
                return self._finalize_response(text, fast_resp)
            logger.info("hybrid: escalate fast→slow (%s)", reason)
            escalate_reason = reason
        else:
            escalate_reason = fast_err or "fast_failed"
            logger.info("hybrid: escalate fast→slow (%s)", escalate_reason)

        # ── 5) Slow-модель (fallback) ──
        slow_resp, slow_err = self._call_model(text, model=slow_model, tier="slow")
        if slow_resp:
            slow_resp.route_reason = f"escalated:{escalate_reason}"
            return self._finalize_response(text, slow_resp)

        # fast мог частично сработать — вернуть его с предупреждением
        if fast_resp and fast_resp.macros:
            fast_resp.route_reason = f"slow_failed_using_fast:{slow_err}"
            logger.warning("hybrid: slow failed, using fast fallback")
            return self._finalize_response(text, fast_resp)

        return BrainResponse(
            speech="Не понял. Повтори, пожалуйста.",
            model_used=slow_model,
            route_tier="slow",
            route_reason=slow_err or escalate_reason,
        )

    def check_connection(self) -> bool:
        try:
            models = self._client_get().list().get("models", [])
            names = {m.get("model", m.get("name", "")) for m in models}
            for candidate in (
                config.OLLAMA_MODEL,
                config.get_ollama_model_fast(),
                config.get_ollama_model_slow(),
            ):
                if not candidate:
                    continue
                target = candidate.split(":")[0]
                if any(target in n for n in names):
                    return True
            return False
        except Exception as exc:
            logger.error("Ollama check: %s", exc)
            return False

    def get_hybrid_status(self) -> dict[str, Any]:
        """Статус моделей для GUI / логов."""
        return {
            "hybrid_enabled": config.is_hybrid_enabled(),
            "fast": config.get_ollama_model_fast(),
            "slow": config.get_ollama_model_slow(),
            "last_route": self._last_route_log,
        }