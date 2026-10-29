"""Copies package: arena waves and the approach pool."""

from copies.approaches import APPROACH_NAMES, APPROACHES, run_approach
from copies.arena import compare, judge, run_arena

__all__ = ["run_arena", "compare", "judge",
           "APPROACH_NAMES", "APPROACHES", "run_approach"]
