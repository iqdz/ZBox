# ZBox

Portable, snappy, secure, accessible email client for Windows.

**Testing beta. Windows only.** Not a finished release: things will
break, and that is what this build is for.

ZBox is a portable, snappy, secure and fully accessible email client.
It was created to give everyone who uses it, including blind and
low-vision users, a real, feature-packed alternative to the bloated
and sluggish options usually available. Accessibility is built in
from the start, not added later: no decorative chrome, every control
named, every important state also given as text, and the HTML message
view tamed so a screen reader gets the message and nothing else. It
is tested with NVDA and JAWS.

It has no speech of its own and never will. Announcements are handed
to whichever screen reader is running, which speaks them in your own
voice, rate and settings.

## [Keyboard shortcuts: jump to the full list](#keyboard-shortcuts)

## Contents

1. [Getting it](#getting-it)
2. [What's new](#whats-new)
3. [Features](#features)
4. [First run](#first-run)
5. [Linking an account](#linking-an-account)
6. [The master password, and moving the folder](#the-master-password-and-moving-the-folder)
7. [Private mode](#private-mode)
8. [HTML mail](#html-mail)
9. [Keyboard shortcuts](#keyboard-shortcuts)
10. [Interface languages](#interface-languages)
11. [Privacy policy](#privacy-policy)
12. [Reporting a bug](#reporting-a-bug)
13. [Licence](#licence)
14. [Credits and third-party components](#credits-and-third-party-components)

## Getting it

Download the zip from the [Releases](../../releases) page, unzip it
anywhere you like, and run `ZBox.exe`.

Source code and releases: https://github.com/iqdz/ZBox

There is no installer. The whole folder is the app: put it on a USB
stick, an external drive or your desktop, and it writes nothing
outside itself. Nothing is added to the registry, nothing is left
behind if you delete the folder.

## What's new

In this version:

1. 22 more interface languages, 50 in all. See Interface languages
   below.
2. Moving through the From list in a new message, reply or forward no
   longer speaks the signature, Cc and Bcc, or encryption notices. Those
   settings still follow the account or identity you choose.

<details>
<summary>Earlier: testing beta 26.10.05</summary>

1. OpenPGP. Read and write encrypted and signed mail, with the
   OpenPGP Key Manager in Tools for your keys and your contacts' keys.
2. S/MIME reading. Tools, S/MIME Certificate Manager imports your own
   certificate (a .p12 or .pfx file) and other people's certificates.
   Signed and encrypted S/MIME mail opens with its status above the
   message. Sending S/MIME mail comes later.
3. Private mode, for using ZBox from a USB drive on a computer that
   is not yours. See Private mode below.
4. Outlook.com, Hotmail, Live and MSN accounts work through Microsoft
   Graph, so IMAP no longer has to be turned on in Outlook.com.
5. A Search tab (Ctrl+Shift+F): words in every account or one folder,
   dates, unread, flagged and attachments, and every message action on
   the results.
6. Address suggestions while writing come only from people you wrote
   to and contacts you saved. Down Arrow in To, Cc or Bcc opens them.
7. Settings in seven categories in a list, with real values in place
   of sliders.
8. Spoken announcements follow your screen reader on their own, and
   information-only popups are now short notices that never take
   focus.
9. Sent, Drafts, Trash, Junk and Archive are found from your server's
   own markings, so folder names in any language work.
10. Ctrl+Z undoes a delete, archive or junk action from the message
    list, the search results, the folder tree or an open message, and
    in a text field only undoes typing.
11. Sort By and Columns for the message list.
12. Everything ZBox keeps now stays in its folder, the browser data of
    HTML mail included, and its own records are encrypted.
13. All of this in every interface language.

</details>

<details>
<summary>Earlier: testing beta 26.10.02</summary>

1. Microsoft sign-in for Outlook.com, Hotmail, Live, MSN and
   Microsoft 365. Sign in on Microsoft's own page in your browser, or
   with a code on any device. ZBox keeps only an encrypted sign-in
   token, never your Microsoft password.
2. Block This Sender, in the message list's context menu in an Inbox
   or Junk folder. That sender's messages move to Trash at once, and
   so does their new mail, including mail that arrived while ZBox was
   closed. For a mailing list post, only that member is blocked, never
   the whole list. Unblock This Sender, in Trash, takes it back.
3. Only the Inbox and the Archive keep an offline copy. Every other
   folder is read from the server with nothing kept on disk, and Sent,
   Drafts, Trash and Junk open at once from a list remembered while
   ZBox runs.
4. At startup the first message list comes straight from the server,
   so mail deleted or moved elsewhere while ZBox was closed does not
   show up, and focus stays on the top message until you move it.
5. A privacy policy, now printed in full below.

</details>

## Features

### Accessibility

- Accessible from the start for everyone who uses it, blind and
  low-vision users included. Tested with NVDA and JAWS.
- Announcements go through your own screen reader, on by default
  while one is running, with one switch for all speech.
- Information-only messages are short notices that never take focus.
- Single-key actions in the message list, which can be turned off.
- Tuning for HTML mail with a screen reader: how long the reader stays
  quiet when a message opens, and how long announcements stay.
- A Keyboard Shortcuts page in Help (F1), and the full list below.

### Accounts and sign-in

- IMAP and SMTP through the Himalaya mail client, with the servers
  filled in for known providers.
- Microsoft sign-in for Outlook.com, Hotmail, Live, MSN and
  Microsoft 365. Personal Microsoft accounts work through Microsoft
  Graph.
- Gmail and others with an app password.
- Sent, Drafts, Trash, Junk and Archive found from your server's own
  markings, in any language.
- Extra identities, each with its own signature, Reply-To address,
  Cc and Bcc, Drafts and Sent folders and outgoing server.
- Mute, pause or disable an account.

### Reading mail

- HTML mail through Microsoft Edge WebView2, prepared for screen
  readers, with a plain Text view always one key away (Ctrl+B).
- Threads, a conversation view, and related messages across folders.
- Watch Thread and Ignore Thread, with filters for watched and ignored
  threads.
- Unified folders that combine one folder type from every account.
- Sort By and Columns for the message list.
- Message headers above the message, a message font size and reading
  font of your own, and the sender announced with Ctrl+U.
- Save a message as a file, open saved .eml messages, save every
  attachment at once to a folder you choose, and view or copy the full
  message headers.

### Writing mail

- A formatted editor with spell check, signatures and draft autosave.
- Address suggestions only from people you wrote to and contacts you
  saved.
- Preview a message as it will be sent, and a word count.
- An address book with groups, vCard and CSV import and export.

### Finding and organizing

- A Search tab across every account, by words, dates, unread,
  flagged and attachments, with every message action on the results.
- Message filters, Block This Sender and Unblock This Sender.
- Move or copy messages by typing part of a folder name.
- Undo (Ctrl+Z) for delete, archive and junk actions.
- Subscribe to server folders, mark a folder read, empty Trash and
  Junk, or let ZBox clear old Trash and Junk after the number of days
  you choose.

### Security and privacy

- OpenPGP encryption and signing, key discovery from a mail provider
  and from keys.openpgp.org, publishing your key, Autocrypt headers,
  and your public key attached on request.
- S/MIME reading: signed and encrypted S/MIME mail, with your own
  certificate imported in the S/MIME Certificate Manager.
- Private mode for reading mail on a computer that is not yours.
- Remote content blocked by default, an ad and tracker blocklist, a
  phishing link check, and optional junk rules that never guess.
- App lock with a Windows Hello passkey, a FIDO2 security key or the
  master password.
- Passwords and sign-in tokens encrypted, and ZBox's own records
  encrypted too.

### Portable and practical

- One folder, no installer, nothing written outside it.
- An offline copy of the Inbox and the Archive.
- Updates found and installed by ZBox itself, put back automatically
  if a new version fails to start. Ask first, or download and install
  quietly.
- Export and import your accounts and settings. Passwords are never
  exported.
- Minimize or close to the system tray, start with Windows, stay
  maximized, and a desktop shortcut.
- Settings for how long cached messages are kept.
- Six interface themes, and sound themes you can add your own to.
- 50 interface languages, listed under Interface languages below.

## First run

1. A licence appears before anything else. Decline and ZBox exits
   and removes the empty folder it was about to create.
2. You are asked to set a master password. Do it, and write it down
   somewhere safe. See the warning below.
3. The New Account wizard opens.

## Linking an account

Type your email address and the wizard fills in the servers for
providers it knows. Every field stays editable, and any IMAP/SMTP
provider can be entered by hand.

### Gmail

Gmail refuses your normal Google password in a mail program. ZBox
signs in with a Google app password instead. To get one:

1. Turn on 2-Step Verification for your Google account. Open
   https://myaccount.google.com/security and, under how you sign in
   to Google, turn on 2-Step Verification. App passwords are not
   offered until it is on.
2. Open https://myaccount.google.com/apppasswords and sign in if
   asked.
3. Create an app password and name it ZBox. Google shows it once, as
   16 letters in groups of four. Copy it.
4. Remove the empty spaces from it, so it is 16 letters with no
   spaces.
5. In the ZBox New Account wizard, type your Gmail address, and paste
   the app password where the wizard asks for the password.
6. Press Test Connection, then Finish.

If the app passwords page says the setting is not available,
2-Step Verification is still off, or the account is a work, school
or Advanced Protection account that does not allow app passwords.

Changing your Google password cancels every app password. Create a
new one the same way, and enter it in Tools > Account and Identities
Settings, in the New password field. Help > About ZBox has a button
that opens Google's own app password instructions.

### Outlook.com, Hotmail, Live and Microsoft 365

These accounts sign in with your Microsoft account, not a password.
On the wizard's Login page, choose Microsoft account as the sign-in
method; it is chosen for you for Outlook.com, Hotmail and Live
addresses. Then sign in on Microsoft's own page in your browser, or
with a code on any device. The server settings are filled in for you.
Outlook.com, Hotmail, Live and MSN accounts work through Microsoft
Graph, so IMAP does not have to be turned on in Outlook.com.

Some work or school accounts need their administrator to approve
ZBox once before it can sign in. Gmail signs in with an app password,
as above.

### Other providers

Anything else with plain IMAP and SMTP should work: Yahoo, Fastmail,
your own server, a work account that still allows app passwords.

## The master password, and moving the folder

Read this part before you copy ZBox anywhere.

Your account passwords are encrypted on disk, and on this computer
Windows itself holds the key, which is why ZBox opens silently here.
That key does not travel. Copy the folder to another computer and
the master password is the only thing that can open your saved
passwords there.

So: set a master password on the computer you set ZBox up on,
**before** you move the folder. If you move it without one, every
account has to be signed in to its server again on the new machine.

It is never stored anywhere, so it cannot be recovered. Write it
down.

ZBox also recognises each computer by a fingerprint. Open the folder
on a machine it does not know and it locks and asks for the master
password, then remembers that machine.

## Private mode

For reading mail from a USB drive on a computer that is not yours.
Turn it on in Settings, App and Data Security, or press
Windows+Ctrl+Shift+P from any program. It needs a master password,
asks for it, and warns before it starts.

While it is on, ZBox reads your mail from the server and keeps it in
memory only. No offline copies, saved messages or debug logs are
kept. Opened attachments and the browser data of HTML mail are
deleted when ZBox closes. Closing also ends the programs ZBox
started, empties the clipboard, and stops trusting the computer,
unless private mode was turned on from it. If the drive is lost, only
encrypted files are left on it.

Limits: Windows itself keeps some records no program can remove, such
as its list of USB drives, the page file, and antivirus and network
logs. A program that was already open when it took an attachment
cannot be told apart from your other work, so it is not closed.

Advice: close every opened attachment yourself before closing ZBox,
above all those opened with Windows' own programs such as Notepad or
Windows Media Player. Their processes cannot always be ended without
extra permissions.

## HTML mail

Messages render through Microsoft Edge WebView2, which most Windows
machines already have. If HTML mail does not render, Settings has a
legacy engine option and a way to use a bundled runtime instead.
Text view (Ctrl+B toggles) always works regardless.

## Keyboard shortcuts

The same list is in ZBox under Help, Keyboard Shortcuts (F1).

<details>
<summary>Show all keyboard shortcuts</summary>

### General

- F6: move between panes.
- Ctrl+Tab and Ctrl+Shift+Tab: next and previous tab.
- Ctrl+W or Ctrl+F4: close the current tab.
- Escape: collapse an expanded thread in the message list; otherwise
  close the current tab, asking first if a message being written has
  unsaved changes.
- Application key or Shift+F10: context menu of the focused item.
- F1: Keyboard Shortcuts.
- Ctrl+Shift+Q: exit ZBox, even when Close to the system tray is on.
- Ctrl+Shift+Delete: clear the offline message cache (ZBox's own
  copies only, nothing on any server).
- Windows+Ctrl+Shift+P, from any program: open the Private mode
  switch.

### Mail

- Ctrl+N: new message.
- Ctrl+Shift+N: new email account.
- F5: get new mail for the current account.
- Shift+F5: get new mail for all accounts.
- Ctrl+Shift+J: jump to the Inbox message list.
- Ctrl+Y: go to the folder list.
- Enter on a closed account or folder in the folder list: open it,
  like Right Arrow.
- Ctrl+Shift+B: Address Book.
- Ctrl+Shift+F: Search tab.
- Ctrl+Shift+T: show related messages.

### The message list

- On a collapsed thread, Delete, Archive, Flag, Junk, Move and Copy
  act on the whole thread. Expand it first (Right Arrow) to act on
  one message.
- Ctrl+A: select all messages.
- Ctrl+Shift+L: load more messages.
- Delete: move to Trash.
- Shift+Delete: delete permanently.
- Ctrl+Z: undo the last Delete, Archive, Mark as Junk or Mark as Not
  Junk. Also works in the Search results, the folder tree and an
  open message.
- S: flag or unflag.
- A: archive.
- J: mark as junk.
- Shift+J: mark as not junk.
- W: Watch Thread on or off.
- K: Ignore Thread on or off.
- M: Move To menu of folders.
- Shift+M: move by typing part of a folder name.
- C: Copy To menu of folders.
- The single keys S, A, J, W, K, M, Shift+M and C are on by default
  and can be turned off in Settings.
- Enter or Space on a collapsed thread: open its newest unread
  message (the newest if all are read) in one tab.
- Right Arrow on a collapsed thread: show its messages without
  opening any.
- Left Arrow: collapse the thread the selection is in.
- Ctrl+Shift+O: open the message in conversation view.
- Star: expand all threads. Backslash: collapse all threads.

### An open message

- Ctrl+R: reply.
- Ctrl+Shift+R: reply all.
- Ctrl+L: forward.
- Ctrl+Delete or Ctrl+D: move to Trash. Shift+Delete: delete
  permanently.
- Alt+A: archive, only while the message body has focus.
- Ctrl+Shift+Page Down and Ctrl+Shift+Page Up: next and previous
  message in the thread, in the same tab.
- Ctrl+B: switch between Text and HTML view.
- Ctrl+P: print. Ctrl+Shift+P: print preview.
- Ctrl+S: save message as a file.
- Ctrl+U: announce sender name and address.
- Ctrl+Shift+U: announce the sender and copy the address.
- Ctrl+Shift+I: show remote content in this message.
- Ctrl+Shift+Equals, Ctrl+Shift+Minus, Ctrl+Shift+0: larger, smaller
  and reset message font size.

### Writing a message

- Ctrl+Enter or Alt+S: send.
- Ctrl+S: save draft.
- Ctrl+Shift+A: attach a file.
- Ctrl+Shift+S: insert the signature of the account in From.
- Ctrl+Shift+V: paste as quotation.
- Ctrl+Shift+W: word count.
- Ctrl+Shift+P: preview the message as it will be sent.
- Down Arrow in To, Cc or Bcc: open address suggestions. Enter or Tab
  inserts, Escape returns, Shift+Delete removes the address from the
  Address Book.
- Escape, Ctrl+W or Ctrl+F4: close, asking first if anything is
  unsaved.
- Alt+C: Compose menu, even from inside the message body.

### Formatting, while writing

- Ctrl+B: bold. Ctrl+I: italic. Ctrl+Shift+X: strikethrough.
- Ctrl+Shift+8: bulleted list. Ctrl+Shift+7: numbered list.
- Ctrl+Shift+9: quote. Ctrl+Shift+6: code. Ctrl+Alt+1: heading.
- Ctrl+K: insert a link.
- Ctrl+Shift+K: say what formatting is in force at the caret.
- Ctrl+Shift+M: Format menu, from anywhere, the message body
  included.

### Spelling, while writing

- Ctrl+Shift+F7: check spelling.
- Ctrl+Shift+F9: next misspelling.
- Ctrl+Shift+F8: add the word to the dictionary.

### Diagnostics

- Ctrl+Shift+F12: Hotkeys Diagnostic on or off. Writes each key
  pressed in a message being written to the debug log. Off by
  default.

</details>

## Interface languages

ZBox's interface is available in English and 49 translations: Afrikaans,
Amharic (አማርኛ), Arabic (العربية), Azerbaijani (Azərbaycanca), Bengali
(বাংলা), Burmese (မြန်မာ), Chinese Simplified (简体中文), Chinese
Traditional (繁體中文), Czech (Čeština), Danish (Dansk), Dutch (Nederlands),
French (Français), German (Deutsch), Greek (Ελληνικά), Gujarati
(ગુજરાતી), Hausa, Hindi (हिन्दी), Hungarian (Magyar), Indonesian (Bahasa
Indonesia), Italian (Italiano), Japanese (日本語), Kannada (ಕನ್ನಡ), Korean
(한국어), Malay (Bahasa Melayu), Malayalam (മലയാളം), Marathi (मराठी),
Norwegian Bokmal (Norsk bokmål), Norwegian Nynorsk (Norsk nynorsk),
Pashto (پښتو), Persian (فارسی), Polish (Polski), Portuguese Brazil
(Português (Brasil)), Portuguese Portugal (Português (Portugal)),
Romanian (Română), Russian (Русский), Somali (Soomaali), Spanish
(Español), Swahili (Kiswahili), Swedish (Svenska), Tamil (தமிழ்), Telugu
(తెలుగు), Thai (ไทย), Turkish (Türkçe), Ukrainian (Українська), Urdu
(اردو), Uzbek (O‘zbekcha), Vietnamese (Tiếng Việt), Yoruba (Yorùbá) and
Zulu (isiZulu).
Arabic, Pashto, Persian and Urdu are laid out right to left. Choose one
in Settings, General, Interface language.

The translations were made mainly with generative translation, not by
native translators, so they will have mistakes. Mail words such as
Inbox, Junk and Trash follow Thunderbird's own wording where it has one.
If you speak one of these languages, corrections are very welcome by
email: say which language, where the text appears, what it says now, and
what it should say.

## Privacy policy

The full ZBox privacy policy, word for word. The same text is in
[PRIVACY.md](PRIVACY.md).

Effective date: 1 October 2026

ZBox is a portable email client for Windows, made by Harith Alhamdani. This policy explains what ZBox does with your information. In short: ZBox collects nothing. It has no analytics, no telemetry, no advertising, no user accounts with the developer, and no servers of its own.

### 1. What stays on your computer

Everything ZBox keeps is stored inside the ZBox folder on your own computer or drive: your account settings, the copies of your mail, your contacts, your settings, and your saved passwords and Microsoft sign-in tokens.

Passwords and sign-in tokens are encrypted with Windows' own protection, and also with your master password if you set one. They never leave your computer, except to sign you in to your own mail provider.

The developer has no access to any of this, and none of it is sent anywhere.

### 2. Connections ZBox makes

ZBox connects to the internet only for the following purposes.

Your mail servers. To receive and send your mail, ZBox connects to the mail servers you set up, such as Gmail, Outlook.com or your own provider. What those providers do with your mail is covered by their own privacy policies.

Microsoft sign-in. For Outlook.com, Hotmail, Live and Microsoft 365 accounts, you sign in on Microsoft's own page, in your browser or with a code. ZBox receives a sign-in token from Microsoft and stores it encrypted on your computer. ZBox never sees your Microsoft password. Microsoft's privacy statement applies to the sign-in itself.

Update checks. ZBox can check its public GitHub repository for a newer version, and download it if you choose to install it. This is optional. You can turn automatic checks off and check by hand instead. GitHub receives only what any web request carries, such as your IP address. GitHub's privacy statement applies.

Blocklist updates. ZBox's tracker, ad and phishing protection uses public blocklists. When blocklist updates are on, ZBox downloads newer copies of these lists from their publishers. This is optional and can be turned off in Settings. Nothing about you or your mail is sent.

Remote content in messages. Images and other content stored on the internet are blocked by default. If you choose to load them, for one message or for a sender, they load from that sender's servers. This can let the sender know you opened the message.

Links you open. A link you choose to open is opened in your web browser.

### 3. Debug logging

Debug logging is off by default. You can turn it on for a while to help solve a problem.

The log records what ZBox does, not what your mail says. It never contains your passwords, your sign-in tokens or the text of your messages. Names, the part of each email address before the @, and subjects are replaced with placeholders before anything is written. The log can still contain technical details: folder names and message counts, the domain part of email addresses, ZBox's internal account and message numbers, dates and times, and file paths on your computer, which may include your Windows user name.

The log stays in the ZBox folder on your computer. ZBox never sends it anywhere. If you decide to share it with the developer, you send it yourself, by email, and you can read it first. The developer uses it only to solve your problem and deletes it once the problem is solved. You can delete the log yourself at any time.

### 4. Contacting the developer

If you email the developer, your message and address are used only to reply to you and to help with your request. They are not shared with anyone.

### 5. Removing your data

To remove an account, use Tools, Remove Account. This deletes its saved password or sign-in token and the copies of its mail on your computer.

To remove everything, delete the ZBox folder.

For a Microsoft account, you can also withdraw ZBox's access from the app permissions page of your Microsoft account.

### 6. Changes to this policy

Any change to this policy is published in ZBox's GitHub repository, with a new effective date.

### 7. Contact

Harith Alhamdani
harith@gvoice.org

## Reporting a bug

Email **harith@gvoice.org**. Not GitHub issues.

What helps:

1. What you did, what you expected, what happened instead.
2. Your screen reader and its version, and your Windows version.
3. Whether it happens every time or once.

If a debug log is asked for: Settings has a debug logging switch, and
each launch writes its own log in `data\logs`, named for the time it
started. That log carries no message content by design. Subjects
appear only as numbers, bodies only as the words "text block",
attachments only by kind, and no complete email addresses: not those
of ZBox's users, and not those of the people their messages are from
or to. Only the email provider's domain, such as gmail.com, can
appear, because it is needed to find problems with a particular
provider. You can read the log before you send it.

## Licence

The full ZBox licence. The same text is in
`docs\LICENSE.txt`, and in ZBox under Help > About ZBox > View
License.

Copyright (c) 2026 Harith Alhamdani. All Rights Reserved.

Software: ZBox

### 1. Grant of personal license

Permission is hereby granted to individuals to download, view, compile, and
modify the original source code of this software ("ZBox") solely for
personal, private, non-commercial use on local devices owned or controlled
by the user.

This grant also covers official builds published by the copyright holder,
including beta and pre-release builds published on the ZBox GitHub
repository: an individual may download, install, and run such a build for
personal, private, non-commercial use, and may take part in beta testing
and report problems. This is a grant to USE an official build, and nothing
more. It does not permit republishing or passing on that build to anyone
else -- see Section 2, which applies to official builds exactly as it
applies to source code. A beta build is unfinished software offered for
testing; see Section 4.

### 2. Public distribution and forking prohibition

You are expressly prohibited from:

1. Publishing, hosting, or distributing ZBox -- modified or unmodified,
   as source code or as a compiled binary -- publicly or privately, to
   any third party.
2. Uploading ZBox, modified or unmodified, to public code repositories,
   forums, websites, or file-sharing services.
3. Distributing compiled binaries or executables built from ZBox source
   code, modified or unmodified.
4. Using this software for any commercial, business, or enterprise
   purpose.

### 3. Reservation of rights

All rights not explicitly granted herein -- including the exclusive right
to publish, distribute, and license this software publicly -- are reserved
by the copyright holder.

### 4. No warranty

ZBox is provided "AS IS" and "AS AVAILABLE", without warranty of any kind,
express or implied, including but not limited to the implied warranties of
merchantability, fitness for a particular purpose, title, and
non-infringement. The copyright holder does not warrant that ZBox will be
uninterrupted, timely, secure, or error-free, that any defect will be
corrected, or that it will send, receive, store, or delete any message
correctly or at all.

This applies with particular force to beta and pre-release builds, which
are offered for testing, are expected to contain defects, and must not be
relied upon for any message, account, or data you cannot afford to lose.
You are responsible for your own backups.

### 5. Limitation of liability

To the maximum extent permitted by applicable law, the copyright holder
shall not be liable for any claim, damages, or other liability -- whether
in contract, tort, or otherwise -- arising from or connected with ZBox or
its use, including without limitation lost, corrupted, unsent, or
misdelivered messages, loss of access to any mail account, loss of data,
loss of profits, or business interruption, even if advised of the
possibility of such damage.

Nothing in Sections 4 and 5 excludes or limits any liability that cannot
lawfully be excluded or limited.

### 6. Third-party services

ZBox connects to mail providers and downloads blocklist and phishing data
from third-party projects. Those services are not operated by the
copyright holder, are governed by their own terms, and may change or stop
without notice.

## Credits and third-party components

ZBox stands on the work of these projects, with thanks. The terms in
Sections 1 to 3 of the licence above apply only to ZBox's own code,
never to these components, which keep their own licences.

### Bundled in every published build, used unmodified

1. [Himalaya](https://github.com/pimalaya/himalaya), the command-line
   email client that does ZBox's IMAP and SMTP work. A build carries
   himalaya.exe in its himalaya folder; ZBox runs it as a separate
   program and never changes it. Copyright Clement Douin and the
   Himalaya contributors. MIT License.
2. [Python](https://www.python.org), the language runtime. Python
   Software Foundation License.
3. [wxPython](https://wxpython.org) and wxWidgets, the user interface
   toolkit. wxWindows Library Licence.
4. [IMAPClient](https://github.com/mjs/imapclient), behind instant new
   mail and fast message loading. Copyright Menno Smits. BSD 3-Clause.
5. [pywin32](https://github.com/mhammond/pywin32), Windows password
   protection. Copyright Mark Hammond and contributors. PSF-style
   licence.
6. [accessible_output2](https://github.com/accessibleapps/accessible_output2),
   which hands announcements to your screen reader. MIT License. It
   carries the NVDA Controller Client, copyright NV Access Limited,
   GNU LGPL 2.1, and the client libraries of other screen readers,
   which belong to their vendors.
7. [PySequoia](https://github.com/wiktor-k/pysequoia), OpenPGP for
   Python. Copyright Wiktor Kwapisiewicz. Apache License 2.0.
8. [Sequoia PGP](https://sequoia-pgp.org) (sequoia-openpgp), the
   OpenPGP library compiled into PySequoia, with its pure Rust
   cryptography backend. By the Sequoia PGP authors. GNU Lesser General
   Public License 2.0 or later, used unmodified. Its source code is at
   https://gitlab.com/sequoia-pgp/sequoia.
9. [cryptography](https://github.com/pyca/cryptography), OpenPGP in
   the classic format and S/MIME. Apache License 2.0 or BSD 3-Clause.
   Its Windows package carries [OpenSSL](https://www.openssl.org),
   Apache License 2.0.
10. [Trix](https://github.com/basecamp/trix), the editor behind the
    message body. MIT License.
11. The SCOWL English word list, behind spell check. SCOWL licence,
    carried with the word list.
12. [keyring](https://github.com/jaraco/keyring), read only, to move a
    password from an older ZBox. Copyright Jason R. Coombs. MIT
    License.

### Downloaded while ZBox runs, never bundled

1. [StevenBlack unified hosts](https://github.com/StevenBlack/hosts),
   ad and tracker blocking. MIT License.
2. [EasyPrivacy](https://easylist.to/pages/licence.html), tracker
   blocking, by the EasyList authors. GNU GPL 3 or Creative Commons
   Attribution-ShareAlike 3.0.
3. [Phishing.Database](https://github.com/Phishing-Database/Phishing.Database),
   the phishing link check. Copyright Mitchell Krog, Nissar Chababy and
   the Phishing.Database contributors. MIT License.

### Already on your computer

1. Microsoft Edge WebView2 Runtime, for HTML mail. Microsoft Software
   License Terms.

Each component is also listed, with its licence and where it comes
from, in `docs\LICENSE.txt` and under Help > About ZBox > View
License.
