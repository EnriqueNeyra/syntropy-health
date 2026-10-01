"""One-time upgrade of data created by the pre-1.0 prototype."""

from __future__ import annotations

import logging

from app.core import settings
from app.core.db import db
from app.store import biometrics, profiles

log = logging.getLogger("syntropy.legacy")


def upgrade_if_needed() -> None:
    if not settings.get("legacy_upgrade_pending") or settings.get("legacy_upgrade_done"):
        return
    prof = profiles.default_profile()
    with db() as conn:
        # Samples predate profiles: attribute them to the default profile.
        moved = conn.execute("UPDATE biometric_samples SET profile_id = ? WHERE profile_id IS NULL", (prof["id"],)).rowcount
        conn.execute("UPDATE sync_batches SET profile_id = ? WHERE profile_id IS NULL", (prof["id"],))
        # Oura and WHOOP report RMSSD, which the prototype mislabelled as SDNN.
        conn.execute("UPDATE biometric_samples SET metric_type = 'hrv_rmssd' WHERE metric_type = 'hrv_sdnn' "
                     "AND (source_name LIKE '%Oura%' OR source_name LIKE '%WHOOP%')")
        # WHOOP kilojoules are total (not active) energy.
        conn.execute("UPDATE biometric_samples SET metric_type = 'total_energy' WHERE metric_type = 'active_energy' "
                     "AND source_name LIKE '%WHOOP%'")
    biometrics.rebuild_daily(prof["id"])
    settings.set("legacy_upgrade_done", True)
    log.info("Upgraded prototype database: %s wearable samples attached to profile %s", moved, prof["name"])
