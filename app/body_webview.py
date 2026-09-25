"""
ZBox's own WebView host for an editable page.

The reading side of the app has spent months getting wx.html2.WebView
to behave with a screen reader, and every mechanism that took to get
right is repeated here rather than shared: the forced accessibility
tree, the guarded script runner, the tracked timers, the deterministic
teardown, the navigation veto. Extracting a common base would mean
editing app/message_view_panel.py, which is the most expensive file in
this project to have got working, in order to serve a composer that
does not exist yet. The duplication is deliberate and is the cheaper
risk; unifying the two is its own later job, with the suite and a live
screen reader read to prove it.

What this file is NOT is the reading path. There is no content anchor,
no blank-spacer cleanup, no reading zoom, no focus delay and no
silence hold, because all five exist to make somebody else's HTML
readable. An editor's page is ours, it is loaded once, and focus
belongs in the editor rather than at the start of the text.

Nothing about the message body is decided here. This class knows how
to hold a page, run script against it safely, carry messages in both
directions and shut down cleanly; app/compose_body.py decides what the
page contains.
"""

import json
import logging
import os
import urllib.parse

import wx

logger = logging.getLogger("zbox.bodywebview")

try:
    import wx.html2
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False


def ensure_forced_accessibility_args():
    """
    Makes sure WebView2 launches its browser process with
    --force-renderer-accessibility.

    main.py sets this at startup for the whole app
    (ensure_webview2_accessibility_args) and message_view_panel repeats
    it before creating a reading WebView. This is the same idempotent
    guard for the composer: the switch is only appended when it is not
    already there, and it has to be in place before the first WebView
    in the process is constructed, because that is when the WebView2
    environment is created and the flags are read.
    """
    flag = "--force-renderer-accessibility"
    var = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    args = os.environ.get(var, "").split()
    if flag not in args:
        args.append(flag)
        os.environ[var] = " ".join(args)


