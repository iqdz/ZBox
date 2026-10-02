"""
The page that holds a Trix message body: its markup, the key router,
the formatting calls, and the reporting that tells Python what the
editor is doing.

Data only. Every value here is a string of HTML or JavaScript handed
to a WebView by whatever wires this up; nothing in this module runs
anything, which is why the suite can check it without a display.

Why a key router exists at all, as measured in testing: a chord
pressed inside a WebView never
reaches wx, but the page does see it. So every ZBox command is caught
here, cancelled before Edge or Trix can act on it, and handed to
Python by name, which then runs the same handler its menu item runs.
One implementation per command rather than two.

Formatting is driven the same way rather than left to Trix's own
shortcuts. Trix was fully initialised in testing -- version 2.1.19,
toolbar with fourteen buttons, editor API reachable -- and Ctrl+B
still did nothing, so the key was being taken before Trix saw it.

Two keys are not winnable and are not used: Edge takes F7 for caret
browsing and Ctrl+Shift+I for developer tools.
"""

# --- diagnostics ----------------------------------------------------
#
# These deliberately do NOT use the bridge. Every one of them returns
# its answer as the value of the script itself, which wx hands back
# from RunScript, because the whole point of them is to be usable when
# the bridge is the thing that is broken. A diagnostic that reports
# through the channel under suspicion tells you nothing.

# What state is the page actually in. Answers the questions this
# session kept guessing at: did wx inject its script message handler,
# did the chord table load, is the key listener installed, did Trix
# start, and has anything thrown.
PROBE = """
(function(){
  var el = document.getElementById('trix');
  var out = {
    handler: (typeof window.zbox),
    handler_post: (window.zbox ? typeof window.zbox.post : 'none'),
    handler_postMessage:
      (window.zbox ? typeof window.zbox.postMessage : 'none'),
    send_defined: (typeof window.zbox_send),
    chords_defined: (typeof window.zbox_chords),
    chord_count: (window.zbox_chords ? window.zbox_chords.length : 0),
    listener: (window.zbox_listener_installed ? 'yes' : 'unknown'),
    editor: (el ? 'present' : 'missing'),
    editor_api: (el && el.editor ? 'yes' : 'no'),
    trix_version: (window.Trix ? Trix.version : 'not loaded'),
    toolbar_buttons:
      document.querySelectorAll('#body-toolbar button').length,
    hotkeys: (window.zbox_hotkeys ? 'on' : 'off'),
    last_error: (window.zbox_last_error || 'none'),
    document_length:
      (el && el.editor ? el.editor.getDocument().toString().length : -1)
  };
  return JSON.stringify(out);
})();
"""

# Turns the hotkeys diagnostic on or off in this page. While it is on
# the router reports every key combination it sees, as it sees it,
# over the one way bridge. Plain typing is never reported: only a key
# held with Ctrl or Alt, a function key, or Escape, so a message can
# never end up in a log the user sends out.
HOTKEYS_ON = "(function(){window.zbox_hotkeys=true;return 'on';})();"
HOTKEYS_OFF = "(function(){window.zbox_hotkeys=false;return 'off';})();"

# A round trip test of the bridge itself: sends one message and
# reports which channel it used, without needing the message to
# arrive for the answer to come back.
BRIDGE_TEST = """
(function(){
  var used = 'none';
  try{
    var b = window.zbox;
    if (b && typeof b.post === 'function') { used = 'post'; }
    else if (b && typeof b.postMessage === 'function') { used = 'postMessage'; }
    else { used = 'navigation fallback'; }
  }catch(e){ used = 'threw: ' + e; }
  window.zbox_send({kind:'diagnostic', text:'bridge test'});
  return used;
})();
"""

# The page's own styling. Forced-colors aware, with a visible focus
# outline: a low-vision writer needs to see where the caret is, and a
# high contrast theme must not be painted over.
PAGE_STYLE = """
  :root { color-scheme: light dark; }
  html, body { margin: 0; padding: 0; height: 100%; }
  body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 11pt;
         line-height: 1.4; }
  #body-label { display: block; padding: 4px 6px; }
  trix-editor { min-height: 12em; padding: 6px; }
  :focus { outline: 2px solid Highlight; outline-offset: 2px; }
  @media (forced-colors: active) {
    trix-editor { border: 1px solid CanvasText; }
  }
  zbox-miss { text-decoration: underline wavy red; }
"""

