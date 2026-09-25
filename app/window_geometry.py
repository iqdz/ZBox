"""
Pure geometry helpers for window-state persistence (audit finding 52).

Kept free of wx entirely -- like envelope_format.py's ordering
helpers, filter_rules.py's matching engine and undo_manager.py's
record-keeping -- so the "should a saved position actually be
restored" decision can be unit-tested without a real windowing
system. main_frame.py's _restore_window_state/_save_window_state are
thin wrappers that call SetSize/SetPosition/Maximize/IsMaximized with
what these return.
"""


def rect_is_visible(x, y, width, height, display_rects, min_visible_px=50):
    """True if at least a min_visible_px x min_visible_px corner of
    the (x, y, width, height) rectangle overlaps one of the given
    display rectangles (each a (dx, dy, dwidth, dheight) tuple).

    Guards against restoring a window to a position that was valid on
    a monitor arrangement that no longer exists -- an external
    monitor unplugged, a resolution change, a laptop undocked -- which
    would otherwise reopen ZBox completely off-screen and unreachable
    by mouse. That matters for a sighted or low-vision ZBox user just
    as much as for anyone else; nothing about this app teaches a
    Windows off-screen-window recovery trick (Alt+Space, M, arrow
    key), so an unreachable window is effectively a broken launch.
    """
    if width <= 0 or height <= 0:
        return False
    for dx, dy, dwidth, dheight in display_rects:
        overlap_w = min(x + width, dx + dwidth) - max(x, dx)
        overlap_h = min(y + height, dy + dheight) - max(y, dy)
        if overlap_w >= min_visible_px and overlap_h >= min_visible_px:
            return True
    return False


def resolve_window_geometry(
    window_x, window_y, window_width, window_height, window_maximized,
    display_rects, default_width=1100, default_height=700,
):
    """Decides the geometry to apply on launch from saved settings
    plus the monitors currently attached.

    Returns (width, height, x, y, maximized). width/height always
    come back concrete (falling back to the given defaults when
    nothing valid was saved). x and y come back None when no saved
    position should be applied -- never saved yet, or the saved
    position is no longer reachable on any attached display (see
    rect_is_visible) -- in which case the caller leaves the frame at
    whatever position it already has (ZBox centers it, its long-
    standing default for a fresh install).
    """
    width = window_width if window_width and window_width > 0 else default_width
    height = window_height if window_height and window_height > 0 else default_height
    x, y = None, None
    if window_x is not None and window_y is not None:
        if rect_is_visible(window_x, window_y, width, height, display_rects):
            x, y = window_x, window_y
    return width, height, x, y, bool(window_maximized)
