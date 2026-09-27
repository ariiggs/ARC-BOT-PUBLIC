"""Client for the shared ARC authorization API."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


ARC_PRODUCTS = ("beta", "public")
AUTHORIZATION_CACHE_SECONDS = 45


class SharedAuthorizationError(RuntimeError):
    """Raised when a shared authorization request cannot be completed."""


@dataclass(frozen=True)
class SharedAuthorization:
    product: str
    guild_id: int
    license_type: str
    duration_days: int
    expires_at: datetime | None


def _parse_expiration(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SharedAuthorizationError("The shared service returned invalid data.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SharedAuthorizationError(
            "The shared service returned invalid data."
        ) from error
    if parsed.tzinfo is None:
        raise SharedAuthorizationError("The shared service returned invalid data.")
    return parsed.astimezone(timezone.utc)


class SharedAuthorizationClient:
    def __init__(self, product: str, repository):
        if product not in ARC_PRODUCTS:
            raise ValueError("ARC_AUTH_PRODUCT must be beta or public.")
        self.product = product
        self.repository = repository
        self.base_url = os.getenv("ARC_AUTH_API_URL", "").strip().rstrip("/")
        self.token = os.getenv("ARC_AUTH_API_TOKEN", "").strip()
        self.requires_shared_api = bool(self.base_url or self.token)
        self.configured = bool(self.base_url and self.token)
        self._bootstrap_complete = False
        self._last_refresh = 0.0
        self._refresh_lock = asyncio.Lock()

    @property
    def has_snapshot(self) -> bool:
        return self.repository.shared_authorizations is not None

    def _request_sync(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> dict:
        if not self.configured:
            raise SharedAuthorizationError(
                "ARC_AUTH_API_URL and ARC_AUTH_API_TOKEN must both be configured."
            )
        parsed_base = urlsplit(self.base_url)
        if parsed_base.scheme != "https" and parsed_base.hostname not in {
            "localhost",
            "127.0.0.1",
        }:
            raise SharedAuthorizationError(
                "The shared authorization API must use HTTPS."
            )
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}/arc/authorizations{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=8) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise SharedAuthorizationError(
                f"The shared authorization service returned HTTP {error.code}."
            ) from error
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SharedAuthorizationError(
                "The shared authorization service could not be reached."
            ) from error
        if not isinstance(result, dict):
            raise SharedAuthorizationError("The shared service returned invalid data.")
        return result

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> dict:
        return await asyncio.to_thread(self._request_sync, method, path, payload)

    def _parse_record(self, product: str, value: object) -> SharedAuthorization:
        if not isinstance(value, dict):
            raise SharedAuthorizationError("The shared service returned invalid data.")
        try:
            record_product = value["product"]
            raw_guild_id = value["guildId"]
            if (
                not isinstance(raw_guild_id, str)
                or not raw_guild_id.isdigit()
                or len(raw_guild_id) > 20
            ):
                raise ValueError("Invalid guild ID.")
            guild_id = int(raw_guild_id)
            license_type = value["licenseType"]
            duration_days = value["durationDays"]
        except (KeyError, TypeError, ValueError) as error:
            raise SharedAuthorizationError(
                "The shared service returned invalid data."
            ) from error
        if (
            record_product != product
            or guild_id <= 0
            or license_type not in {"Standard", "Gold", "Diamond"}
            or type(duration_days) is not int
            or duration_days < 0
        ):
            raise SharedAuthorizationError("The shared service returned invalid data.")
        return SharedAuthorization(
            product=product,
            guild_id=guild_id,
            license_type=license_type,
            duration_days=duration_days,
            expires_at=_parse_expiration(value.get("expiresAt")),
        )

    async def list_authorizations(
        self, product: str
    ) -> list[SharedAuthorization]:
        if product not in ARC_PRODUCTS:
            raise ValueError("Choose ARC Beta or ARC Public.")
        result = await self._request("GET", f"/{product}")
        raw_records = result.get("records")
        if not isinstance(raw_records, list):
            raise SharedAuthorizationError("The shared service returned invalid data.")
        return [self._parse_record(product, record) for record in raw_records]

    async def import_legacy_authorizations(self) -> None:
        if self._bootstrap_complete:
            return
        local_authorizations = self.repository.list_authorizations(
            include_expired=True
        )
        records = []
        for guild_id, expires_at, duration_days in local_authorizations:
            license_type = self.repository.authorized_guild_license_types.get(
                guild_id
            )
            if license_type is None:
                license_type = self.repository.get_server_license_type(guild_id)
            records.append(
                {
                    "guildId": str(guild_id),
                    "licenseType": license_type,
                    "durationDays": duration_days,
                    "expiresAt": expires_at.isoformat() if expires_at else None,
                }
            )
        if len(records) > 1000:
            raise SharedAuthorizationError(
                "More than 1,000 local authorizations need to be imported."
            )
        result = await self._request(
            "POST",
            f"/{self.product}/import",
            {"records": records},
        )
        if (
            type(result.get("imported")) is not int
            or type(result.get("alreadyImported")) is not bool
        ):
            raise SharedAuthorizationError("The shared service returned invalid data.")
        self._bootstrap_complete = True

    async def authorize_guild(
        self,
        product: str,
        guild_id: int,
        duration_days: int,
        license_type: str,
    ) -> SharedAuthorization:
        result = await self._request(
            "PUT",
            f"/{product}/{quote(str(guild_id), safe='')}",
            {
                "licenseType": license_type,
                "durationDays": duration_days,
            },
        )
        return self._parse_record(product, result)

    async def revoke_guild(self, product: str, guild_id: int) -> bool:
        result = await self._request(
            "DELETE",
            f"/{product}/{quote(str(guild_id), safe='')}",
        )
        if type(result.get("removed")) is not bool:
            raise SharedAuthorizationError("The shared service returned invalid data.")
        return result["removed"]

    async def refresh_current(self, *, force: bool = False) -> bool:
        if not self.requires_shared_api:
            return True
        if not self.configured:
            return False
        if (
            not force
            and self.has_snapshot
            and time.monotonic() - self._last_refresh < AUTHORIZATION_CACHE_SECONDS
        ):
            return True

        async with self._refresh_lock:
            if (
                not force
                and self.has_snapshot
                and time.monotonic() - self._last_refresh
                < AUTHORIZATION_CACHE_SECONDS
            ):
                return True
            try:
                await self.import_legacy_authorizations()
                records = await self.list_authorizations(self.product)
                self.repository.set_shared_authorizations(
                    [
                        {
                            "guildId": record.guild_id,
                            "licenseType": record.license_type,
                            "durationDays": record.duration_days,
                            "expiresAt": record.expires_at,
                        }
                        for record in records
                    ]
                )
            except (SharedAuthorizationError, ValueError):
                return False
            self._last_refresh = time.monotonic()
            return True