BODY = """
<label for="trix" id="body-label">Message body</label>
<trix-toolbar id="body-toolbar"></trix-toolbar>
<input id="trix-input" type="hidden" name="content">
<trix-editor id="trix" toolbar="body-toolbar" input="trix-input" spellcheck="false"
             aria-labelledby="body-label" aria-label="Message body"></trix-editor>
"""

BRIDGE = """
window.zbox_send = function(payload){
  var text;
  try{ text = JSON.stringify(payload); }catch(e){ return; }
  try{
    var b = window.zbox;
    if(b){
      if (typeof b.post === 'function') { b.post(text); return; }
      if (typeof b.postMessage === 'function') { b.postMessage(text); return; }
    }
  }catch(e){}
  // Second channel, and not a nicety. wx injects the script message
  // handler into the page itself, and that injection can lose the
  // race against a page that is already running -- when it does, the
  // handler object simply is not there, every chord is swallowed in
  // silence and the body becomes a keyboard trap with no error
  // anywhere. The reading view hit exactly this and answered it the
  // same way: navigate to a scheme Python vetoes, and read the
  // payload out of the URL. A vetoed navigation changes nothing on
  // the page, so this costs nothing when it is not needed.
  try{
    window.location.href = 'zbox://message/' + encodeURIComponent(text);
  }catch(e){}
};
"""

# name, ctrl, shift, alt, key.
CHORDS = """
window.zbox_chords = [
  ['send',            true,  false, false, 'Enter'],
  ['send',            false, false, true,  's'],
  ['save_draft',      true,  false, false, 's'],
  ['attach',          true,  true,  false, 'a'],
  ['insert_signature',true,  true,  false, 's'],
  ['word_count',      true,  true,  false, 'w'],
  ['paste_quotation', true,  true,  false, 'v'],
  ['spellcheck',      true,  true,  false, 'F7'],
  ['spellcheck_next', true,  true,  false, 'F9'],
  ['spellcheck_add',  true,  true,  false, 'F8'],
  ['hotkeys_diagnostic', true, true, false, 'F12'],
  ['preview',         true,  true,  false, 'p'],
  ['formatting',      true,  true,  false, 'k'],
  ['format_menu',     true,  true,  false, 'm'],
  ['fmt:bold',        true,  false, false, 'b'],
  ['fmt:italic',      true,  false, false, 'i'],
  ['fmt:strike',      true,  true,  false, 'x'],
  ['fmt:bullet',      true,  true,  false, '8'],
  ['fmt:number',      true,  true,  false, '7'],
  ['fmt:quote',       true,  true,  false, '9'],
  ['fmt:code',        true,  true,  false, '6'],
  ['fmt:heading1',    true,  false, true,  '1'],
  ['link',            true,  false, false, 'k'],
  // The way out. A WebView that bridges none of these is a keyboard
  // trap, which the reading view already had to fix once. Alt and
  // F10 are deliberately absent: once F6 has moved focus onto an
  // ordinary wx control the menu bar works natively again.
  ['cycle_panes',     false, false, false, 'F6'],
  ['close_tab',       false, false, false, 'Escape'],
  ['close_tab',       true,  false, false, 'w'],
  ['close_tab',       true,  false, false, 'F4'],
  ['next_tab',        true,  false, false, 'Tab'],
  ['previous_tab',    true,  true,  false, 'Tab'],
  // Quit. Bridged even though nothing in the composer owns it,
  // because a chord that works everywhere else in ZBox and dies
  // inside one control is worse than one that never existed: muscle
  // memory does not know where the WebView starts.
  ['quit',            true,  true,  false, 'q']
];

// Off unless something turns it on, and off in anything that ships.
// While it is off the page reports nothing at all.
window.zbox_hotkeys = false;

document.addEventListener('keydown', function(e){
  var key = e.key;
  var lowered = (key || '').length === 1 ? key.toLowerCase() : key;
  // With Shift held, 8 arrives as * and 7 as &, and that varies by
  // layout. e.code names the physical key, so a digit chord is
  // matched on the key itself rather than on whatever character the
  // layout produced.
  var code = e.code || '';
  var digit = code.indexOf('Digit') === 0 ? code.slice(5) : '';
  var parts = [];
  if (e.ctrlKey) { parts.push('Ctrl'); }
  if (e.shiftKey) { parts.push('Shift'); }
  if (e.altKey) { parts.push('Alt'); }
  parts.push(key + (code ? ' [' + code + ']' : ''));
  var described = parts.join('+');
  // Combinations only. A plain letter is message text and must never
  // reach a log the user sends out.
  var loggable = e.ctrlKey || e.altKey || key === 'Escape' ||
                 /^F\\d+$/.test(key || '');

  for (var i = 0; i < window.zbox_chords.length; i++){
    var c = window.zbox_chords[i];
    if (e.ctrlKey === c[1] && e.shiftKey === c[2] && e.altKey === c[3] &&
        (lowered === c[4] || (digit && digit === c[4]))){
      e.preventDefault();
      e.stopPropagation();
      if (window.zbox_hotkeys && loggable) {
        window.zbox_send({kind:'keylog', text: described + '  ->  ' + c[0]});
      }
      window.zbox_send({kind:'chord', name:c[0]});
      return;
    }
  }
  if (window.zbox_hotkeys && loggable) {
    window.zbox_send({kind:'keylog',
      text: described + '  ->  no chord matched, passed to the page'});
  }
}, true);

// Proof the listener above was actually reached, for the probe. A
// chord table that loaded and a listener that installed are two
// different facts and this session learned the hard way that they
// have to be checked separately.
window.zbox_listener_installed = true;

window.addEventListener('error', function(e){
  window.zbox_last_error = (e.message || 'error') + ' at ' +
    (e.filename || 'page') + ':' + (e.lineno || 0);
});
"""

