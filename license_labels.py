"""Display names for persisted ARC license tiers."""

LICENSE_DISPLAY_NAMES = {
    "Standard": "ARC Go",
    "Gold": "ARC Pro",
    "Diamond": "ARC Pro Max",
}


def license_display_name(license_type: str) -> str:
    """Return the public-facing name without changing the stored tier key."""
    return LICENSE_DISPLAY_NAMES.get(license_type, license_type)