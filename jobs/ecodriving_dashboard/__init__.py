"""Driver Eco Dashboard V1 — snapshot/data foundation.

This package contains the presentation-oriented per-driver snapshot contract
consumed by the (not yet implemented) Driver Eco Dashboard frontend, its
deterministic derivation from the existing Eco Driving data model, and the
per-pipeline-family source adapters.

It reuses `jobs.ecodriving.eco_scoring` as the single authoritative scoring
source. It never forks a business rule.
"""