SETUP = """
(function(){
  var el = document.getElementById('trix');

  // Spelling marks, made by ZBox rather than by Chromium. Chromium's
  // own spell check in this editor stops once a second WebView exists
  // in the process (session av), so the editor has spellcheck off and
  // Python marks unknown words as a Trix text attribute instead. The
  // attribute is drawn as a small custom tag that tells a screen
  // reader the text is a spelling error. trix_html drops the tag, so
  // it never leaves ZBox in a message, a draft or a signature.
  try{
    if (!customElements.get('zbox-miss')) {
      customElements.define('zbox-miss', class extends HTMLElement {
        connectedCallback(){ this.setAttribute('aria-invalid', 'spelling'); }
      });
    }
    Trix.config.textAttributes.misspelled = {tagName: 'zbox-miss', inheritable: false};
  }catch(err){
    window.zbox_last_error = 'spelling marks: ' + err;
  }

  var watched = ['bold','italic','strike','href','bullet','number',
                 'quote','code','heading1'];

  window.zbox_attrs = function(reason){
    var names = [];
    for (var i = 0; i < watched.length; i++){
      try { if (el.editor.attributeIsActive(watched[i])) { names.push(watched[i]); } }
      catch (err) { }
    }
    window.zbox_send({kind:'attrs', names:names, reason:reason});
  };

  window.zbox_link = function(url){
    try{
      if (url) { el.editor.activateAttribute('href', url); }
      else { el.editor.deactivateAttribute('href'); }
      window.zbox_attrs('change');
    }catch(err){
      window.zbox_send({kind:'error', text:'Trix refused the link: ' + err});
    }
    el.focus();
  };

  el.addEventListener('trix-attribute-change', function(){ window.zbox_attrs('change'); });

  // Per-block direction, independent of the interface language and
  // of the chrome around this editor: each block Trix produced (a
  // line, a quote, a list item, a heading) gets its own dir from its
  // own first strong character, the same rule unicode-bidi:plaintext
  // would apply -- except that property does nothing in Trident, so
  // this does it by hand instead, portable to both engines. Only
  // runs at all when the page itself opened right-to-left; an
  // English interface leaves every block exactly as the browser's
  // own default already had it.
  var RTL_CHAR = /[\u0591-\u07FF\uFB1D-\uFDFD\uFE70-\uFEFC]/;
  var LTR_CHAR = /[A-Za-z\u00C0-\u02FF\u0370-\u0590\u2C00-\uFB1C]/;
  window.zbox_apply_bidi = function(){
    if (document.documentElement.getAttribute('dir') !== 'rtl') { return; }
    var blocks = el.querySelectorAll('div, p, blockquote, pre, li, h1');
    for (var i = 0; i < blocks.length; i++){
      var text = blocks[i].textContent || '';
      var dir = 'ltr';
      for (var j = 0; j < text.length; j++){
        var ch = text.charAt(j);
        if (RTL_CHAR.test(ch)) { dir = 'rtl'; break; }
        if (LTR_CHAR.test(ch)) { break; }
      }
      if (blocks[i].getAttribute('dir') !== dir) {
        blocks[i].setAttribute('dir', dir);
      }
    }
  };

  // The mirror. Python cannot read this body synchronously, so the
  // page pushes what it holds after every change and Python keeps
  // the latest copy. That is what lets the unsaved-changes check and
  // the autosave tick stay as cheap as they were against an edit
  // box. Debounced, because Trix fires this per keystroke.
  // The document string rides along: it is what Trix counts positions
  // against, and what the spelling marks are computed from.
  window.zbox_mirror = function(reason){
    var doc = '';
    try { doc = el.editor.getDocument().toString(); } catch (err) { }
    window.zbox_send({
      kind: 'content',
      reason: reason || 'mirror',
      html: document.getElementById('trix-input').value,
      text: el.innerText,
      document: doc
    });
  };
  var mirrorTimer = null;
  el.addEventListener('trix-change', function(){
    window.zbox_apply_bidi();
    if (mirrorTimer) { clearTimeout(mirrorTimer); }
    mirrorTimer = setTimeout(window.zbox_mirror, 200);
  });

  // Attachments belong to the message's attachment list, not inside
  // the document, so there is one place a file can be attached.
  el.addEventListener('trix-file-accept', function(e){
    e.preventDefault();
    window.zbox_send({kind:'file_rejected'});
  });

  // The Applications key, Shift+F10 or a right click on a word ZBox
  // marked as misspelled opens ZBox's own suggestions instead of
  // Chromium's menu, whose spell check is off here and so offers none.
  // Decided from the ranges the last marking run applied, and only
  // while the document is still the one they were computed for. On
  // any other word the normal menu opens untouched. Off unless the
  // host has a menu to show (window.zbox_spell_menu_on).
  el.addEventListener('contextmenu', function(e){
    try{
      var m = window.zbox_miss;
      if (!window.zbox_spell_menu_on || !m) { return; }
      if (el.editor.getDocument().toString() !== m.doc) { return; }
      var s = el.editor.getSelectedRange();
      for (var i = 0; i < m.ranges.length; i++){
        var r = m.ranges[i];
        if (s[0] >= r[0] && s[0] <= r[1] && s[1] <= r[1]){
          e.preventDefault();
          e.stopPropagation();
          window.zbox_send({kind:'spell_menu', start:r[0], end:r[1]});
          return;
        }
      }
    }catch(err){ }
  }, true);

  window.zbox_send({
    kind: 'ready',
    version: (window.Trix && window.Trix.VERSION) ? window.Trix.VERSION : 'unknown',
    toolbar: !!document.querySelector('trix-toolbar'),
    toolbar_buttons: document.querySelectorAll('trix-toolbar button').length,
    api: !!(el && el.editor),
    custom_element: !!(window.customElements && customElements.get('trix-editor'))
  });

  window.zbox_apply_bidi();
  el.focus();
})();
"""

