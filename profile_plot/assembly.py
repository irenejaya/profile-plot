"""Assemble per-piece sampling results into one profile per raster.

numpy only (no QGIS), so the logic can be unit tested directly.
"""

import numpy as np

from . import sampling
from .sampling import OUTSIDE

PENDING_LEVEL = -2


class RasterProfile:
    """Full-resolution profile of one raster along the alignment.

    ``kind == "steps"``: intervals ``x0[i]..x1[i]`` with value ``v[i]``
    (native-cell mode).  ``kind == "points"``: stations ``x`` with values
    ``v`` (1 cm station mode); ``x1`` is ``None``.
    """

    __slots__ = ("kind", "x0", "x1", "v", "length", "preview", "pending",
                 "interpolated", "vmin", "vmax", "start_value", "end_value",
                 "has_gap", "coverage", "row", "col")

    def __init__(self, kind, x0, x1, v, length, preview=False, pending=False,
                 interpolated=False, row=None, col=None):
        self.kind = kind
        self.x0 = x0
        self.x1 = x1
        self.v = v
        self.length = float(length)
        self.preview = preview
        self.pending = pending
        self.interpolated = interpolated
        self.row = row
        self.col = col
        finite = np.isfinite(v)
        if finite.any():
            self.vmin = float(np.min(v[finite]))
            self.vmax = float(np.max(v[finite]))
        else:
            self.vmin = self.vmax = None
        self.has_gap = bool(v.size and not finite.all())
        if kind == "steps":
            self.start_value = float(v[0]) if v.size and x0[0] <= 1e-9 else float("nan")
            self.end_value = (float(v[-1]) if v.size and abs(x1[-1] - length) <= 1e-6
                              else float("nan"))
            total = float(np.sum(x1 - x0)) if v.size else 0.0
            covered = float(np.sum((x1 - x0)[finite])) if v.size else 0.0
            self.coverage = covered / total if total > 0 else 0.0
        else:
            self.start_value = float(v[0]) if v.size and x0[0] <= 1e-9 else float("nan")
            self.end_value = (float(v[-1]) if v.size and abs(x0[-1] - length) <= 1e-6
                              else float("nan"))
            self.coverage = float(finite.mean()) if v.size else 0.0

    @property
    def x(self):
        return self.x0

    def value_at(self, chainage):
        if self.kind == "steps":
            return sampling.value_at(self.x0, self.x1, self.v, chainage)
        return sampling.value_at_station(self.x0, self.v, chainage)

    def values_at(self, chainages):
        if self.kind == "steps":
            return sampling.step_values_at(self.x0, self.x1, self.v, chainages)
        idx = np.searchsorted(self.x0, chainages - 1e-9)
        idx = np.clip(idx, 0, max(self.x0.size - 1, 0))
        out = self.v[idx] if self.x0.size else np.full(chainages.shape, np.nan)
        ok = np.abs(self.x0[idx] - chainages) <= 1e-6 if self.x0.size else None
        if ok is not None:
            out = np.where(ok, out, np.nan)
        return out


class NativeAssembler:
    """Concatenates native-cell piece results.  The concatenation of the
    fixed prefix is cached so that tail updates only append."""

    def __init__(self):
        self._prefix_sig = None
        self._prefix = None

    @staticmethod
    def _part_arrays(ca, cb, res):
        if res is None:
            return (np.array([ca]), np.array([cb]),
                    np.array([OUTSIDE], dtype=np.int32),
                    np.array([OUTSIDE], dtype=np.int32),
                    np.array([np.nan]), np.array([PENDING_LEVEL], dtype=np.int16))
        n = res.ch0.size
        return (res.ch0, res.ch1, res.row, res.col, res.value,
                np.full(n, res.level, dtype=np.int16))

    def _concat(self, parts):
        arrays = [self._part_arrays(ca, cb, r) for ca, cb, r in parts if cb > ca]
        if not arrays:
            return None
        return tuple(np.concatenate(a) for a in zip(*arrays))

    def assemble(self, fixed_parts, tail_part, length):
        """``fixed_parts``: list of ``(ca, cb, result_or_None)``;
        ``tail_part``: same tuple or ``None``."""
        sig = tuple((ca, cb, id(r)) for ca, cb, r in fixed_parts)
        if sig != self._prefix_sig:
            self._prefix = self._concat(fixed_parts)
            self._prefix_sig = sig
        pieces = []
        if self._prefix is not None:
            pieces.append(self._prefix)
        if tail_part is not None and tail_part[1] > tail_part[0]:
            pieces.append(self._part_arrays(*tail_part))
        if not pieces:
            return None
        if len(pieces) == 1:
            ch0, ch1, rows, cols, vals, levels = pieces[0]
        else:
            ch0, ch1, rows, cols, vals, levels = (np.concatenate(a)
                                                  for a in zip(*pieces))
        # Merge identical cells across vertices, keep values aligned.
        n = ch0.size
        if n > 1:
            change = ((rows[1:] != rows[:-1]) | (cols[1:] != cols[:-1])
                      | (levels[1:] != levels[:-1]))
            both_out = (rows[1:] == OUTSIDE) & (rows[:-1] == OUTSIDE)
            change &= ~both_out
            if not change.all():
                starts = np.concatenate(([0], np.flatnonzero(change) + 1))
                ends = np.concatenate((starts[1:] - 1, [n - 1]))
                ch1 = ch1[ends]
                ch0 = ch0[starts]
                rows = rows[starts]
                cols = cols[starts]
                vals = vals[starts]
                levels = levels[starts]
        preview = bool(np.any(levels > 0))
        pending = bool(np.any(levels == PENDING_LEVEL))
        return RasterProfile("steps", ch0, ch1, vals, length, preview=preview,
                             pending=pending, row=rows, col=cols)


