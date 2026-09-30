"""Leaderboard feature surface owned by the Beta command module.

The entry point supplies the process-local collaborators once, when commands
are installed.  Keeping the public feature names here is important: views and
helpers imported by integrations now belong to this module rather than being
registered as adapters in ``main``.
"""
from __future__ import annotations

from types import ModuleType
from typing import Any

from discord.ext import commands

_runtime: dict[str, Any] = {}


def bind_owner(owner: ModuleType) -> None:
    """Bind process-local services and the concrete view types for this bot."""
    global _runtime
    _runtime = dict(owner.__dict__)
    for name in (
        "LeaderboardPanelView",
        "LeaderboardBlueprintOfferView",
        "LeaderboardSettingsView",
        "LeaderboardScrimSelectView",
        "LeaderboardScrimEditView",
        "LeaderboardPointsSystemModal",
        "LeaderboardAccentColorView",
        "LeaderboardAccentColorModal",
        "LeaderboardTeamCountView",
        "LeaderboardOrientationView",
        "IncompleteResultsReviewView",
        "LeaderboardRow",
    ):
        implementation = _runtime.get(name)
        if implementation is not None:
            globals()[name] = implementation


def _implementation(name: str) -> Any:
    implementation = _runtime.get(name)
    if implementation is None:
        raise RuntimeError(f"Leaderboard implementation is unavailable: {name}")
    return implementation


class LeaderboardPanelView:
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardPanelView")(*args, **kwargs)


class LeaderboardBlueprintOfferView(LeaderboardPanelView):
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardBlueprintOfferView")(*args, **kwargs)


class LeaderboardSettingsView(LeaderboardPanelView):
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardSettingsView")(*args, **kwargs)


class LeaderboardScrimSelectView(LeaderboardPanelView):
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardScrimSelectView")(*args, **kwargs)


class LeaderboardScrimEditView(LeaderboardPanelView):
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardScrimEditView")(*args, **kwargs)


class LeaderboardPointsSystemModal(LeaderboardPanelView):
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardPointsSystemModal")(*args, **kwargs)


class LeaderboardAccentColorView(LeaderboardPanelView):
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardAccentColorView")(*args, **kwargs)


class LeaderboardAccentColorModal(LeaderboardPanelView):
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardAccentColorModal")(*args, **kwargs)


class LeaderboardTeamCountView(LeaderboardPanelView):
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardTeamCountView")(*args, **kwargs)


class LeaderboardOrientationView(LeaderboardPanelView):
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardOrientationView")(*args, **kwargs)


class IncompleteResultsReviewView(LeaderboardPanelView):
    def __new__(cls, *args, **kwargs):
        return _implementation("IncompleteResultsReviewView")(*args, **kwargs)


class LeaderboardRow:
    def __new__(cls, *args, **kwargs):
        return _implementation("LeaderboardRow")(*args, **kwargs)


def _leaderboard_profile_suffix(*args, **kwargs):
    return _implementation("_leaderboard_profile_suffix")(*args, **kwargs)


def _leaderboard_background_path(*args, **kwargs):
    return _implementation("_leaderboard_background_path")(*args, **kwargs)


def _leaderboard_background_metadata_path(*args, **kwargs):
    return _implementation("_leaderboard_background_metadata_path")(*args, **kwargs)


def _read_leaderboard_background_metadata(*args, **kwargs):
    return _implementation("_read_leaderboard_background_metadata")(*args, **kwargs)


def _write_leaderboard_background_metadata(*args, **kwargs):
    return _implementation("_write_leaderboard_background_metadata")(*args, **kwargs)


def _leaderboard_scrim_profile(*args, **kwargs):
    return _implementation("_leaderboard_scrim_profile")(*args, **kwargs)


def _leaderboard_profile_accent_color(*args, **kwargs):
    return _implementation("_leaderboard_profile_accent_color")(*args, **kwargs)


def leaderboard_canvas_dimensions(*args, **kwargs):
    return _implementation("leaderboard_canvas_dimensions")(*args, **kwargs)