# Self-contained on purpose. Depending on a function defined during
# setup means a setup that half ran leaves formatting silently dead,
# which is exactly the failure that is hard to diagnose.
FORMAT = """
(function(){
  var el = document.getElementById('trix');
  if (!el || !el.editor) {
    window.zbox_send({kind:'error', text:'No Trix editor to format with.'});
    return;
  }
  try{
    if (el.editor.attributeIsActive(NAME)) { el.editor.deactivateAttribute(NAME); }
    else { el.editor.activateAttribute(NAME); }
    if (window.zbox_attrs) { window.zbox_attrs('change'); }
  }catch(err){
    window.zbox_send({kind:'error', text:'Trix refused ' + NAME + ': ' + err});
  }
  el.focus();
})();
"""

LINK = "window.zbox_link(URL);"
ASK_ATTRS = "window.zbox_attrs('ask');"

GET_CONTENT = (
    "window.zbox_send({kind:'content', html: "
    "document.getElementById('trix-input').value, "
    "text: document.getElementById('trix').innerText, "
    "reason: REASON});"
)

INSERT_HTML = (
    "(function(){var el=document.getElementById('trix');"
    "el.editor.insertHTML(__ZBOX_HTML__);el.focus();})();"
)

INSERT_TEXT = (
    "(function(){var el=document.getElementById('trix');"
    "el.editor.insertString(TEXT);el.focus();})();"
)

