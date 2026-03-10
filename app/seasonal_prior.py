"""
Seasonal Priors — Bayesian confidence adjustment per Tier 3 class based on time of year.

Snowmobiles are unlikely in July; boats are unlikely in January.
This module adjusts routing scores based on learned or manual seasonal weights.
"""

import logging
from datetime import date, datetime
from typing import Dict, Optional

from db_connection import get_cursor

logger = logging.getLogger(__name__)

# Default weights for classes with strong seasonal patterns
DEFAULT_SEASONAL_WEIGHTS = {
    # (tier3_class, month) -> weight
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


class SeasonalPrior:
    """Bayesian seasonal confidence adjustment for classification routing."""

    def __init__(self):
        self._cache = {}
        self._cache_loaded = False

    def _load_cache(self):
        """Load seasonal priors from DB into memory cache."""
        try:
            with get_cursor(commit=False) as cursor:
                cursor.execute("SELECT tier3_class, month, prior_weight FROM seasonal_priors")
                for row in cursor.fetchall():
                    key = (row['tier3_class'], row['month'])
                    self._cache[key] = row['prior_weight']
            self._cache_loaded = True
        except Exception as e:
            logger.debug(f"Could not load seasonal priors from DB: {e}")
            self._cache_loaded = True  # Don't retry on error

    def get_prior_weight(self, tier3_class: str, ref_date: date = None) -> float:
        """Get the seasonal prior weight for a class at a given date.

        Args:
            tier3_class: Tier 3 classification string
            ref_date: Reference date (defaults to today)

        Returns:
            Multiplicative weight (1.0 = neutral, >1 = more likely, <1 = less likely)
        """
        if not tier3_class:
            return 1.0

        if ref_date is None:
            ref_date = date.today()

        month = ref_date.month

        # Check DB cache first
        if not self._cache_loaded:
            self._load_cache()

        key = (tier3_class.lower(), month)
        if key in self._cache:
            return self._cache[key]

        # Fall back to defaults
        defaults = DEFAULT_SEASONAL_WEIGHTS.get(tier3_class.lower())
        if defaults:
            return defaults.get(month, 1.0)

        return 1.0

    def apply_to_routing_score(self, tier3_class: str, routing_score: float,
                                ref_date: date = None) -> float:
        """Apply seasonal prior to a routing/confidence score.

        Args:
            tier3_class: Tier 3 classification
            routing_score: Original routing score (0-1)
            ref_date: Reference date

        Returns:
            Adjusted score, clamped to [0, 1]
        """
        weight = self.get_prior_weight(tier3_class, ref_date)
        adjusted = routing_score * weight
        return max(0.0, min(1.0, adjusted))

    def learn_from_observations(self, months_back: int = 12):
        """Learn seasonal weights from approved prediction history.

        Computes empirical class frequency by month from approved predictions.
        Updates seasonal_priors table with source='learned'.
        """
        with get_cursor() as cursor:
            cursor.execute("""
                SELECT vehicle_tier3, EXTRACT(MONTH FROM created_at)::int AS month,
                       COUNT(*) AS cnt
                FROM ai_predictions
                WHERE review_status IN ('approved', 'auto_approved')
                  AND vehicle_tier3 IS NOT NULL
                  AND created_at > NOW() - INTERVAL '%s months'
                GROUP BY vehicle_tier3, month
                ORDER BY vehicle_tier3, month
            """, (months_back,))
            rows = cursor.fetchall()

        if not rows:
            return {'learned': 0}

        # Compute per-class monthly distribution
        class_totals = {}
        class_monthly = {}
        for row in rows:
            cls = row['vehicle_tier3']
            month = row['month']
            cnt = row['cnt']
            class_totals[cls] = class_totals.get(cls, 0) + cnt
            if cls not in class_monthly:
                class_monthly[cls] = {}
            class_monthly[cls][month] = cnt

        learned = 0
        for cls, total in class_totals.items():
            if total < 24:  # Need at least 2 per month on average
                continue
            avg_per_month = total / 12.0
            for month in range(1, 13):
                actual = class_monthly.get(cls, {}).get(month, 0)
                if avg_per_month > 0:
                    weight = round(actual / avg_per_month, 2)
                    weight = max(0.05, min(5.0, weight))  # Clamp
                else:
                    weight = 1.0

                with get_cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO seasonal_priors (tier3_class, month, prior_weight, source, observation_count)
                        VALUES (%s, %s, %s, 'learned', %s)
                        ON CONFLICT (tier3_class, month) DO UPDATE
                        SET prior_weight = EXCLUDED.prior_weight,
                            source = 'learned',
                            observation_count = EXCLUDED.observation_count
                    """, (cls, month, weight, actual))
                    learned += 1

        # Reload cache
        self._cache = {}
        self._cache_loaded = False

        return {'learned': learned}
