"""Тонкий адаптер к LLM. Сейчас — Claude; в бою сюда же подключается YandexGPT / GigaChat,
если данные нельзя отправлять за пределы РФ (152-ФЗ). Остальной код зависит только от call_json().
"""
from __future__ import annotations

import json
import os

MODEL = os.getenv("KP_MODEL", "claude-opus-5-5")


class LLMError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def call_json(system: str, user: str, schema: dict, effort: str = "medium") -> dict:
    """Один запрос → JSON, валидный по схеме (structured outputs)."""
    import anthropic

    client = anthropic.Anthropic()
    params = dict(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    try:
        try:
            # при отказе модели API само повторит запрос на запасной модели
            resp = client.beta.messages.create(
                **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        except anthropic.BadRequestError:
            resp = client.messages.create(**params)  # аккаунт/платформа без fallbacks
    except anthropic.AuthenticationError as e:
        raise LLMError("Неверный ключ API") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Превышен лимит запросов к ИИ, повторите позже") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Ошибка ИИ ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("Нет связи с сервисом ИИ") from e

    if resp.stop_reason == "refusal":
        raise LLMError("Модель отказалась обрабатывать текст")
    if resp.stop_reason == "max_tokens":
        raise LLMError("Ответ ИИ обрезан по длине")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError("ИИ вернул некорректный JSON") from e