def assemble_stations(fixed_parts, tail_part, length, interpolated):
    """Concatenate station results.  The end station comes from the last
    piece (the tail while drawing)."""
    xs, vs = [], []
    preview = False
    pending = False
    parts = list(fixed_parts)
    if tail_part is not None and tail_part[1] > tail_part[0]:
        parts.append(tail_part)
    if not parts:
        return None
    for ca, cb, res in parts:
        if cb <= ca:
            continue
        if res is None:
            idx = sampling.station_indices(ca, cb)
            xs.append(idx * sampling.STATION_STEP)
            vs.append(np.full(idx.size, np.nan))
            pending = True
        else:
            xs.append(res.ch)
            vs.append(res.value)
            preview |= res.level > 0
    last = parts[-1][2]
    end_ch = parts[-1][1]
    end_v = last.end_value if last is not None else np.nan
    xs.append(np.array([end_ch]))
    vs.append(np.array([end_v]))
    x = np.concatenate(xs)
    v = np.concatenate(vs)
    return RasterProfile("points", x, None, v, length, preview=preview,
                         pending=pending, interpolated=interpolated)


# ---------------------------------------------------------------------------
# Grade and table helpers
# ---------------------------------------------------------------------------

class GradeInfo:
    __slots__ = ("grade", "dz", "length", "across_gap", "coverage", "preview",
                 "reason")

    def __init__(self, grade=None, dz=None, length=0.0, across_gap=False,
                 coverage=1.0, preview=False, reason=""):
        self.grade = grade
        self.dz = dz
        self.length = length
        self.across_gap = across_gap
        self.coverage = coverage
        self.preview = preview
        self.reason = reason


def grade_for(profile):
    """Signed net grade of a profile, start to end."""
    if profile is None:
        return GradeInfo(reason="unavailable")
    length = profile.length
    if length <= 1e-9:
        return GradeInfo(length=length, reason="unavailable")
    s, e = profile.start_value, profile.end_value
    g = sampling.endpoint_grade(s, e, length)
    if g is None:
        return GradeInfo(length=length, preview=profile.preview,
                         coverage=profile.coverage, reason="unavailable")
    return GradeInfo(grade=g, dz=e - s, length=length,
                     across_gap=profile.has_gap, coverage=profile.coverage,
                     preview=profile.preview or profile.pending)


class TableData:
    """Rows for the Table tab / CSV export, computed once per data change."""

    def __init__(self, profiles, names):
        self.names = names
        self.kind = None
        self.from_ch = np.empty(0)
        self.to_ch = None
        self.columns = []
        profiles = [p for p in profiles if p is not None]
        if not profiles:
            return
        if all(p.kind == "steps" for p in profiles):
            self.kind = "intervals"
            bounds = sampling.union_boundaries([p.x0 for p in profiles] +
                                               [p.x1 for p in profiles])
            if bounds.size < 2:
                return
            mids = 0.5 * (bounds[:-1] + bounds[1:])
            self.from_ch = bounds[:-1]
            self.to_ch = bounds[1:]
            self.columns = [p.values_at(mids) for p in profiles]
        else:
            self.kind = "stations"
            base = max(profiles, key=lambda p: p.x0.size)
            self.from_ch = base.x0
            self.columns = [p.values_at(base.x0) for p in profiles]

    @property
    def row_count(self):
        return int(self.from_ch.size)
