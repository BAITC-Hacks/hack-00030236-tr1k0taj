"""Mock backend data: clients, policies, claims, payments. Missing data -> None / [] (kernel maps
it to the `not_found` error code from actions.json)."""

import re

from app.knowledge.store import Store
from app.knowledge.types import Claim, Client, Payment, Policy

CONTACT_FIELDS = {"phone", "email", "address"}


def normalize_phone(raw: str) -> str | None:
    """'8 701 000 00 04', '+7(701)0000004', '7010000004' -> '+77010000004' (slot `phone` format)."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    return None


class Records:
    def __init__(self, store: Store):
        self._store = store

    async def find_client(
        self, *, phone: str | None = None, iin: str | None = None
    ) -> Client | None:
        if phone:
            normalized = normalize_phone(phone)
            found = await self._store.find("client", phone=normalized) if normalized else []
        elif iin:
            found = await self._store.find("client", iin=re.sub(r"\D", "", iin))
        else:
            raise ValueError("phone or iin is required")
        return Client.model_validate(found[0]) if found else None

    async def client(self, client_id: str) -> Client | None:
        p = await self._store.get("client", client_id)
        return Client.model_validate(p) if p else None

    async def policies(self, client_id: str) -> list[Policy]:
        return [
            Policy.model_validate(p) for p in await self._store.find("policy", client_id=client_id)
        ]

    async def policy(self, policy_number: str) -> Policy | None:
        p = await self._store.get("policy", policy_number)
        return Policy.model_validate(p) if p else None

    async def claims(self, client_id: str) -> list[Claim]:
        return [
            Claim.model_validate(p) for p in await self._store.find("claim", client_id=client_id)
        ]

    async def claim(self, claim_number: str) -> Claim | None:
        p = await self._store.get("claim", claim_number.upper())
        return Claim.model_validate(p) if p else None

    async def payments(self, client_id: str, date: str | None = None) -> list[Payment]:
        """date is ISO 'YYYY-MM-DD', already resolved against DATASET_TODAY by the caller."""
        fields = {"client_id": client_id} | ({"date": date} if date else {})
        return [Payment.model_validate(p) for p in await self._store.find("payment", **fields)]

    async def update_contact(self, client_id: str, field: str, value: str) -> Client:
        """Irreversible: call only after explicit, parameter-bound confirmation (ADR 0005)."""
        if field not in CONTACT_FIELDS:
            raise ValueError(f"contact_field must be one of {sorted(CONTACT_FIELDS)}")
        if field == "phone":
            normalized = normalize_phone(value)
            if not normalized:
                raise ValueError("phone must match +7XXXXXXXXXX")
            value = normalized
        payload = await self._store.get("client", client_id)
        if payload is None:
            raise LookupError(f"client {client_id} not found")
        payload[field] = value
        await self._store.put("client", client_id, payload)
        return Client.model_validate(payload)