class BodyWebView:
    """
    One WebView holding one editable page.

    on_message is called with the decoded payload of anything the page
    posts through the script-message bridge. on_link is called with a
    URL the page tried to navigate to, so the host decides where it
    goes; nothing ever navigates this control away from the editor.
    on_ready is called once, the first time the page reports itself
    loaded.

    using_webview is False when the control could not be created at
    all, which is what app/compose_body.py turns into the markup
    fallback.
    """

    def __init__(self, parent, on_message=None, on_link=None, on_ready=None,
                 name="Message body", handler_name="zbox"):
        self.view = None
        self._on_message = on_message
        self._on_link = on_link
        self._on_ready = on_ready
        self._handler_name = handler_name
        self._closing = False
        self._tearing_down = False
        self._loaded = False
        self._seen_message = False
        self._timers = []

        if not HAS_WEBVIEW:
            return

        try:
            ensure_forced_accessibility_args()
            self.view = wx.html2.WebView.New(parent)
            self.view.SetName(name)
            # No SetAccessible override. The reading panel installed
            # one once: wx puts a wxAccessible on the HWND, and that
            # enumerates wx children, so the rendered document left
            # the accessibility tree entirely and the content became
            # unreadable. The window class in the announcement is
            # cosmetic; the document is not.
            self.view.Bind(wx.html2.EVT_WEBVIEW_LOADED, self._on_loaded)
            self.view.Bind(wx.html2.EVT_WEBVIEW_NAVIGATING, self._on_navigating)
            # A link with target="_blank" raises no navigation event
            # at all -- WebView2 treats it as a new-window request --
            # so without this a pasted link would click into silence.
            new_window_event = getattr(wx.html2, "EVT_WEBVIEW_NEWWINDOW", None)
            if new_window_event is not None:
                self.view.Bind(new_window_event, self._on_new_window)
            self.view.Bind(
                wx.html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED,
                self._on_script_message,
            )
            # The return value matters and was being thrown away.
            # AddScriptMessageHandler reports whether the backend
            # accepted the handler at all, and when it refuses, the
            # page's window.zbox never exists: every chord is swallowed
            # in silence with nothing logged anywhere. If this says
            # False, the navigation fallback in trix_page's BRIDGE is
            # carrying the whole composer.
            added = self.view.AddScriptMessageHandler(handler_name)
            if added:
                logger.info(
                    "Compose body: script message handler %r registered.",
                    handler_name,
                )
            else:
                logger.warning(
                    "Compose body: the backend REFUSED the script message "
                    "handler %r. The page will fall back to navigation "
                    "messages for everything it sends.",
                    handler_name,
                )
        except Exception:
            logger.exception("The compose body WebView could not be created.")
            self._destroy_view()
            self.view = None

    # --- what the host asks it ----------------------------------------

    @property
    def control(self):
        return self.view

    @property
    def using_webview(self):
        return self.view is not None

    @property
    def loaded(self):
        return self._loaded

    def set_document(self, page_html):
        """Loads the page. SetPage with no base URL, which is why
        everything the page needs is inlined into it."""
        if self._closing or self.view is None:
            return False
        try:
            self.view.SetPage(page_html, "")
        except RuntimeError as exc:
            logger.debug("set_document: RuntimeError %r", exc)
            return False
        logger.info("Compose body: page set, %d bytes.", len(page_html))
        return True

    def run_script(self, script, why):
        """
        The single entry point for every RunScript against this page.

        wx.LogNull is the part that matters and the part a try/except
        cannot do: the native "Error running JavaScript" box is a
        wxLogError collected by wxLogGui inside wxWidgets, not a Python
        exception, so suspending the log target is the only way to stop
        it appearing. The closing guard stops the call being made at
        all once teardown has begun, and the success flag the API
        returns is logged rather than discarded.

        Returns True only when the script actually ran.
        """
        return self.run_script_result(script, why)[0]

    def run_script_result(self, script, why):
        """
        The same call, handing back what the script evaluated to.

        This is the channel diagnostics use, and the reason they can be
        trusted: it does not touch the message bridge at all, so it
        keeps working when the bridge is the thing that is broken.

        Returns (ok, output).
        """
        if self._closing or self.view is None:
            logger.debug("run_script: %s skipped (closing or no webview)", why)
            return False, None
        try:
            with wx.LogNull():
                result = self.view.RunScript(script)
        except RuntimeError as exc:
            logger.debug("run_script: %s RuntimeError %r", why, exc)
            return False, None
        except Exception as exc:
            logger.debug("run_script: %s failed %r", why, exc)
            return False, None
        output = None
        if isinstance(result, tuple):
            ok = bool(result[0]) if result else False
            if len(result) > 1:
                output = result[1]
        else:
            ok = bool(result)
        if not ok:
            logger.debug("run_script: %s ok=%r out=%r", why, ok, output)
        return ok, output

    def focus(self):
        """Win32 focus onto the control. Moving focus on into the
        editor element is the caller's job, and must happen after
        this, never before: placing DOM focus while the control has no
        native focus does not move a screen reader's cursor."""
        if self._closing or self.view is None:
            return False
        try:
            self.view.SetFocus()
        except RuntimeError:
            return False
        return True

    def enable(self, enabled):
        if self.view is None:
            return
        try:
            self.view.Enable(bool(enabled))
        except RuntimeError:
            pass

    def later(self, delay_ms, func, *args):
        """wx.CallLater whose handle is kept, so teardown can stop it.
        A timer that outlives its WebView fires against a control that
        is gone, or against a replacement with no document yet, which
        is the second of the two JavaScript error dialogs the reading
        panel had to learn about."""
        timer = wx.CallLater(delay_ms, func, *args)
        self._timers.append(timer)
        return timer

    def teardown(self):
        """Idempotent. Stops every pending timer, marks the view closing
        so a completed-load event fired during shutdown is ignored
        rather than run against a half-closed controller, then detaches
        and destroys the control.

        The detach is not optional and is not tidiness. Destroying a
        window its sizer still holds leaves the sizer carrying a
        pointer to a dead child, and the next layout -- which a tab
        close performs -- walks it. That is a process exit with no
        Python traceback, not an exception. message_view_panel's
        _teardown_webview detaches first for the same reason.

        The re-entry guard exists because compose_panel reaches this
        from three routes -- prepare_close, the send path's own
        DeletePage, and the panel's destroy handler -- and the last of
        those can fire while the first is still running.
        """
        if self._tearing_down:
            return
        self._tearing_down = True
        for timer in self._timers:
            try:
                timer.Stop()
            except RuntimeError:
                pass
        self._timers = []
        self._closing = True
        self._loaded = False
        self._destroy_view()
        self._tearing_down = False

    def _destroy_view(self):
        if self.view is None:
            return
        try:
            sizer = self.view.GetContainingSizer()
            if sizer is not None:
                sizer.Detach(self.view)
        except Exception:
            pass
        try:
            self.view.Destroy()
        except RuntimeError:
            pass
        self.view = None

    # --- what the page tells it ---------------------------------------

    def _on_loaded(self, event):
        url = event.GetURL() or ""
        # SetPage on the Edge backend goes through NavigateToString,
        # whose document URL is about:blank, not a data: URL. An
        # earlier version of the reading panel tested for data: here
        # and therefore discarded every single load event.
        internal = (not url) or url.startswith("data:") or url == "about:blank"
        if not internal or self._closing:
            return
        self._loaded = True
        logger.info("Compose body: page loaded (%s).", url or "no url")
        if self._on_ready is not None:
            self._on_ready()

    def _on_script_message(self, event):
        self._deliver(event.GetString())

    def _deliver(self, raw):
        """One decoder for both channels, so a message that arrived by
        navigation is indistinguishable from one that arrived through
        the script handler."""
        if not self._seen_message:
            self._seen_message = True
            logger.info("Compose body: first message received from the page.")
        if self._on_message is None:
            return
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("_deliver: undecodable payload %r", raw)
            return
        if not isinstance(payload, dict):
            return
        self._on_message(payload)

    # The page's second channel to Python, used when wx's script
    # message handler was not injected in time. See BRIDGE in
    # app/trix_page.py.
    MESSAGE_SCHEME = "zbox://message/"

    def _on_navigating(self, event):
        url = event.GetURL() or ""
        if url.startswith(self.MESSAGE_SCHEME):
            event.Veto()
            self._deliver(urllib.parse.unquote(url[len(self.MESSAGE_SCHEME):]))
            return
        if not url or url == "about:blank" or url.startswith("data:"):
            return
        event.Veto()
        if self._on_link is not None:
            self._on_link(url)

    def _on_new_window(self, event):
        url = event.GetURL() or ""
        if url and self._on_link is not None:
            self._on_link(url)
