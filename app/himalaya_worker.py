"""
Background worker for Himalaya CLI calls. One daemon thread pulls
jobs off a queue and runs them; each job's result (or error) is
handed back to the caller on the main thread via wx.CallAfter, so
the UI thread never blocks on a subprocess call and never touches
wx widgets from a worker thread.

This is also where the future IMAP IDLE watcher hands off its "new
mail" signal: the watcher thread only posts a lightweight signal via
wx.CallAfter, and the actual fetch is queued here like any other job.
"""

import logging
import queue
import threading
import time

import wx

import himalaya_client

logger = logging.getLogger("zbox.worker")


class HimalayaWorker:
    def __init__(self, pause_while=None):
        # pause_while: zero-argument callable; while it returns True
        # this worker holds before starting its next job. The
        # background worker uses it to stand aside while the message
        # list is being arrowed through, so the key handling never
        # competes with background Python work. A job already
        # running is not interrupted.
        self._pause_while = pause_while
        self._queue = queue.Queue()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """
        Stops delivering results to the UI. Called from the frame's
        close handler, before the window is destroyed.

        Without it, quitting mid-fetch produced a burst of
        "Unhandled exception in a worker result callback" -- one per
        job still in flight or queued (25 of them in one real
        session's log). Every one was the same harmless thing: a
        himalaya_client.shutdown() kill surfacing as a job failure,
        whose on_error then tried to touch a status bar, notebook or
        dialog on a frame that was already being destroyed. Harmless
        individually, but they buried the log's genuine errors under
        two dozen tracebacks at the exact moment someone reading it
        is looking for what went wrong.

        The queued jobs themselves are left alone: the threads are
        daemons and the process is going away regardless.
        """
        self._stopped.set()

    def submit(self, work_fn, on_success, on_error=None, is_cancelled=None):
        """
        work_fn: zero-argument callable that does the blocking
                 Himalaya call and returns a result.
        on_success: called on the main thread with the result.
        on_error: called on the main thread with the exception, if
                  work_fn raised one. Optional.
        is_cancelled: zero-argument callable returning True when the
                  caller no longer wants this job. Checked once,
                  immediately before the job runs, alongside the
                  shutdown check. A job the caller has given up on --
                  a message open whose watchdog already timed out --
                  is dropped instead of spending a subprocess on a
                  result nobody will use. A job that has already
                  started is not interrupted. Optional.
        """
        self._queue.put((work_fn, on_success, on_error, is_cancelled))

    def _run(self):
        while True:
            work_fn, on_success, on_error, is_cancelled = self._queue.get()
            while self._pause_while is not None and not self._stopped.is_set() and self._paused():
                time.sleep(0.05)
            if self._stopped.is_set():
                # Quitting: nothing is left to receive a result, and
                # the widgets a callback would touch are being
                # destroyed. Drop the job rather than running it.
                self._queue.task_done()
                continue
            if is_cancelled is not None:
                try:
                    cancelled = bool(is_cancelled())
                except Exception:
                    # A broken check must not strand the job or kill
                    # this thread: run the job as though it were
                    # still wanted.
                    logger.exception("A job's is_cancelled check failed.")
                    cancelled = False
                if cancelled:
                    logger.debug(
                        "Job dropped before it ran: the caller cancelled it."
                    )
                    self._queue.task_done()
                    continue
            try:
                result = work_fn()
            except himalaya_client.HimalayaShuttingDown as exc:
                # The app is closing, and shutdown() killed the
                # subprocess this job was blocked on. Logged as a
                # failure it produced a full traceback per job still
                # in flight -- 25 of them in one real session -- at
                # the exact moment a log reader is looking for a real
                # error. Nothing is delivered: stop() has already run
                # by this point, and the widgets a callback would
                # touch are being destroyed.
                logger.debug("Himalaya job ended during shutdown: %s", exc)
            except himalaya_client.HimalayaBackgroundSkipped as exc:
                # Not a failure: the per-account gate stood this
                # background job down because interactive work for the
                # same account was running, and it will be retried on
                # a later poll. Logged at ERROR with a traceback it
                # accounted for six of the eight ERROR lines in a
                # normal session, which is exactly how a real failure
                # goes unnoticed.
                logger.debug("Background job stood down: %s", exc)
                if on_error is not None:
                    self._deliver(on_error, exc)
            except Exception as exc:  # noqa: BLE001, deliberately broad: any
                # Himalaya/subprocess failure must reach the UI, not
                # kill this worker thread.
                logger.exception("Himalaya job failed.")
                if on_error is not None:
                    self._deliver(on_error, exc)
            else:
                self._deliver(on_success, result)
            finally:
                self._queue.task_done()

    def _paused(self):
        try:
            return bool(self._pause_while())
        except Exception:
            # A broken check must never park this thread for good.
            logger.exception("A worker's pause check failed.")
            return False

    def _deliver(self, callback, value):
        """
        Hands one result to the main thread, unless stop() has been
        called since the job started -- which is the common case
        during a quit, where himalaya_client.shutdown() has just
        killed the subprocess this job was blocked on.
        """
        if self._stopped.is_set():
            return
        wx.CallAfter(_safe_callback, callback, value)


def _safe_callback(callback, value):
    """
    Runs an on_success/on_error callback with exception logging.
    Without this, an exception raised inside a wx.CallAfter target
    (e.g. a UI update that assumed the wrong data shape) can be
    silently swallowed by wx's internals depending on platform and
    build, leaving no trace anywhere, including this app's own log
    file, even though a real bug just happened. That exact class of
    silent failure previously hid a real problem end to end.
    """
    try:
        callback(value)
    except Exception:
        logger.exception("Unhandled exception in a worker result callback.")
