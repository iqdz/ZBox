"""
Assistive-technology helpers for ZBox.

Also holds the two presentation helpers that keep ZBox usable for
low-vision readers rather than blind ones: a background colour that
follows the system theme instead of forcing white, and text and
dialog sizing that follows the system font instead of a pixel count.

Screen readers like NVDA and JAWS announce a read-only wx.TextCtrl
as "read only edit" when it receives focus. In a message viewer the
text is meant to be read, never edited, so that announcement is
confusing noise. Presenting the control to assistive technologies as
static text instead makes the reader simply speak the content with no
"read only edit" tag attached.
"""

import wx

import lang


class ReadOnlyTextAsStatic(wx.Accessible):
    """
    Accessible object that reports a read-only text control as static
    text. The control keeps all of its real behavior for sighted
    users; only the accessibility role is re-labeled.
    """

    def __init__(self, window):
        super().__init__(window)

    def GetRole(self, childId):
        # wxPython Phoenix requires (status, role), not a bare role
        # constant -- returning just the constant silently fails and
        # wx falls back to the control's real role (a read-only edit
        # field), which is the exact "read only edit" announcement
        # this class exists to suppress.
        return (wx.ACC_OK, wx.ROLE_SYSTEM_STATICTEXT)


def make_read_only_viewer(control):
    """Attaches the static-text accessibility role to a read-only
    text control so screen readers don't announce it as an editable
    field."""
    control.SetAccessible(ReadOnlyTextAsStatic(control))
    return control


class SilentPane(wx.Accessible):
    """
    Accessible object that reports a window as unnamed static text
    rather than as a pane.

    For the one window that needs it: the message tab parks keyboard
    focus on itself for the few hundred milliseconds between the tab
    opening and the HTML document being ready to focus (see
    MessageViewPanel._park_focus_while_loading). A wx.Panel's default
    accessible role is a pane, so NVDA/JAWS announced a bare "panel"
    in that gap -- quieter than the "read only edit" it replaced, but
    still a word spoken before the message. An unnamed static text
    has neither a name nor a role worth speaking.

    Safe here in a way it is not on a wx.html2.WebView: wx installs
    its own wxAccessible on the window's HWND and wxAccessible
    enumerates *wx* children, which for an ordinary panel is exactly
    right -- its children really are wx windows. On the WebView the
    rendered document is not, and overriding there once made the
    message unreadable.
    """

    def __init__(self, window):
        super().__init__(window)

    def GetName(self, childId):
        # Phoenix wants (status, value), never a bare value -- see
        # ReadOnlyTextAsStatic.GetRole for what silently returning the
        # wrong shape costs.
        if childId == 0:
            return (wx.ACC_OK, "")
        return (wx.ACC_NOT_IMPLEMENTED, "")

    def GetRole(self, childId):
        if childId == 0:
            return (wx.ACC_OK, wx.ROLE_SYSTEM_STATICTEXT)
        return (wx.ACC_NOT_IMPLEMENTED, wx.ROLE_SYSTEM_CLIENT)


class ProtectedFieldName(wx.Accessible):
    """
    Names a password field so the reader says whether it is masked,
    in a word that means something.

    A password box announces its masked state as part of its name
    here rather than leaving it to whatever the screen reader
    derives from the control itself, which is where "checked" came
    from -- a word that says nothing about a password field and, on
    a page that also has real checkboxes, actively misleads. The
    name carries "protected" while the text is hidden and drops it
    the moment a Show password control reveals the text, so the
    announcement always describes what is actually on screen.
    """

    def __init__(self, window, label):
        super().__init__(window)
        self._label = label
        self.masked = True

    def GetName(self, childId):
        # Phoenix wants (status, value); see ReadOnlyTextAsStatic.
        if childId != 0:
            return (wx.ACC_NOT_IMPLEMENTED, "")
        if self.masked:
            return (
                wx.ACC_OK,
                lang.t(
                    "dialogs", "acc_protected",
                    default="%s, protected" % self._label,
                    label=self._label,
                ),
            )
        return (wx.ACC_OK, self._label)


def make_protected_field(control, label):
    """Attaches ProtectedFieldName to a password control and returns
    it, so the caller can flip .masked when the text is shown or
    hidden. The control's plain name is kept in step as well, for a
    build where SetAccessible does nothing."""
    accessible = ProtectedFieldName(control, label)
    try:
        control.SetAccessible(accessible)
    except Exception:  # noqa: BLE001
        pass
    control.SetName(
        lang.t(
            "dialogs", "acc_protected",
            default="%s, protected" % label, label=label,
        )
    )
    return accessible


