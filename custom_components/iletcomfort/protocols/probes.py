"""Optional C3 subtype probe specification (#110)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OptionalProbe:
    """One optional C3 subtype a dialect asks the coordinator to query (#110).

    The dialect only decides *what* to probe; the coordinator owns the
    session-scoped ``ProbeState`` (per ``subtype``), the client fetcher, the
    session-disable on a first ``ApiError`` and the transient cache fallback.
    ``requires`` names a subtype whose frame must be present in this poll's
    ``raw_frames`` (a fresh reply or the transient fallback) before this
    probe runs; probes run in the order returned.
    """

    subtype: int
    label: str
    min_len: int
    requires: int | None = None
