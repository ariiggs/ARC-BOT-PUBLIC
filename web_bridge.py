"""Authenticated HTTP bridge for the ARC beta Interactive workspace.

The bridge runs inside the Discord bot process so web mutations use the same
repository instance, Discord cache, and event loop as !setup. It is disabled
unless both ARC_BETA_WEB_BRIDGE_PORT and ARC_BETA_WEB_BRIDGE_SECRET exist.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import discord

from scrim_state import ScrimRepository

logger = logging.getLogger("pung-scrim-bot.web-bridge")

MAX_BODY_BYTES = 1_000_000
UPDATE_FIELDS = {
    "name",
    "public_channel_id",
    "staff_channel_id",
    "staff_role_id",
    "pending_role_id",
    "confirmed_role_id",
    "cap_channel_id",
    "logs_channel_id",
    "history_channel_id",
    "registration_channel_id",
    "registration_role_id",
    "registration_auto_accept",
    "slot_start",
    "slot_end",
    "max_matches",
    "maps",
    "match_maps",
}
LEADERBOARD_FIELDS = {
    "kill_points_value",
    "placement_points_string",
    "leaderboard_layout",
    "leaderboard_background",
}
EMOJI_FIELDS = {
    "emoji_available",
    "emoji_reserved",
    "emoji_pending",
    "emoji_confirmed",
}
CREATE_FIELDS = UPDATE_FIELDS | LEADERBOARD_FIELDS | {"is_open"}


class BridgeRequestError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _response(status: int, payload: object) -> bytes:
    body = _json_bytes(payload)
    reason = {
        200: "OK",
        201: "Created",
        204: "No Content",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }.get(status, "Error")
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body


def _scrim_payload(repository: ScrimRepository, scrim: object) -> dict:
    entry = scrim.payload()
    config = repository.get_idpw_config(scrim.id)
    entry["idpw_config"] = config.payload() if config is not None else None
    return entry


def _snapshot(repository: ScrimRepository, guild_id: int) -> dict:
    server_config = repository.get_server_config(guild_id)
    return {
        "guild_id": guild_id,
        "server_config": (
            server_config.payload() if server_config is not None else None
        ),
        "scrims": [
            _scrim_payload(repository, scrim)
            for scrim in repository.list(guild_id)
        ],
    }


def _as_object(body: object) -> dict:
    if not isinstance(body, dict):
        raise BridgeRequestError(400, "The request body must be a JSON object.")
    return body


def _positive_id(value: object, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or value <= 0:
        raise BridgeRequestError(400, f"{field} must be a positive integer.")
    return value


class InteractiveBridge:
    def __init__(
        self,
        bot: discord.Client,
        repository: ScrimRepository,
        publish_scrim: Callable[[object], Awaitable[object]],
    ):
        self.bot = bot
        self.repository = repository
        self.publish_scrim = publish_scrim
        self._mutation_lock = asyncio.Lock()
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> bool:
        port_value = os.getenv("ARC_BETA_WEB_BRIDGE_PORT", "").strip()
        secret = os.getenv("ARC_BETA_WEB_BRIDGE_SECRET", "")
        if not port_value or not secret:
            logger.info(
                "Interactive bridge disabled; configure "
                "ARC_BETA_WEB_BRIDGE_PORT and ARC_BETA_WEB_BRIDGE_SECRET to enable it."
            )
            return False
        try:
            port = int(port_value)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            logger.error("ARC_BETA_WEB_BRIDGE_PORT must be a valid TCP port.")
            return False

        host = os.getenv("ARC_BETA_WEB_BRIDGE_HOST", "0.0.0.0").strip() or "0.0.0.0"
        try:
            self.server = await asyncio.start_server(
                self._handle_client,
                host,
                port,
                limit=MAX_BODY_BYTES + 16_384,
            )
        except OSError:
            logger.exception("Could not start the Interactive bridge on %s:%s.", host, port)
            return False
        logger.info("Interactive bridge listening on %s:%s.", host, port)
        return True

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            method, target, headers, body = await self._read_request(reader)
            if not hmac.compare_digest(
                headers.get("authorization", ""),
                f"Bearer {os.getenv('ARC_BETA_WEB_BRIDGE_SECRET', '')}",
            ):
                writer.write(_response(401, {"error": "Bridge authentication required."}))
                await writer.drain()
                return
            status, payload = await self._dispatch(method, target, body)
            writer.write(_response(status, payload))
            await writer.drain()
        except BridgeRequestError as error:
            writer.write(_response(error.status, {"error": str(error)}))
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        except Exception:
            logger.exception("Interactive bridge request failed.")
            writer.write(
                _response(
                    500,
                    {"error": "The Interactive bridge could not complete the request."},
                )
            )
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, str], object]:
        request_line = await reader.readline()
        if not request_line:
            raise BridgeRequestError(400, "Missing HTTP request.")
        parts = request_line.decode("latin1").strip().split()
        if len(parts) != 3 or parts[2] != "HTTP/1.1":
            raise BridgeRequestError(400, "Only HTTP/1.1 requests are supported.")
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            if b":" not in line:
                raise BridgeRequestError(400, "Malformed HTTP header.")
            name, value = line.decode("latin1").split(":", 1)
            headers[name.strip().lower()] = value.strip()
        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError as error:
            raise BridgeRequestError(400, "Invalid Content-Length.") from error
        if content_length > MAX_BODY_BYTES:
            raise BridgeRequestError(413, "Request body is too large.")
        raw_body = await reader.readexactly(content_length) if content_length else b""
        if not raw_body:
            body: object = {}
        else:
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError as error:
                raise BridgeRequestError(400, "Request body must be valid JSON.") from error
        return parts[0].upper(), parts[1], headers, body

    def _guild(self, guild_id: int) -> discord.Guild:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise BridgeRequestError(503, "The bot is not connected to this guild.")
        if not self.repository.is_guild_authorized(guild_id):
            raise BridgeRequestError(403, "This guild is not authorized for ARC Beta.")
        return guild

    @staticmethod
    def _validate_refs(guild: discord.Guild, values: dict) -> None:
        channel_fields = {
            key
            for key in (
                "public_channel_id",
                "staff_channel_id",
                "cap_channel_id",
                "logs_channel_id",
                "history_channel_id",
                "registration_channel_id",
            )
            if key in values
        }
        for field in channel_fields:
            value = _positive_id(values[field], field, optional=True)
            if value is not None and not isinstance(guild.get_channel(value), discord.TextChannel):
                raise BridgeRequestError(400, f"{field} must reference a text channel in this guild.")
        for field in ("staff_role_id", "pending_role_id", "confirmed_role_id", "registration_role_id"):
            if field not in values:
                continue
            value = _positive_id(values[field], field, optional=True)
            role = guild.get_role(value) if value is not None else None
            if value is not None and (
                role is None or role.is_default() or role.managed
            ):
                raise BridgeRequestError(400, f"{field} must reference a regular role in this guild.")

    async def _dispatch(self, method: str, target: str, body: object) -> tuple[int, object]:
        path = urlsplit(target).path.rstrip("/")
        parts = path.split("/")
        if path == "/healthz" and method == "GET":
            return 200, {"ok": True}
        if len(parts) < 4 or parts[1:3] != ["v1", "setup"]:
            raise BridgeRequestError(404, "Unknown bridge endpoint.")
        try:
            guild_id = int(parts[3])
        except ValueError as error:
            raise BridgeRequestError(400, "Invalid guild ID.") from error
        guild = self._guild(guild_id)

        if len(parts) == 4 and method == "GET":
            return 200, _snapshot(self.repository, guild_id)
        if len(parts) == 5 and parts[4] == "scrims" and method == "POST":
            return await self._create_scrim(guild, guild_id, _as_object(body))
        if len(parts) < 6 or parts[4] != "scrims":
            raise BridgeRequestError(404, "Unknown bridge endpoint.")
        scrim_id = parts[5]
        if len(parts) == 6 and method in {"PATCH", "POST"}:
            return await self._update_scrim(guild, guild_id, scrim_id, _as_object(body))
        if len(parts) == 6 and method == "DELETE":
            return await self._delete_scrim(guild, guild_id, scrim_id)
        if len(parts) == 7 and parts[6] == "actions" and method == "POST":
            action = _as_object(body).get("action")
            if not isinstance(action, str):
                raise BridgeRequestError(400, "Action is required.")
            return await self._scrim_action(guild_id, scrim_id, action)
        raise BridgeRequestError(404, "Unknown bridge endpoint.")

    async def _create_scrim(
        self, guild: discord.Guild, guild_id: int, body: dict
    ) -> tuple[int, object]:
        unknown = set(body) - CREATE_FIELDS - {"idpw"}
        if unknown:
            raise BridgeRequestError(400, f"Unknown create fields: {sorted(unknown)}.")
        values = {key: body[key] for key in CREATE_FIELDS if key in body}
        required = {
            "name",
            "public_channel_id",
            "staff_channel_id",
            "logs_channel_id",
            "history_channel_id",
            "pending_role_id",
            "confirmed_role_id",
            "max_matches",
        }
        if not required.issubset(values):
            raise BridgeRequestError(
                400,
                "The setup-required name, channels, captain roles, and match count are required.",
            )
        idpw = body.get("idpw")
        if not isinstance(idpw, dict) or not {
            "target_channel_id",
            "password_type",
            "timezone_name",
        }.issubset(idpw):
            raise BridgeRequestError(
                400,
                "ID/password target, password mode, and timezone are required.",
            )
        self._validate_refs(guild, values)
        async with self._mutation_lock:
            try:
                scrim = self.repository.create(guild_id, **values)
                self._save_idpw(scrim.id, idpw)
                await self.publish_scrim(scrim)
            except (TypeError, ValueError) as error:
                raise BridgeRequestError(400, str(error)) from error
        return 201, _scrim_payload(self.repository, scrim)

    async def _update_scrim(
        self, guild: discord.Guild, guild_id: int, scrim_id: str, body: dict
    ) -> tuple[int, object]:
        unknown = set(body) - UPDATE_FIELDS - LEADERBOARD_FIELDS - EMOJI_FIELDS - {"idpw", "emojis"}
        if unknown:
            raise BridgeRequestError(400, f"Unknown update fields: {sorted(unknown)}.")
        changes = {key: body[key] for key in UPDATE_FIELDS if key in body}
        leaderboard = {key: body[key] for key in LEADERBOARD_FIELDS if key in body}
        self._validate_refs(guild, changes)
        async with self._mutation_lock:
            scrim = self.repository.get(scrim_id)
            if scrim is None or scrim.guild_id != guild_id:
                raise BridgeRequestError(404, "This scrim does not exist on this server.")
            try:
                if changes:
                    scrim = self.repository.update_scrim(scrim_id, guild_id, **changes)
                if leaderboard:
                    scrim = self.repository.update_leaderboard_settings(
                        scrim_id, guild_id, **leaderboard
                    )
                if "idpw" in body:
                    self._save_idpw(scrim_id, body["idpw"])
                emoji_values = body.get("emojis")
                if emoji_values is not None:
                    if not isinstance(emoji_values, dict):
                        raise BridgeRequestError(400, "emojis must be a JSON object.")
                    for field_name, emoji in emoji_values.items():
                        if field_name not in EMOJI_FIELDS:
                            raise BridgeRequestError(400, f"Unknown emoji field: {field_name}.")
                        scrim = self.repository.save_scrim_emoji(
                            scrim_id, guild_id, field_name, emoji
                        )
                await self.publish_scrim(scrim)
            except (TypeError, ValueError) as error:
                raise BridgeRequestError(400, str(error)) from error
        return 200, _scrim_payload(self.repository, scrim)

    def _save_idpw(self, scrim_id: str, value: object) -> None:
        config = _as_object(value)
        current = self.repository.get_idpw_config(scrim_id)
        target = config.get(
            "target_channel_id",
            current.target_channel_id if current is not None else None,
        )
        password = config.get(
            "fixed_password",
            current.fixed_password if current is not None else "",
        )
        timezone_name = config.get(
            "timezone_name",
            current.timezone_name if current is not None else "UTC",
        )
        password_type = config.get(
            "password_type",
            "fixed" if current is None else "dynamic",
        )
        self.repository.save_idpw_config(
            scrim_id,
            target_channel_id=_positive_id(target, "target_channel_id") or 0,
            fixed_password=password if isinstance(password, str) else "",
            timezone_name=timezone_name if isinstance(timezone_name, str) else "",
            password_type=password_type if isinstance(password_type, str) else "",
            announcement_message_id=(
                _positive_id(config["announcement_message_id"], "announcement_message_id", optional=True)
                if "announcement_message_id" in config
                else (current.announcement_message_id if current is not None else None)
            ),
        )

    async def _delete_scrim(
        self, guild: discord.Guild, guild_id: int, scrim_id: str
    ) -> tuple[int, object]:
        async with self._mutation_lock:
            scrim = self.repository.get(scrim_id)
            if scrim is None or scrim.guild_id != guild_id:
                raise BridgeRequestError(404, "This scrim does not exist on this server.")
            old = _scrim_payload(self.repository, scrim)
            for channel_id, message_id in (
                (scrim.public_channel_id, scrim.public_message_id),
                (scrim.staff_channel_id, scrim.staff_message_id),
            ):
                if channel_id and message_id:
                    channel = guild.get_channel(channel_id)
                    if isinstance(channel, discord.TextChannel):
                        try:
                            message = await channel.fetch_message(message_id)
                            await message.edit(view=None)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            logger.warning("Could not retire deleted scrim board %s.", scrim_id)
            self.repository.delete(scrim_id, guild_id)
        return 200, {"deleted": True, "scrim": old}

    async def _scrim_action(
        self, guild_id: int, scrim_id: str, action: str
    ) -> tuple[int, object]:
        if action not in {"open", "close", "reset_counter"}:
            raise BridgeRequestError(400, "Unknown scrim action.")
        async with self._mutation_lock:
            scrim = self.repository.get(scrim_id)
            if scrim is None or scrim.guild_id != guild_id:
                raise BridgeRequestError(404, "This scrim does not exist on this server.")
            with self.repository.transaction():
                if action == "open":
                    scrim.is_open = True
                elif action == "close":
                    scrim.is_open = False
                else:
                    scrim.current_match_counter = 1
            await self.publish_scrim(scrim)
        return 200, _scrim_payload(self.repository, scrim)


async def start_interactive_bridge(
    bot: discord.Client,
    repository: ScrimRepository,
    publish_scrim: Callable[[object], Awaitable[object]],
) -> InteractiveBridge:
    bridge = InteractiveBridge(bot, repository, publish_scrim)
    await bridge.start()
    return bridge