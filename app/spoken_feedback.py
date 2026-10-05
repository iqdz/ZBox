"""
Whether ZBox speaks.

One switch for every spoken announcement: message actions, new mail,
who sent this, compose check boxes, sign-in steps, update notices and
the notices that replaced information-only popups (notice_toast).

Settings.announce_actions holds the user's choice: True or False once
it has been changed in Settings, None until then. None means
automatic: ZBox speaks while a screen reader is running and stays
silent otherwise, so a sighted user without one hears nothing and a
screen reader user needs no setup.

A screen reader counts as running when one of the speech library's
own reader outputs (NVDA, JAWS, Dolphin, System Access, PC Talker,
ZDSR) says it is active, or Windows' screen reader flag is set, which
also covers Narrator. The system voice output never counts: it is
always available. The answer is kept for two seconds, so a reader
started after ZBox is noticed almost at once.

Nothing here imports wx or the speech library at import time, so the
settings code and the tests can import it anywhere. Main thread only,
like announce.py, since the readers' COM objects belong to the thread
that made them.
"""

import logging
import time

logger = logging.getLogger("zbox.spoken_feedback")

_READER_MODULES = ("nvda", "jaws", "dolphin", "system_access", "pc_talker", "zdsr")
_CHECK_SECONDS = 2.0

_manager = None
_outputs = None  # None = not built yet
_last = [0.0, False]


def use(manager):
    """The settings manager whose settings decide. Called once by it."""
    global _manager
    _manager = manager


def _windows_flag():
    try:
        import ctypes

        flag = ctypes.c_int(0)
        if ctypes.windll.user32.SystemParametersInfoW(0x0046, 0, ctypes.byref(flag), 0):
            return bool(flag.value)
    except Exception:  # noqa: BLE001 - not Windows, or the call failed
        pass
    return False


def _reader_outputs():
    global _outputs
    if _outputs is not None:
        return _outputs
    _outputs = []
    for name in _READER_MODULES:
        try:
            import importlib

            module = importlib.import_module("accessible_output2.outputs." + name)
            output_class = getattr(module, "output_class", None)
            if output_class is not None:
                _outputs.append(output_class())
        except Exception:  # noqa: BLE001 - that reader's library is missing
            logger.debug("Reader output %s not available.", name, exc_info=True)
    return _outputs


def _reader_output_active():
    for output in _reader_outputs():
        try:
            if output.is_active():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def screen_reader_running():
    """True when a screen reader is running. Checked at most every two
    seconds."""
    now = time.monotonic()
    if _last[0] and now - _last[0] < _CHECK_SECONDS:
        return _last[1]
    running = _reader_output_active() or _windows_flag()
    _last[0], _last[1] = now, running
    return running


def enabled(settings=None):
    """Whether ZBox should speak now: the user's choice when there is
    one, otherwise whether a screen reader is running."""
    if settings is None and _manager is not None:
        settings = getattr(_manager, "settings", None)
    choice = getattr(settings, "announce_actions", None) if settings is not None else None
    if choice is None:
        return screen_reader_running()
    return bool(choice)


def speak(parent, text):
    """announce.speak behind the switch. False when speech is off, as
    when no reader took the text."""
    if not enabled():
        return False
    import announce

    return announce.speak(parent, text)
