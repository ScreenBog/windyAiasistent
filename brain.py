"""
Мозг ассистента: Ollama (GPU, контекст, JSON).
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


def _load_system_prompt() -> str:
    try:
        prompt = config.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
        apps = ", ".join(sorted(config.APP_PATHS.keys())) or "chrome, telegram, steam"
        games = ", ".join(f"{k}({v})" for k, v in config.STEAM_GAMES.items()) or "нет"
        chats = ", ".join(config.TELEGRAM_CHATS.keys()) or "не настроены"
        return (
            prompt.replace("{{APPS}}", apps)
            .replace("{{GAMES}}", games)
            .replace("{{TELEGRAM_CHATS}}", chats)
        )
    except FileNotFoundError:
        return '{"speech":"...", "actions":[]}'


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pat in (r"```(?:json)?\s*(\{.*?\})\s*```", r"(\{.*\})"):
        m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def _parse_actions(data: dict[str, Any]) -> list[Action]:
    raw = data.get("actions") or data.get("action") or []
    if isinstance(raw, dict):
        raw = [raw]
    actions: list[Action] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or item.get("name") or "").strip()
        if not tool:
            continue
        params = item.get("params") or item.get("arguments") or {}
        if not isinstance(params, dict):
            params = {"value": params}
        actions.append(Action(tool=tool, params=params))
    return actions


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
    def __init__(
        self,
        model: str | None = None,
        host: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.model = model or config.OLLAMA_MODEL
        self.host = host or config.OLLAMA_HOST
        self.system_prompt = system_prompt or _load_system_prompt()
        self._history: list[dict[str, str]] = []
        self._client = None
        self.max_history = 20

    def _get_client(self):
        if self._client is None:
            import ollama
            self._client = ollama.Client(host=self.host, timeout=config.OLLAMA_TIMEOUT)
        return self._client

    def reload_prompt(self) -> None:
        config.reload_settings()
        self.system_prompt = _load_system_prompt()
        if self.model != config.OLLAMA_MODEL:
            self.model = config.OLLAMA_MODEL
        if self.host != config.OLLAMA_HOST:
            self.host = config.OLLAMA_HOST
            self._client = None

    def reset_history(self) -> None:
        self._history.clear()

    def think(self, user_text: str) -> BrainResponse:
        user_text = (user_text or "").strip()
        if not user_text:
            return BrainResponse(speech=config.NO_SPEECH)

        messages = [
            {"role": "system", "content": self.system_prompt},
            *self._history[-self.max_history :],
            {"role": "user", "content": user_text},
        ]

        try:
            client = self._get_client()
            response = client.chat(
                model=self.model,
                messages=messages,
                options=_ollama_options(),
                format="json",
            )
            raw = response.get("message", {}).get("content", "") or ""
            data = _extract_json(raw)
            if not data:
                return BrainResponse(speech="Не понял. Повтори, пожалуйста.", raw=raw)

            speech = str(data.get("speech") or data.get("response") or data.get("text") or "").strip()
            actions = _parse_actions(data)

            self._history.append({"role": "user", "content": user_text})
            self._history.append({"role": "assistant", "content": raw})
            if len(self._history) > self.max_history:
                self._history = self._history[-self.max_history :]

            return BrainResponse(speech=speech, actions=actions, raw=raw)

        except Exception as exc:
            logger.error("Ollama: %s", exc)
            return BrainResponse(
                speech="Не могу связаться с Ollama. Запусти ollama serve.",
                raw=str(exc),
            )

    def check_connection(self) -> bool:
        try:
            client = self._get_client()
            models = client.list()
            names = {m.get("model", m.get("name", "")) for m in models.get("models", [])}
            target = self.model.split(":")[0]
            ok = any(target in n for n in names)
            if not ok:
                logger.warning("Модель %s не найдена. Есть: %s", self.model, names)
            return ok
        except Exception as exc:
            logger.error("Ollama: %s", exc)
            return False