def set_field_protected(control, accessible, label, masked):
    """Switches a password field between protected and shown, in both
    places the name can come from."""
    if accessible is not None:
        accessible.masked = masked
    control.SetName(
        lang.t(
            "dialogs", "acc_protected",
            default="%s, protected" % label, label=label,
        )
        if masked
        else label
    )


# --- Toggle controls ---------------------------------------------------
#
# Under the dark theme wxWidgets 3.3 owner-draws check boxes, radio
# buttons and toggle buttons, because a native one cannot take the
# theme's text colour. An owner-drawn button tells Windows it is a
# plain push button with no checked state, so NVDA and JAWS said
# "button" for Minimize to tray, Start with Windows and every other
# option, and nothing when Space changed it. ToggleState reports the
# real role and state; install_toggle_states attaches it to every such
# control the app creates. The name is left to Windows, so labels and
# mnemonics read exactly as before.

_OBJID_CLIENT = getattr(wx, "OBJID_CLIENT", -4)
_ACC_SELF = getattr(wx, "ACC_SELF", 0)
_EVENT_STATECHANGE = getattr(wx, "ACC_EVENT_OBJECT_STATECHANGE", 0x800A)
_STATE_CHECKED = getattr(wx, "ACC_STATE_SYSTEM_CHECKED", 0x10)
_STATE_MIXED = getattr(wx, "ACC_STATE_SYSTEM_MIXED", 0x20)
_STATE_PRESSED = getattr(wx, "ACC_STATE_SYSTEM_PRESSED", 0x8)
_STATE_FOCUSED = getattr(wx, "ACC_STATE_SYSTEM_FOCUSED", 0x4)
_STATE_FOCUSABLE = getattr(wx, "ACC_STATE_SYSTEM_FOCUSABLE", 0x100000)
_STATE_UNAVAILABLE = getattr(wx, "ACC_STATE_SYSTEM_UNAVAILABLE", 0x1)
_VALUE_STATES = _STATE_CHECKED | _STATE_MIXED | _STATE_PRESSED


def toggle_state_flags(window, kind):
    """The MSAA state of a check box ("check"), radio button
    ("radio") or toggle button ("toggle"), read from the control."""
    state = _STATE_FOCUSABLE
    try:
        if not window.IsEnabled():
            state |= _STATE_UNAVAILABLE
        if window.HasFocus():
            state |= _STATE_FOCUSED
        if (
            kind == "check"
            and window.Is3State()
            and window.Get3StateValue() == wx.CHK_UNDETERMINED
        ):
            state |= _STATE_MIXED
        elif window.GetValue():
            state |= _STATE_PRESSED if kind == "toggle" else _STATE_CHECKED
    except RuntimeError:
        # The control was destroyed while a reader was asking.
        pass
    return state


class ToggleState(wx.Accessible):
    """Reports a toggle control's real role and checked state."""

    _ROLES = {
        "check": wx.ROLE_SYSTEM_CHECKBUTTON,
        "radio": wx.ROLE_SYSTEM_RADIOBUTTON,
        "toggle": wx.ROLE_SYSTEM_PUSHBUTTON,
    }

    def __init__(self, window, kind):
        super().__init__(window)
        self._window = window
        self._kind = kind

    def GetRole(self, childId):
        # Phoenix wants (status, value); see ReadOnlyTextAsStatic.
        if childId != 0:
            return (wx.ACC_NOT_IMPLEMENTED, 0)
        return (wx.ACC_OK, self._ROLES[self._kind])

    def GetState(self, childId):
        if childId != 0:
            return (wx.ACC_NOT_IMPLEMENTED, 0)
        return (wx.ACC_OK, toggle_state_flags(self._window, self._kind))


def _sync_toggle_state(window):
    """Tells screen readers a toggle's state changed, once per real
    change. Run after the event, so the control holds its new value."""
    if not window:
        # Destroyed before this ran (a dialog closed on the click).
        return
    if getattr(window, "_zbox_native_toggle", False):
        # A real Windows check box or radio button: Windows sends the
        # state change itself, and a second one would speak twice.
        return
    accessible = getattr(window, "_zbox_toggle_state", None)
    if accessible is None:
        return
    try:
        value = toggle_state_flags(window, accessible._kind) & _VALUE_STATES
    except Exception:  # noqa: BLE001 -- destroyed meanwhile
        return
    if value == getattr(window, "_zbox_toggle_value", None):
        return
    window._zbox_toggle_value = value
    try:
        wx.Accessible.NotifyEvent(_EVENT_STATECHANGE, window, _OBJID_CLIENT, _ACC_SELF)
    except Exception:  # noqa: BLE001 -- no accessibility support
        pass