# The editor's own document string, which is what Trix counts its
# positions against. NOT innerText: the rendered text and the document
# text drift apart around lists and block boundaries, and a spell
# check walking one while correcting into the other would replace the
# wrong range.
GET_DOCUMENT = (
    "(function(){var el=document.getElementById('trix');"
    "if(!el||!el.editor){return '';}"
    "return el.editor.getDocument().toString();})();"
)

# Selects a range and leaves focus in the editor.
#
# This is what spell check now is on the Trix body: put the caret on
# the word, say it, and let Chromium's own context menu -- the
# Application key -- offer the suggestions. Chromium is already spell
# checking this text in C++ and answers instantly; what it will not do
# is tell anyone where the misspellings are, which is the one thing a
# screen reader user cannot see for themselves.
SELECT_RANGE = (
    "(function(){var el=document.getElementById('trix');"
    "el.focus();"
    "el.editor.setSelectedRange([START, END]);})();"
)

# Every correction in one go.
#
# One script rather than one per correction, and that is not an
# optimisation. Sent separately, Trix re-renders between them and each
# offset after the first is counted against a document that has
# already moved, so the corrections land in the wrong places. Applied
# in sequence inside a single script, the editor's state never changes
# underneath the list.
#
# Each entry is start, end, replacement, expressed against the document
# as it stood after the previous entry, which is exactly how the spell
# check dialog recorded them.
#
# The editor is focused first, before any selection is set. Trix will
# not place a selection into an editor that does not hold focus, and
# the spell check dialog took focus away from it to show itself.
# SELECT_RANGE has always focused first, which is why the caret walk
# could move while the corrections could not land.
#
# Every correction is guarded on its own. One range Trix refuses used
# to throw out of the loop, which left the rest of the corrections
# unmade, the mirror stale and the trailing report never sent -- a
# half-corrected message that then went quiet. Now a refused range is
# recorded and the run carries on, and the count that comes back is
# what actually happened rather than what was asked for.
REPLACE_MANY = (
    "(function(){var el=document.getElementById('trix');"
    "if(!el||!el.editor){"
    "return JSON.stringify({applied:0,failed:['no editor'],text:''});}"
    "el.focus();"
    "var edits=EDITS;var applied=0;var failed=[];"
    "for(var i=0;i<edits.length;i++){"
    "try{"
    "var len=el.editor.getDocument().toString().length;"
    "var a=Math.max(0,Math.min(edits[i][0],len));"
    "var b=Math.max(a,Math.min(edits[i][1],len));"
    "el.editor.setSelectedRange([a,b]);"
    "el.editor.insertString(edits[i][2]);"
    "applied++;"
    "}catch(err){failed.push(i+': '+err);}}"
    "if (window.zbox_mirror) { window.zbox_mirror(); }"
    "return JSON.stringify({applied:applied,failed:failed,"
    "text:el.editor.getDocument().toString()});})();"
)

# One correction, in place. Selecting the range and inserting over it
# is what leaves the formatting around it alone -- the alternative,
# reloading the whole body with corrected text, flattens every bold,
# link and list in the message (open item 47).
REPLACE_RANGE = (
    "(function(){var el=document.getElementById('trix');"
    "el.editor.setSelectedRange([START, END]);"
    "el.editor.insertString(TEXT);"
    "if (window.zbox_mirror) { window.zbox_mirror(); }})();"
)

