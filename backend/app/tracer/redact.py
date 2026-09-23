"""Маскирование персональных данных в атрибутах span'ов (ADR 0013).

Применяется и к локальному хранилищу, и к OTLP-экспорту: сырой телефон, ИИН, email
и ФИО не должны попадать в трассу ни под каким ключом.
"""

import re
from collections.abc import Mapping
from typing import Any

MASK = "[redacted]"

# Последний сегмент ключа: `client.phone`, `phone`, `user.email` и т.п.
DENY_KEYS = frozenset({
    "phone", "phone_number", "iin", "email", "full_name", "fio",
    "first_name", "last_name", "birth_date",
})

# Префикс атрибутов с содержимым (промпты, транскрипты): хранятся локально,
# в OTLP уходят только при TRACE_EXPORT_CONTENT=true.
LOCAL_PREFIX = "local."

_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email
    re.compile(r"(?:\+7|\b8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b"),  # телефон KZ
    re.compile(r"\b\d{12}\b"),  # ИИН
)


def mask_text(value: str) -> str:
    for pattern in _PATTERNS:
        value = pattern.sub(MASK, value)
    return value


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, (list, tuple)):
        return [mask_text(v) if isinstance(v, str) else v for v in value]
    return value


def redact(attributes: Mapping[str, Any] | None, *, keep_local: bool = True) -> dict[str, Any]:
    """Копия атрибутов: ключи из DENY_KEYS замаскированы, в строках замаскированы шаблоны ПД."""
    out: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if not keep_local and key.startswith(LOCAL_PREFIX):
            continue
        out[key] = MASK if key.rsplit(".", 1)[-1].lower() in DENY_KEYS else _mask_value(value)
    return out
