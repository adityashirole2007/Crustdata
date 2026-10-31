"""Priority = Distress x Exposure.

This is the file that makes Prahari a procurement product rather than a data toy.

A distressed vendor you buy Rs 5 lakh a year from is a log line. The sole-source
supplier of a critical casting at Rs 8 crore a year with a 120-day lead time is a
fire. The risk model cannot tell those apart — only the procurement ledger can.
"""
from __future__ import annotations

from .models import Vendor


def _piecewise(x: float, points: list[list[float]]) -> float:
    """Linear interpolation across (threshold, factor) breakpoints."""
    pts = sorted((float(a), float(b)) for a, b in points)
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def exposure_factor(vendor: Vendor, cfg: dict) -> float:
    """0..~1 scalar capturing how much this vendor failing would actually hurt."""
    ec = cfg["exposure"]
    base = _piecewise(vendor.open_po_value_inr or 0.0, ec["po_value_breakpoints"])
    if vendor.sole_source:
        base *= float(ec["sole_source_multiplier"])
    base *= _piecewise(float(vendor.lead_time_days or 0), ec["lead_time_days_breakpoints"])
    if vendor.critical_component:
        base *= float(ec["critical_component_multiplier"])
    return min(base, 1.0)


def priority(distress_value: float, vendor: Vendor, cfg: dict) -> tuple[str, float, float]:
    """Return (band, priority_score, exposure_factor).

    priority_score is on the same 0-100 scale as distress so the two are
    legible side by side in the queue.
    """
    ef = exposure_factor(vendor, cfg)
    score = distress_value * ef
    bands = cfg["exposure"]["priority_bands"]
    for name in sorted(bands, key=lambda k: bands[k], reverse=True):
        if score >= float(bands[name]):
            return name, score, ef
    return "LOG", score, ef


def explain(vendor: Vendor, cfg: dict) -> dict[str, object]:
    ec = cfg["exposure"]
    return {
        "open_po_value_inr": vendor.open_po_value_inr,
        "po_factor": round(_piecewise(vendor.open_po_value_inr or 0.0,
                                      ec["po_value_breakpoints"]), 3),
        "sole_source": vendor.sole_source,
        "sole_source_multiplier": ec["sole_source_multiplier"] if vendor.sole_source else 1.0,
        "lead_time_days": vendor.lead_time_days,
        "lead_time_multiplier": round(_piecewise(float(vendor.lead_time_days or 0),
                                                 ec["lead_time_days_breakpoints"]), 3),
        "critical_component": vendor.critical_component,
        "critical_multiplier": (ec["critical_component_multiplier"]
                                if vendor.critical_component else 1.0),
        "exposure_factor": round(exposure_factor(vendor, cfg), 3),
    }
