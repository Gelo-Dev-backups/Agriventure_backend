"""
utils/recommendation_engine.py
Document-grounded (well - rule-table-grounded) advice for the Advice
screen: cross-references an incoming sensor reading against the
"advice_rules" table (see migrations/004_role_battery_advice_engine.sql
for the seeded starter rules and their honestly-labeled internal source)
rather than generating free-form AI text. Every recommendation this
produces carries the rule's priority/source_reference/recommended_action
straight through, so the app can always show *why* a card exists.

Called from routes/sensors.py immediately after a reading is stored - a
direct 1:1 port of agriventure_backedn_xampp's lib/RecommendationEngine.php,
kept behaviorally identical (same dedupe rule, same message templating) so
either backend produces the same advice for the same data.
"""

from typing import Optional

from config import db_cursor, logger


def evaluate_reading(farm_id: int, crop_type: Optional[str], reading: dict) -> None:
    """
    Advice generation is a side effect of a successfully-recorded reading,
    not part of that atomic insert - callers should invoke this AFTER
    committing the reading, and never let a failure here roll back an
    already-committed reading (hence the try/except swallow below, mirroring
    the audit-logging pattern elsewhere in this codebase).
    """
    try:
        with db_cursor() as cur:
            cur.execute(
                '''SELECT * FROM "advice_rules"
                   WHERE is_active = true AND (crop_type = %s OR crop_type IS NULL)''',
                (crop_type,),
            )
            rules = cur.fetchall()

        for rule in rules:
            value = reading.get(rule["metric"])
            if value is None:
                continue
            value = float(value)
            threshold = float(rule["threshold"])

            breached = value < threshold if rule["comparator"] == "lt" else value > threshold
            if breached:
                _maybe_create_recommendation(farm_id, rule, value)
    except Exception as e:
        logger.warning(f"RecommendationEngine.evaluate_reading failed (non-fatal): {e}")


def _maybe_create_recommendation(farm_id: int, rule: dict, value: float) -> None:
    rec_type = rule["metric"]

    with db_cursor(commit=True) as cur:
        # Dedupe: one bad reading shouldn't spam a fresh card on every
        # single ingest - skip if this farm already has an unacknowledged
        # recommendation of the same type.
        cur.execute(
            '''SELECT recommendation_id FROM "recommendations"
               WHERE farm_id = %s AND recommendation_type = %s AND is_acknowledged = false
               LIMIT 1''',
            (farm_id, rec_type),
        )
        if cur.fetchone():
            return

        message = rule["problem_template"].replace("{value}", f"{value:.1f}")

        cur.execute(
            '''INSERT INTO "recommendations"
                   (farm_id, recommendation_type, message, priority, source_reference, recommended_action)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING recommendation_id''',
            (farm_id, rec_type, message, rule["priority"], rule["source_reference"], rule["recommended_action"]),
        )
        recommendation_id = cur.fetchone()["recommendation_id"]

        cur.execute('SELECT user_id, farm_name FROM "farms" WHERE farm_id = %s', (farm_id,))
        farm = cur.fetchone()
        if not farm:
            return

        priority_label = rule["priority"].capitalize()
        cur.execute(
            '''INSERT INTO "notifications" (user_id, recommendation_id, title, body)
               VALUES (%s, %s, %s, %s)''',
            (
                farm["user_id"],
                recommendation_id,
                f"{priority_label} advice: {farm['farm_name']}",
                message,
            ),
        )
