"""Compatibility facade for the analysis pipeline.

The implementation lives under :mod:`geo_monitor.analysis.pipeline`; this module
keeps older imports such as ``geo_monitor.job_analysis`` working.
"""

from .analysis.pipeline import *  # noqa: F401,F403