# Replaces the whole body, for the quoted original and the signature
# a tab opens with. The caret goes to the start afterwards, which is
# where a reply is written, and the mirror is refreshed at once so
# the unsaved-changes check starts from what is actually there.
#
# The content goes in at HTML_ARG, not at a bare HTML: the caller
# substitutes with str.replace, which also hit the HTML inside
# loadHTML and insertHTML, broke the script, and so dropped every
# reply's quoted original (set_html ok=False, 25 September 2026).
HTML_ARG = "__ZBOX_HTML__"
#
# The mirror goes out as 'loaded', not 'mirror': it is what the body
# held before anyone typed, which is how the composer knows the body is
# untouched and may be rebuilt when From changes.
SET_HTML = (
    "(function(){var el=document.getElementById('trix');"
    "el.editor.loadHTML(__ZBOX_HTML__);"
    "el.editor.setSelectedRange([0, 0]);"
    "if (window.zbox_mirror) { window.zbox_mirror('loaded'); }})();"
)

# Replaces every spelling mark in one step: clears them all, then marks
# the given ranges. Only when the document is still exactly the text
# the ranges were computed from; otherwise the writer typed in the
# meantime, nothing is touched, and the next pause redoes it. The
# selection is not moved: the new document is swapped in underneath
# it.
MARK_EXPECTED = "__ZBOX_EXPECTED__"
MARK_RANGES = "__ZBOX_RANGES__"
MARK_APPLY = (
    "(function(){try{var el=document.getElementById('trix');"
    "if(!el||!el.editor){return 'no editor';}"
    "var c=el.editor.composition;var d=c.document;"
    "if(d.toString()!==__ZBOX_EXPECTED__){return 'stale';}"
    "var n=d.removeAttributeAtRange('misspelled',[0,d.toString().length]);"
    "var r=__ZBOX_RANGES__;"
    "window.zbox_miss={doc:d.toString(),ranges:r};"
    "for(var i=0;i<r.length;i++){n=n.addAttributeAtRange('misspelled',true,r[i]);}"
    "if(n.isEqualTo(d)){return 'unchanged';}"
    "c.setDocument(n);return 'marked';"
    "}catch(err){return 'error '+err;}})();"
)

FOCUS = "(function(){var el=document.getElementById('trix'); if(el){el.focus();}})();"

# Lets the page hand misspelled words to ZBox's suggestions menu.
SPELL_MENU_ON = "(function(){window.zbox_spell_menu_on=true;return 'on';})();"

# What a formatting change is called out loud. Nothing announces bold
# to a screen reader on its own: a sighted writer sees the text
# thicken, and that is the whole feedback loop.
ATTRIBUTE_LABELS = {
    "bold": "Bold",
    "italic": "Italic",
    "strike": "Strikethrough",
    "href": "Link",
    "bullet": "Bulleted list",
    "number": "Numbered list",
    "quote": "Quote",
    "code": "Code",
    "heading1": "Heading",
}

# label, attribute, accelerator. One list, so a Format menu and the
# chord table cannot drift apart.
FORMAT_COMMANDS = (
    ("&Bold", "bold", "Ctrl+B"),
    ("&Italic", "italic", "Ctrl+I"),
    ("Stri&kethrough", "strike", "Ctrl+Shift+X"),
    ("Bulleted &list", "bullet", "Ctrl+Shift+8"),
    ("&Numbered list", "number", "Ctrl+Shift+7"),
    ("&Quote", "quote", "Ctrl+Shift+9"),
    ("C&ode", "code", "Ctrl+Shift+6"),
    ("&Heading", "heading1", "Ctrl+Alt+1"),
)


def document_html(trix_css, trix_js, is_rtl=False):
    """
    The whole page, as one string.

    Everything is inlined because a page handed to a WebView with no
    base URL has nowhere to resolve a stylesheet or a script from.
    Order matters: the editor element exists in the markup first, then
    Trix defines the custom element, and only then does the setup
    reach for el.editor.

    is_rtl sets dir="rtl" on the page itself -- ZBox's own chrome
    here, the toolbar and the "Message body" label. It is also what
    zbox_apply_bidi (in SETUP) reads back from the page to decide
    whether to run at all; the body's own per-block direction is
    never set from this directly, see that function's own comment.
    """
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"%s><head><meta charset="utf-8">'
        "<title>Message body</title>"
        "<style>%s</style><style>%s</style></head>"
        "<body>%s"
        "<script>%s</script>"
        "<script>%s</script>"
        "<script>%s</script>"
        "<script>%s</script>"
        "</body></html>"
    ) % (
        ' dir="rtl"' if is_rtl else "",
        trix_css or "", PAGE_STYLE, BODY, BRIDGE, CHORDS, trix_js or "", SETUP,
    )
