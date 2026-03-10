"""
Seed seasonal_priors table with default weights from the improvement plan.

Snowmobiles are unlikely in summer; boats are unlikely in winter.
These priors are used as a Bayesian confidence adjustment in the voting system.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from db_connection import get_cursor

SEASONAL_WEIGHTS = {
    # Winter classes: high Dec-Feb, low Jun-Aug
    'snowmobile': {12: 2.0, 1: 2.0, 2: 2.0, 3: 1.5, 4: 0.5, 5: 0.1, 6: 0.05, 7: 0.05, 8: 0.05, 9: 0.1, 10: 0.5, 11: 1.5},
    'snowmobile trailer — open': {12: 2.0, 1: 2.0, 2: 2.0, 3: 1.5, 4: 0.5, 5: 0.1, 6: 0.05, 7: 0.05, 8: 0.05, 9: 0.1, 10: 0.5, 11: 1.5},
    'snowmobile trailer — enclosed': {12: 2.0, 1: 2.0, 2: 2.0, 3: 1.5, 4: 0.5, 5: 0.1, 6: 0.05, 7: 0.05, 8: 0.05, 9: 0.1, 10: 0.5, 11: 1.5},
    # Boat classes: high May-Sep, low Nov-Mar
    'bass boat': {1: 0.2, 2: 0.2, 3: 0.3, 4: 0.8, 5: 1.5, 6: 2.0, 7: 2.0, 8: 2.0, 9: 1.5, 10: 0.8, 11: 0.3, 12: 0.2},
    'pontoon': {1: 0.1, 2: 0.1, 3: 0.2, 4: 0.5, 5: 1.5, 6: 2.0, 7: 2.0, 8: 2.0, 9: 1.5, 10: 0.5, 11: 0.2, 12: 0.1},
    'center console': {1: 0.2, 2: 0.2, 3: 0.3, 4: 0.8, 5: 1.5, 6: 2.0, 7: 2.0, 8: 2.0, 9: 1.5, 10: 0.8, 11: 0.3, 12: 0.2},
    'jet ski': {1: 0.05, 2: 0.05, 3: 0.1, 4: 0.3, 5: 1.0, 6: 2.0, 7: 2.5, 8: 2.5, 9: 1.5, 10: 0.3, 11: 0.1, 12: 0.05},
    'kayak': {1: 0.1, 2: 0.1, 3: 0.3, 4: 0.8, 5: 1.5, 6: 2.0, 7: 2.0, 8: 2.0, 9: 1.5, 10: 0.5, 11: 0.2, 12: 0.1},
    'canoe': {1: 0.1, 2: 0.1, 3: 0.3, 4: 0.8, 5: 1.5, 6: 2.0, 7: 2.0, 8: 2.0, 9: 1.5, 10: 0.5, 11: 0.2, 12: 0.1},
    # Boat trailers follow boat season
    'single-axle': {1: 0.2, 2: 0.2, 3: 0.3, 4: 0.8, 5: 1.5, 6: 2.0, 7: 2.0, 8: 2.0, 9: 1.5, 10: 0.8, 11: 0.3, 12: 0.2},
    'tandem-axle': {1: 0.2, 2: 0.2, 3: 0.3, 4: 0.8, 5: 1.5, 6: 2.0, 7: 2.0, 8: 2.0, 9: 1.5, 10: 0.8, 11: 0.3, 12: 0.2},
    'triple-axle': {1: 0.2, 2: 0.2, 3: 0.3, 4: 0.8, 5: 1.5, 6: 2.0, 7: 2.0, 8: 2.0, 9: 1.5, 10: 0.8, 11: 0.3, 12: 0.2},
}


def seed_seasonal_priors():
    """Insert default seasonal priors into the database."""
    inserted = 0
    for tier3_class, monthly_weights in SEASONAL_WEIGHTS.items():
        for month, weight in monthly_weights.items():
            with get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO seasonal_priors (tier3_class, month, prior_weight, source, observation_count)
                    VALUES (%s, %s, %s, 'manual', 0)
                    ON CONFLICT (tier3_class, month) DO UPDATE
                    SET prior_weight = EXCLUDED.prior_weight,
                        source = CASE WHEN seasonal_priors.source = 'learned'
                                      THEN seasonal_priors.source
                                      ELSE EXCLUDED.source END
                """, (tier3_class, month, weight))
                inserted += 1

    print(f"Seeded {inserted} seasonal prior entries for {len(SEASONAL_WEIGHTS)} classes")
    return inserted


if __name__ == '__main__':
    seed_seasonal_priors()
