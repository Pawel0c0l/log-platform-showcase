"""BRAVO00016 person-family Eco Driving Explorer provider.

Identity and source set only; every behaviour lives in
:mod:`api.eco_driving_explorer.family_provider`.

**This family is not the driver family with different table names.** Its
aggregation job (``jobs/ecodriving_person/job_eco_driving_person_aggregate.py``)
writes ``aggregation_included = (match_count = 1)``: inclusion depends solely on
resolving exactly one physical person for the trip's driver name. The private
driver-tag flag is still recorded on the assignment row, but it has **no bearing
on inclusion**, and migration ``043``'s
``chk_eco_person_trip_assignments_identity_outcome`` ties inclusion to a resolved
identity alone.

An applicable trip marked ``is_private_trip = TRUE`` with
``aggregation_included = TRUE`` therefore **does** contribute to this family's
score, ranking and trip evidence. Applying the driver family's private-trip
exclusion here would silently drop real production data.

Identity is ``person_name_group_key`` — one physical person, possibly several
provider source aliases — and the display name is the canonical name the
assignment already resolved, not a roster lookup.
"""

from __future__ import annotations

from .family_provider import FamilyEcoDrivingProvider
from . import queries as q

PROVIDER_KEY = "bravo00016_person"
CLIENT_CODE = "BRAVO00016"
RANKING_FAMILY = "person"
DISPLAY_NAME = "BRAVO00016 — Eco Driving (osoba)"


class BravoPersonEcoDrivingProvider(FamilyEcoDrivingProvider):
    PROVIDER_KEY = PROVIDER_KEY
    CLIENT_CODE = CLIENT_CODE
    RANKING_FAMILY = RANKING_FAMILY
    DISPLAY_NAME = DISPLAY_NAME
    SOURCES = q.PERSON_SOURCES