def _queue_toggle_sync(event):
    event.Skip()
    window = event.GetEventObject()
    if window is not None:
        wx.CallAfter(_sync_toggle_state, window)


_GWL_STYLE = -16
_BS_TYPEMASK = 0xF
_BS_OWNERDRAW = 0xB
_BS_AUTOCHECKBOX = 0x3
_BS_AUTO3STATE = 0x6
_BS_AUTORADIOBUTTON = 0x9


def _make_native_toggle(window, kind):
    """
    Turns an owner-drawn check box or radio button back into a real
    Windows one, keeping its value. On focus a screen reader asks
    Windows for the button's style and checked state, and an
    owner-drawn button answers "button, no state" whatever the
    accessible object says. wx reads the style to decide how to get
    and set the value, so it follows the change. The dark visual style
    is removed from this one control, so Windows draws it with the
    colours the dark theme hands a label instead of black text. Returns
    whether it changed anything; never raises.
    """
    if kind not in ("check", "radio"):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        uxtheme = ctypes.WinDLL("uxtheme")
        uxtheme.SetWindowTheme.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]

        hwnd = window.GetHandle()
        style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
        if style & _BS_TYPEMASK != _BS_OWNERDRAW:
            return False
        three_state = kind == "check" and window.Is3State()
        value = window.Get3StateValue() if three_state else window.GetValue()
        if kind == "radio":
            native = _BS_AUTORADIOBUTTON
        else:
            native = _BS_AUTO3STATE if three_state else _BS_AUTOCHECKBOX
        user32.SetWindowLongW(hwnd, _GWL_STYLE, (style & ~_BS_TYPEMASK) | native)
        uxtheme.SetWindowTheme(hwnd, "", "")
        if three_state:
            window.Set3StateValue(value)
        else:
            window.SetValue(bool(value))
        window.Refresh()
        return True
    except Exception:  # noqa: BLE001 -- keeps the owner-drawn control
        return False


def make_toggle_accessible(window, kind):
    """
    Attaches ToggleState to one check box, radio button or toggle
    button. The state-change notice is driven by the control's own
    event and by key and mouse release as well, all skipped, so a
    handler elsewhere that does not Skip cannot silence it; a value
    that did not change sends nothing. Never raises.
    """
    if getattr(window, "_zbox_toggle_state", None) is not None:
        return window
    window._zbox_native_toggle = _make_native_toggle(window, kind)
    try:
        accessible = ToggleState(window, kind)
        window.SetAccessible(accessible)
    except Exception:  # noqa: BLE001 -- a build without accessibility
        return window
    window._zbox_toggle_state = accessible
    window._zbox_toggle_value = toggle_state_flags(window, kind) & _VALUE_STATES
    changed = {
        "check": wx.EVT_CHECKBOX,
        "radio": wx.EVT_RADIOBUTTON,
        "toggle": wx.EVT_TOGGLEBUTTON,
    }[kind]
    for binder in (changed, wx.EVT_KEY_UP, wx.EVT_LEFT_UP):
        window.Bind(binder, _queue_toggle_sync)
    return window


_TOGGLES_INSTALLED = False


def install_toggle_states():
    """
    Replaces wx.CheckBox, wx.RadioButton and wx.ToggleButton with
    subclasses that call make_toggle_accessible when created, so every
    dialog gets the right announcement with no per-dialog code. Call
    once, before the first window. Returns whether it installed.
    """
    global _TOGGLES_INSTALLED
    if _TOGGLES_INSTALLED:
        return False

    def subclass(base, kind):
        class Toggle(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if self.GetParent() is not None:
                    make_toggle_accessible(self, kind)

            def Create(self, *args, **kwargs):
                created = super().Create(*args, **kwargs)
                if created:
                    make_toggle_accessible(self, kind)
                return created

        Toggle.__name__ = base.__name__
        Toggle.__qualname__ = base.__name__
        return Toggle

    for name, kind in (("CheckBox", "check"), ("RadioButton", "radio"), ("ToggleButton", "toggle")):
        base = getattr(wx, name, None)
        if base is not None:
            setattr(wx, name, subclass(base, kind))
    _TOGGLES_INSTALLED = True
    return True


def make_silent_focus_target(window):
    """Makes a window announce nothing when it briefly takes focus.
    Best effort: a build without working accessibility support costs
    the announcement back, never an exception during construction."""
    try:
        window.SetAccessible(SilentPane(window))
    except Exception:  # noqa: BLE001
        pass
    return window


def content_background():
    """The system's own window background.

    ZBox used to paint its panes with a literal white
    wx.Colour(255, 255, 255). On a default theme that is invisible --
    it is the same white the system would have used. On Windows High
    Contrast it is the bug: the system supplies light text intended
    for a black window, and a forced white pane leaves a low-vision
    reader with near-invisible text in the one mode they chose
    precisely to be able to see.

    SYS_COLOUR_WINDOW is whatever the current theme says a content
    area should be -- white on a default theme, black under High
    Contrast Black, and correct under a dark theme too. Read at call
    time, not at import: the value is meaningless before wx.App
    exists.
    """
    return wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)


