"""Compatibility exports for integrations using the original flat module path."""

from arc_bot.domain.license_labels import (
    LICENSE_DISPLAY_NAMES,
    license_display_name,
)

__all__ = ["LICENSE_DISPLAY_NAMES", "license_display_name"]