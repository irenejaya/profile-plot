"""Background sampling engine.

A small pool of worker threads pulls jobs from a priority queue so that
interactive work (the moving tail, overview previews) always runs before
bulk work.  Results are published on the GUI thread through a Qt signal.
Jobs carry a cancel flag; cancelled or superseded jobs never emit.
"""

import heapq
import itertools
import os
import threading
import time

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .readers import DatasetPool, ReadContext, TileCache, sample_piece

PRIORITY_INTERACTIVE = 0
PRIORITY_NORMAL = 1


class Job:
    """One raster x one piece x one sampling mode x one resolution level."""

    __slots__ = ("key", "spec", "piece", "mode", "level", "priority", "tag",
                 "coarse", "refine", "_cancel", "elapsed", "tiles_read")

    def __init__(self, key, spec, piece, mode, level=0,
                 priority=PRIORITY_NORMAL, tag=None, coarse=0.0, refine=False):
        self.key = key
        self.spec = spec
        self.piece = piece
        self.mode = mode
        self.level = level
        self.priority = priority
        self.tag = tag
        self.coarse = coarse      # > 0: quick preview spacing in metres
        self.refine = refine      # True: low-value refinement of a preview
        self._cancel = threading.Event()
        self.elapsed = 0.0
        self.tiles_read = 0

    def cancel(self):
        self._cancel.set()

    def is_cancelled(self):
        return self._cancel.is_set()


class SamplingEngine(QObject):
    """Runs :class:`Job` objects on worker threads."""

    # (job, result) where result is a NativeResult/StationResult or an
    # Exception instance.
    jobFinished = pyqtSignal(object, object)

    def __init__(self, workers=None, cache_bytes=None, parent=None):
        super().__init__(parent)
        if workers is None:
            workers = max(2, min(4, (os.cpu_count() or 2)))
        self.cache = TileCache() if cache_bytes is None else TileCache(cache_bytes)
        self.pool = DatasetPool()
        self._heap = []
        self._counter = itertools.count()
        self._cv = threading.Condition()
        self._stopping = False
        self._threads = []
        for i in range(workers):
            t = threading.Thread(target=self._worker, name="ProfilePlot-%d" % i,
                                 daemon=True)
            t.start()
            self._threads.append(t)

    # -- public API -------------------------------------------------------

    def submit(self, job):
        with self._cv:
            heapq.heappush(self._heap, (job.priority, next(self._counter), job))
            self._cv.notify()
        return job

    def run_sync(self, job):
        """Run a job on the calling thread (used by tests/benchmarks)."""
        return self._execute(job)

    def release_source(self, spec):
        """Forget cached tiles and handles of a raster."""
        if spec is None:
            return
        self.cache.drop(spec.key)
        if spec.kind == "gdal":
            self.pool.close(spec.path)

    def shutdown(self, timeout=2.0):
        with self._cv:
            self._stopping = True
            for _, _, job in self._heap:
                job.cancel()
            self._heap.clear()
            self._cv.notify_all()
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self.pool.close_all()
        self.cache.clear()

    # -- worker side ------------------------------------------------------

    def _execute(self, job):
        t0 = time.perf_counter()
        ctx = ReadContext(job.spec, self.cache, self.pool)
        try:
            result = sample_piece(ctx, job.piece, job.mode, job.level,
                                  job.is_cancelled, job.coarse)
        except Exception as exc:  # report to the GUI instead of dying
            result = exc
        finally:
            job.tiles_read = ctx.tiles_read
            ctx.close()
        job.elapsed = time.perf_counter() - t0
        return result

    def _worker(self):
        while True:
            with self._cv:
                while not self._heap and not self._stopping:
                    self._cv.wait()
                if self._stopping:
                    return
                _, _, job = heapq.heappop(self._heap)
            if job.is_cancelled():
                continue
            result = self._execute(job)
            if result is None or job.is_cancelled() or self._stopping:
                continue
            try:
                self.jobFinished.emit(job, result)
            except RuntimeError:
                # The QObject was deleted while we were working.
                return