def apply_content_background(window):
    """Paints a pane as a content area, following the system theme."""
    window.SetBackgroundColour(content_background())
    return window


def wrap_text(control, characters=58):
    """Wraps a StaticText to a width measured in characters of its
    own font, rather than a fixed pixel count.

    Wrap(380) means 380 physical pixels however large the text is, so
    at 150 or 200 percent Windows text scaling the same sentence
    needs two or three times the width it is given and the dialog
    clips it. Measuring the font keeps the line length constant in
    characters, which is what the reader actually experiences, and
    lets the dialog grow with the text.
    """
    try:
        width = control.GetTextExtent("x" * characters).width
    except Exception:
        width = 380
    control.Wrap(max(width, 200))
    return control


def note_text(parent, label, characters=58):
    """A wrapped explanatory paragraph, sized to the system font."""
    control = wx.StaticText(parent, label=label)
    return wrap_text(control, characters)


def bind_close_accelerators(dialog, close_id=wx.ID_CLOSE):
    """
    Binds Escape, Ctrl+W and Ctrl+F4 to close a dialog -- so a
    read-only "page" dialog (About ZBox, Keyboard Shortcuts,
    Documentation, the license viewer) closes with the same keys
    that already close a message tab, for the same familiarity
    reasoning ZBox already doubles up hotkeys for elsewhere (e.g.
    compose_panel's Ctrl+Enter/Alt+S both sending).

    wx's own default Escape handling only fires for a control whose
    id is wx.ID_CANCEL; a dialog built around a plain "Close" button
    (wx.ID_CLOSE, as every dialog above uses) gets no such default,
    which is the gap this closes. close_id is what EndModal is
    called with -- wx.ID_CLOSE by default, matching the visible
    Close button these dialogs already have.

    Not for a dialog where Escape needs to mean something more
    specific than a plain close (see eula_dialog.EulaDialog's gate
    mode, which binds these three keys itself, to Disagree rather
    than a no-op close), or for a dialog that already closes
    correctly via a real wx.ID_CANCEL button -- this exists for the
    dialogs that had neither.
    """
    escape_id = wx.NewIdRef()
    ctrl_w_id = wx.NewIdRef()
    ctrl_f4_id = wx.NewIdRef()

    def _close(_event):
        dialog.EndModal(close_id)

    dialog.Bind(wx.EVT_MENU, _close, id=escape_id)
    dialog.Bind(wx.EVT_MENU, _close, id=ctrl_w_id)
    dialog.Bind(wx.EVT_MENU, _close, id=ctrl_f4_id)
    dialog.SetAcceleratorTable(wx.AcceleratorTable([
        (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, escape_id),
        (wx.ACCEL_CTRL, ord("W"), ctrl_w_id),
        (wx.ACCEL_CTRL, wx.WXK_F4, ctrl_f4_id),
    ]))


def _fit_scrolled_children(dialog, reserve=160, fallback=600):
    """Gives every scrolled panel directly on dialog a real size.

    A wx.ScrolledWindow does not report its content's height as its
    best size, so SetSizerAndFit made Settings open one or two rows
    tall. A keyboard user never noticed, because focus scrolls itself
    into view; a mouse user saw a single setting. FitInside makes the
    whole content the scrollable area, so the wheel and the scroll bar
    reach all of it, and the minimum height is the content's own,
    capped at the screen height less room for the title bar and the
    buttons. Never raises: a dialog must always open.
    """
    try:
        display = wx.Display(wx.Display.GetFromWindow(dialog))
        cap = display.GetClientArea().GetHeight() - reserve
    except Exception:
        cap = fallback
    if cap <= 0:
        cap = fallback
    for child in dialog.GetChildren():
        if not isinstance(child, wx.ScrolledWindow):
            continue
        inner = child.GetSizer()
        if inner is None:
            continue
        try:
            child.FitInside()
            content = inner.CalcMin()
            bar = max(wx.SystemSettings.GetMetric(wx.SYS_VSCROLL_X), 0)
            child.SetMinSize((content.width + bar, min(content.height, cap)))
        except Exception:
            continue


