# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared variance primitives and the result type for derived ACS estimates.

Every module that produces a derived estimate (``derived``, ``replicate``,
``aggregate``, ``medians``, ``geo_crosswalk``) returns :class:`DerivedEstimate`
or a DataFrame with the same fields as columns, so downstream code and the
validation suite can treat them uniformly.

Replicate variance (successive difference replication, 80 replicates):

    Var(X) = 4/80 * sum_{r=1..80} (X_r - X)^2
    SE = sqrt(Var), MOE90 = 1.645 * SE

Source: ACS Variance Replicate Estimate Tables documentation, U.S. Census
Bureau (https://www.census.gov/programs-surveys/acs/data/variance-tables.html).
"""

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np

# ACS published MOEs are 90% margins; Census uses 1.645 (not 1.6449) for them.
Z90 = 1.645
REPLICATE_COUNT = 80
REPLICATE_FACTOR = 4.0 / REPLICATE_COUNT
REPLICATE_COLUMNS: List[str] = [f"rep_{i}" for i in range(1, REPLICATE_COUNT + 1)]

# Method identifiers recorded on every derived estimate.
METHOD_PUBLISHED = "published"
METHOD_SUM = "sum"
METHOD_DIFFERENCE = "difference"
METHOD_PROPORTION = "proportion"
METHOD_PROPORTION_AS_RATIO = "proportion_ratio_fallback"
METHOD_RATIO = "ratio"
METHOD_PRODUCT = "product"
METHOD_PERCENT_CHANGE = "percent_change"
METHOD_REPLICATE = "replicate"
METHOD_MEDIAN_INTERPOLATED = "median_interpolated"
METHOD_APPORTIONED = "apportioned"

ArrayLike = Union[float, Sequence[float], np.ndarray]


@dataclass
class DerivedEstimate:
    """A derived estimate with its 90% MOE and the method that produced it."""

    estimate: float
    moe: float
    method: str
    se: float = math.nan
    cv_percent: float = math.nan
    notes: List[str] = field(default_factory=list)
    components: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if math.isnan(self.se) and not math.isnan(self.moe):
            self.se = abs(self.moe) / Z90
        if math.isnan(self.cv_percent) and self.estimate not in (0, None) and not math.isnan(self.se):
            self.cv_percent = self.se / abs(self.estimate) * 100.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def moe_to_se(moe: ArrayLike) -> np.ndarray:
    """Convert published ACS 90% MOEs to standard errors."""
    return np.abs(np.asarray(moe, dtype=float)) / Z90


def se_to_moe(se: ArrayLike, z: float = Z90) -> np.ndarray:
    return np.asarray(se, dtype=float) * z


def replicate_variance(estimate: ArrayLike, replicates: ArrayLike) -> np.ndarray:
    """Successive-difference replicate variance.

    ``replicates`` has the 80 replicate values on its last axis; ``estimate``
    broadcasts against the leading axes. Returns the variance array.
    """
    reps = np.asarray(replicates, dtype=float)
    if reps.shape[-1] != REPLICATE_COUNT:
        raise ValueError(f"expected {REPLICATE_COUNT} replicates on the last axis, got {reps.shape[-1]}")
    est = np.asarray(estimate, dtype=float)[..., None]
    return REPLICATE_FACTOR * np.sum((reps - est) ** 2, axis=-1)


def replicate_se(estimate: ArrayLike, replicates: ArrayLike) -> np.ndarray:
    return np.sqrt(replicate_variance(estimate, replicates))


def replicate_moe(estimate: ArrayLike, replicates: ArrayLike, z: float = Z90) -> np.ndarray:
    return z * replicate_se(estimate, replicates)


def derived_from_replicates(
    estimate: float,
    replicates: Sequence[float],
    method: str = METHOD_REPLICATE,
    notes: Optional[List[str]] = None,
) -> DerivedEstimate:
    """Wrap a scalar estimate and its 80 replicate values as a DerivedEstimate."""
    se = float(replicate_se(estimate, replicates))
    return DerivedEstimate(
        estimate=float(estimate),
        moe=se * Z90,
        se=se,
        method=method,
        notes=list(notes or []),
    )
