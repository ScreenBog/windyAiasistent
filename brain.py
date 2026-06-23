"""
Мозг: Ollama + строгий JSON output.
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


def _load_prompt() -> str:
    try:
        p = config.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        return (
            p.replace("{{APPS}}", ", ".join(sorted(config.APP_PATHS)) or "chrome, telegram")
            .replace("{{GAMES}}", ", ".join(f"{k}:{v}" for k, v in config.STEAM_GAMES.items()) or "-")
        )
    except FileNotFoundError:
        return '{"speech":"...","actions":[]}'


def _parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pat in (r"```json\s*(\{.*?\})\s*```", r"(\{.*\})"):
        m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def _parse_actions(data: dict) -> list[Action]:
    raw = data.get("actions") or []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[Action] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or item.get("name") or "").strip()
        if not tool:
            continue
        params = item.get("params") or item.get("arguments") or {}
        if not isinstance(params, dict):
            params = {"value": params}
        out.append(Action(tool=tool, params=params))
    return out


def _ollama_opts() -> dict[str, Any]:
    o = {
        "temperature": config.OLLAMA_TEMPERATURE,
        "num_ctx": config.OLLAMA_NUM_CTX,
        "top_p": config.OLLAMA_TOP_P,
        "repeat_penalty": config.OLLAMA_REPEAT_PENALTY,
    }
    if config.OLLAMA_NUM_GPU >= 0:
        o["num_gpu"] = config.OLLAMA_NUM_GPU
    return o


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

    def think(self, text: str) -> BrainResponse:
        text = (text or "").strip()
        if not text:
            return BrainResponse(speech=config.NO_SPEECH)

        msgs = [
            {"role": "system", "content": self.system_prompt},
            *self._history[-16:],
            {"role": "user", "content": text},
        ]
        try:
            resp = self._client_get().chat(
                model=self.model,
                messages=msgs,
                options=_ollama_opts(),
                format="json",
            )
            raw = resp.get("message", {}).get("content", "") or ""
            data = _parse_json(raw)
            if not data:
                return BrainResponse(speech="Не понял. Повтори.", raw=raw)

            speech = str(data.get("speech") or data.get("response") or "").strip()
            actions = _parse_actions(data)
            self._history += [{"role": "user", "content": text}, {"role": "assistant", "content": raw}]
            self._history = self._history[-20:]
            return BrainResponse(speech=speech, actions=actions, raw=raw)
        except Exception as exc:
            logger.error("Ollama: %s", exc)
            return BrainResponse(speech="Ollama недоступна. Запусти ollama serve.", raw=str(exc))

    def check_connection(self) -> bool:
        try:
            models = self._client_get().list().get("models", [])
            names = {m.get("model", m.get("name", "")) for m in models}
            t = self.model.split(":")[0]
            return any(t in n for n in names)
        except Exception as exc:
            logger.error("Ollama check: %s", exc)
            return False