def fit_dialog(dialog, sizer, min_width_chars=52):
    """Sizes a dialog to its contents instead of to a pixel guess.

    Fixed sizes like size=(420, 900) assume one font size. Past about
    150 percent Windows text scaling the content outgrows them and
    the OK and Cancel buttons walk off the bottom of the screen,
    which is unreachable by keyboard as well as by mouse.

    Fit first, then clamp to what the display can actually show, so a
    dialog that genuinely needs more room than the screen has ends up
    screen-height (and scrollable, where its content is in a
    scrolled window) rather than partly off it.
    """
    _fit_scrolled_children(dialog)
    dialog.SetSizerAndFit(sizer)
    try:
        minimum = dialog.GetTextExtent("x" * min_width_chars).width
    except Exception:
        minimum = 0
    width, height = dialog.GetSize()
    width = max(width, minimum)

    try:
        display = wx.Display(wx.Display.GetFromWindow(dialog))
        area = display.GetClientArea()
        max_width, max_height = area.GetWidth(), area.GetHeight()
    except Exception:
        max_width, max_height = width, height

    dialog.SetSize(min(width, max_width), min(height, max_height))
    dialog.Centre()
    return dialog


# Standard buttons wx labels itself (CreateButtonSizer, a bare
# wx.Button(parent, wx.ID_OK)): wx's own English, since ZBox translates
# through lang rather than wx's catalogs. (id, lang key, English).
_STOCK_BUTTONS = (
    (wx.ID_OK, "stock_ok", "OK"),
    (wx.ID_CANCEL, "stock_cancel", "Cancel"),
    (wx.ID_YES, "stock_yes", "&Yes"),
    (wx.ID_NO, "stock_no", "&No"),
    (wx.ID_CLOSE, "stock_close", "&Close"),
    (wx.ID_APPLY, "stock_apply", "&Apply"),
    (wx.ID_HELP, "stock_help", "&Help"),
)


def _plain(label):
    return (label or "").replace("&", "").strip().lower()


def localize_stock_buttons(window):
    """Gives every standard button under window the chosen language's
    text. Only a button still showing wx's own stock label is touched,
    so one a dialog labelled itself ("Save Draft") keeps its words, and
    a second call changes nothing. English is left exactly as wx drew
    it. Never raises."""
    try:
        if lang.current_code() == lang.DEFAULT_CODE:
            return
        wanted = {}
        for button_id, key, english in _STOCK_BUTTONS:
            wanted[int(button_id)] = (key, english)
        pending = list(window.GetChildren())
        changed = False
        while pending:
            child = pending.pop()
            try:
                pending.extend(child.GetChildren())
            except Exception:
                pass
            if not isinstance(child, wx.Button):
                continue
            entry = wanted.get(child.GetId())
            if entry is None:
                continue
            key, english = entry
            stock = wx.GetStockLabel(child.GetId(), wx.STOCK_NOFLAGS)
            if _plain(child.GetLabel()) not in (_plain(stock), _plain(english)):
                continue
            child.SetLabel(lang.control_label(key, english))
            changed = True
        if changed:
            # A translated label can be wider than wx's English one.
            window.Layout()
    except Exception:  # noqa: BLE001 -- a label must never stop a dialog showing
        pass


_stock_labels_installed = False


def install_stock_labels():
    """Relabels the standard buttons of every ZBox dialog just before
    it is shown, modal or not. Installed once at startup; the language
    is read when each dialog shows, so it can be installed before
    lang.load runs."""
    global _stock_labels_installed
    if _stock_labels_installed:
        return
    original_modal = wx.Dialog.ShowModal
    original_show = wx.Dialog.Show

    def show_modal(self, *args, **kwargs):
        localize_stock_buttons(self)
        return original_modal(self, *args, **kwargs)

    def show(self, *args, **kwargs):
        if not args or args[0]:
            localize_stock_buttons(self)
        return original_show(self, *args, **kwargs)

    wx.Dialog.ShowModal = show_modal
    wx.Dialog.Show = show
    _stock_labels_installed = True
