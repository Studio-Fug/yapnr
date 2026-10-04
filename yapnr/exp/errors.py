"""The errors ``yapnr exp`` reports as a one-line message (exit 2) instead of a traceback."""

from __future__ import annotations

from yapnr.exp.backends.base import SubmitError
from yapnr.exp.bundle import BundleError
from yapnr.exp.cloud import CloudError
from yapnr.exp.config import ConfigError
from yapnr.exp.cost import CostError
from yapnr.exp.fetch import FetchError
from yapnr.exp.image import ImageError
from yapnr.exp.plan import PlanError
from yapnr.exp.prices import PriceError
from yapnr.exp.spec import SpecError

ERRORS = (
    BundleError,
    CloudError,
    ConfigError,
    CostError,
    FetchError,
    ImageError,
    PlanError,
    PriceError,
    SpecError,
    SubmitError,
)
