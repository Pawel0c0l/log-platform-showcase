"""ALPHA00001 driver-family Eco Driving Explorer provider.

Identity and source set only; every behaviour lives in
:mod:`api.eco_driving_explorer.family_provider`.

This family's aggregation job writes
``aggregation_included = (assigned_id IS NOT NULL AND is_private_trip IS FALSE)``
and migration ``028`` adds ``chk_eco_trip_assignments_private_trip_exclusion``,
so a private trip can be neither written nor read as included. The private-trip
exclusion is therefore preserved here by construction, not by a rule this layer
applies.
"""

from __future__ import annotations

from .family_provider import FamilyEcoDrivingProvider
from . import queries as q

PROVIDER_KEY = "alpha00001_driver"
CLIENT_CODE = "ALPHA00001"
RANKING_FAMILY = "driver"
DISPLAY_NAME = "ALPHA00001 — Eco Driving (kierowca)"


class AlphaDriverEcoDrivingProvider(FamilyEcoDrivingProvider):
    PROVIDER_KEY = PROVIDER_KEY
    CLIENT_CODE = CLIENT_CODE
    RANKING_FAMILY = RANKING_FAMILY
    DISPLAY_NAME = DISPLAY_NAME
    SOURCES = q.DRIVER_SOURCES
