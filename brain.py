"""
Мозг Windy: Ollama + строгий JSON tool calling.

Гарантии:
  - format="json" в Ollama
  - Парсинг с извлечением JSON из markdown
  - Валидация схемы (speech + actions)
  - Retry при невалидном ответе
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import bootstrap  # noqa: F401
import config
from tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT = re.compile(r"(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})", re.DOTALL)


@dataclass
class Action:
    tool: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class BrainResponse:
    speech: str = ""
    actions: list[Action] = field(default_factory=list)
    raw: str = ""

    @property
    def has_actions(self) -> bool:
        return bool(self.actions)


def _load_prompt() -> str:
    try:
        text = config.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        return (
            text.replace("{{APPS}}", ", ".join(sorted(config.APP_PATHS)) or "chrome, telegram")
            .replace("{{GAMES}}", ", ".join(f"{k}:{v}" for k, v in config.STEAM_GAMES.items()) or "-")
            .replace("{{TOOLS}}", ", ".join(sorted(TOOL_REGISTRY.keys())))
        )
    except FileNotFoundError:
        logger.warning("system prompt not found")
        return '{"speech":"...","actions":[]}'


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


def _validate_response(data: dict[str, Any]) -> tuple[bool, str]:
    """Проверка минимальной схемы JSON-ответа."""
    if not isinstance(data, dict):
        return False, "root not object"

    speech = data.get("speech") or data.get("response")
    if speech is not None and not isinstance(speech, str):
        return False, "speech must be string"

    actions = data.get("actions")
    if actions is None:
        return True, ""
    if not isinstance(actions, list):
        return False, "actions must be array"

    for i, item in enumerate(actions):
        if not isinstance(item, dict):
            return False, f"action[{i}] not object"
        tool = item.get("tool") or item.get("name")
        if not tool or not isinstance(tool, str):
            return False, f"action[{i}] missing tool"
        params = item.get("params") or item.get("arguments") or {}
        if not isinstance(params, dict):
            return False, f"action[{i}] params not object"

    return True, ""


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


class Brain:
    def __init__(self) -> None:
        self.model = config.OLLAMA_MODEL
        self.host = config.OLLAMA_HOST
        self.system_prompt = _load_prompt()
        self._history: list[dict[str, str]] = []
        self._client = None

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

    def _chat_once(self, user_text: str, strict_hint: bool = False) -> str:
        hint = ""
        if strict_hint:
            hint = (
                "\n\nВАЖНО: ответь ТОЛЬКО валидным JSON "
                '{"speech":"...","actions":[{"tool":"...","params":{}}]} без markdown.'
            )

        messages = [
            {"role": "system", "content": self.system_prompt},
            *self._history[-16:],
            {"role": "user", "content": user_text + hint},
        ]
        resp = self._client_get().chat(
            model=self.model,
            messages=messages,
            options=_ollama_options(),
            format="json",
        )
        return resp.get("message", {}).get("content", "") or ""

    def think(self, text: str) -> BrainResponse:
        text = (text or "").strip()
        if not text:
            return BrainResponse(speech=config.NO_SPEECH)

        last_raw = ""
        last_error = ""

        for attempt in range(config.OLLAMA_JSON_RETRIES + 1):
            try:
                raw = self._chat_once(text, strict_hint=(attempt > 0))
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
                actions = _parse_actions(data)

                self._history += [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": raw},
                ]
                self._history = self._history[-20:]
                return BrainResponse(speech=speech, actions=actions, raw=raw)

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
            target = self.model.split(":")[0]
            return any(target in n for n in names)
        except Exception as exc:
            logger.error("Ollama check: %s", exc)
            return False