def _migrate_legacy_leaderboard_background(*args, **kwargs):
    return _implementation("_migrate_legacy_leaderboard_background")(*args, **kwargs)


def _current_leaderboard_background_metadata(*args, **kwargs):
    return _implementation("_current_leaderboard_background_metadata")(*args, **kwargs)


def _current_leaderboard_background_path(*args, **kwargs):
    return _implementation("_current_leaderboard_background_path")(*args, **kwargs)


def _ensure_leaderboard_background_preview(*args, **kwargs):
    return _implementation("_ensure_leaderboard_background_preview")(*args, **kwargs)


def _store_leaderboard_background(*args, **kwargs):
    return _implementation("_store_leaderboard_background")(*args, **kwargs)


def _restore_default_leaderboard_background(*args, **kwargs):
    return _implementation("_restore_default_leaderboard_background")(*args, **kwargs)


def _build_empty_leaderboard_blueprint(*args, **kwargs):
    return _implementation("_build_empty_leaderboard_blueprint")(*args, **kwargs)


def _load_dimensioned_leaderboard_blueprint(*args, **kwargs):
    return _implementation("_load_dimensioned_leaderboard_blueprint")(*args, **kwargs)


def _generate_default_leaderboard_background(*args, **kwargs):
    return _implementation("_generate_default_leaderboard_background")(*args, **kwargs)


def _load_leaderboard_background_canvas(*args, **kwargs):
    return _implementation("_load_leaderboard_background_canvas")(*args, **kwargs)


def _build_configured_leaderboard_image(*args, **kwargs):
    return _implementation("_build_configured_leaderboard_image")(*args, **kwargs)


def build_leaderboard_image(*args, **kwargs):
    return _implementation("build_leaderboard_image")(*args, **kwargs)


async def leaderboard_settings_command(ctx: commands.Context) -> None:
    return await _implementation("leaderboard_settings_command")(ctx)


async def leaderboard_command(ctx: commands.Context) -> None:
    return await _implementation("build_and_send_leaderboard")(ctx)


def install_leaderboard_commands(bot: commands.Bot, owner: ModuleType) -> dict[str, object]:
    bind_owner(owner)
    for name in ("setres", "res", "leaderboard", "lb"):
        bot.remove_command(name)
    bot.command(name="setres")(commands.guild_only()(leaderboard_settings_command))
    bot.command(name="res", aliases=["leaderboard", "lb"])(
        commands.guild_only()(leaderboard_command)
    )
    names = (
        "LeaderboardBackgroundDimensionsError", "LeaderboardPanelView",
        "LeaderboardBlueprintOfferView", "LeaderboardSettingsView",
        "LeaderboardScrimSelectView", "LeaderboardScrimEditView",
        "LeaderboardPointsSystemModal", "LeaderboardAccentColorView",
        "LeaderboardAccentColorModal", "LeaderboardTeamCountView",
        "LeaderboardOrientationView", "IncompleteResultsReviewView",
        "LeaderboardRow", "calculate_leaderboard",
        "build_results_publication_message", "missing_score_matches",
        "parse_match_score_lines", "leaderboard_canvas_dimensions",
        "_leaderboard_profile_suffix", "_leaderboard_background_path",
        "_leaderboard_background_metadata_path",
        "_read_leaderboard_background_metadata",
        "_write_leaderboard_background_metadata", "_leaderboard_scrim_profile",
        "_leaderboard_profile_accent_color", "_migrate_legacy_leaderboard_background",
        "_current_leaderboard_background_metadata", "_current_leaderboard_background_path",
        "_ensure_leaderboard_background_preview", "_store_leaderboard_background",
        "_restore_default_leaderboard_background", "_build_empty_leaderboard_blueprint",
        "_load_dimensioned_leaderboard_blueprint",
        "_generate_default_leaderboard_background",
        "_load_leaderboard_background_canvas", "_build_configured_leaderboard_image",
        "build_leaderboard_image",
    )
    bindings = {name: globals()[name] for name in names if name in globals()}
    bindings.update({
        "leaderboard_settings_command": leaderboard_settings_command,
        "leaderboard_command": leaderboard_command,
    })
    return bindings