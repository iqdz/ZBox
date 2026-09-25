"""
The conversation tab: one whole thread in a single tab.

This replaces what Enter on a collapsed thread used to do. That
opened every message in the thread as its own tab -- twelve messages
meant twelve MessageViewPanels, and with the default view set to
HTML, twelve WebView2 (Edge) instances created back to back, each
loading asynchronously and each taking focus when it landed. A
screen reader read the first message and then went silent. The user
who reported it was on a 13th-gen CPU with 64 GB of RAM, which is
the tell that it was never a throughput problem: it was a dozen
browser engines competing for focus.

Thunderbird's own answer to a long thread is the same one this
implements -- Open Message in Conversation (Ctrl+Shift+O), built in
since well before 115, which puts the conversation in ONE tab rather
than spawning one per message. (Thunderbird has no mass-open-tabs
command at all; the one user report of it behaving that way was
handled as a fault.) ZBox binds the same key to the same idea.

The shape here is deliberately the one ZBox already knows how to
make accessible, rather than a scrolling stack of rendered messages:

  * an EnvelopeListPanel of the thread's messages, oldest first,
    with threading turned OFF -- it IS the thread, so grouping it
    again would be circular. A list gives the screen reader "3 of
    12", first-letter navigation and a stable focus position, none
    of which a long scrolling document gives you.
  * a ReaderPanel underneath showing the selected message's body as
    plain text, the same control the Mail tab's own preview uses.

So: no WebView is created anywhere in this tab, no matter how many
messages the thread holds, and there is exactly one thing on screen
that can take focus. Enter on a row still opens that single message
in a full message tab, HTML view and all, for the one message you
actually want to look at.

Bodies load one at a time, on selection, cache-first. Since message
bodies now come off a held-open IMAP connection
(imap_body_fetch.py), a warm row paints with no I/O at all and a
cold one costs a fraction of a second rather than the 3-22 seconds
the old subprocess path measured. The same stale-result guard the
Mail tab uses applies here: a body that arrives after the user has
arrowed on is discarded, not painted.
"""

import logging

import wx

import lang
from accessible import apply_content_background
from envelope_format import _envelope_backend
from envelope_list_panel import EnvelopeListPanel
import himalaya_client
from reader_panel import ReaderPanel

logger = logging.getLogger("zbox.conversation")


class ConversationTabPanel(wx.Panel):
    def __init__(self, parent, main_frame, account, folder, envelopes):
        super().__init__(parent)
        apply_content_background(self)
        self.main_frame = main_frame
        self.account = account
        self.folder = folder
        self.envelopes = list(envelopes)
        # Bumped on every selection; a body that comes back for an
        # older one is dropped. Same guard, same reason, as
        # ZBoxMainFrame._body_request_id.
        self._body_request_id = 0

        count = len(self.envelopes)
        heading = wx.StaticText(
            self,
            label=(
                lang.t(
                    "dialogs", "conv_heading_one",
                    default="Conversation: 1 message",
                )
                if count == 1
                else lang.t(
                    "dialogs", "conv_heading_many",
                    default="Conversation: %d messages" % count,
                    count=count,
                )
            ),
        )
        heading.SetFont(heading.GetFont().Bold())

        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        self.list_panel = EnvelopeListPanel(
            splitter,
            context_menu_handler=lambda *a: None,
            selection_handler=self._on_selected,
            activation_handler=self._on_activated,
            # Threading off on purpose: this list IS one thread.
            enable_threading=False,
        )
        self.list_panel.populate(self.envelopes)
        self.reader = ReaderPanel(splitter)
        splitter.SplitHorizontally(self.list_panel, self.reader)
        splitter.SetMinimumPaneSize(80)
        self._splitter = splitter

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(heading, 0, wx.ALL, 6)
        sizer.Add(splitter, 1, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(sizer)

    def focus_default(self):
        """Lands focus on the message list, so the screen reader
        starts on 'message 1 of N' rather than on the tab itself."""
        self.list_panel.focus_top_message()

    # -- list callbacks ------------------------------------------------

    def _on_selected(self, envelope):
        message_id = envelope.get("id")
        if message_id is None:
            return
        backend = _envelope_backend(envelope)

        cached = himalaya_client.read_message_cached_only(
            self.main_frame.paths, self.account, message_id,
            folder=self.folder, backend=backend,
        )
        self._body_request_id += 1
        if cached is not None:
            self.reader.set_message(cached)
            return

        request_id = self._body_request_id
        self.reader.show_loading()

        def work():
            return himalaya_client.read_message(
                self.main_frame.paths, self.account, message_id,
                folder=self.folder, backend=backend,
            )

        def on_success(message):
            if request_id != self._body_request_id:
                return
            try:
                self.reader.set_message(message)
            except RuntimeError:
                logger.debug("Conversation tab closed before a body arrived.")

        def on_error(exc):
            if request_id != self._body_request_id:
                return
            try:
                self.reader.show_error(str(exc))
            except RuntimeError:
                logger.debug("Conversation tab closed before an error arrived.")

        self.main_frame.lanes.for_read(self.account.account_id, backend).submit(
            work, on_success, on_error,
        )

    def _on_activated(self, envelope):
        """Enter on a row: open that ONE message in a full message
        tab, with the HTML view available. One at a time is the whole
        point -- the pile-up this tab exists to prevent came from
        doing it for every message at once. The whole thread goes
        with it, so Ctrl+Shift+Page Down/Up flip through it from
        there without coming back here."""
        self.main_frame._open_conversation_message(
            self.account, self.folder, envelope,
            thread_envelopes=self.envelopes,
        )
