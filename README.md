# ZBox

Portable, snappy, secure, accessible email client.

**Testing beta. Windows only.** Not a finished release: things will
break, and that is what this build is for.

ZBox is a desktop email client built for screen reader users. No
decorative chrome, every control named, every important state also
given as text, and the HTML message view tamed so a reader gets the
message and nothing else. NVDA and JAWS are the readers it is tested
against.

It has no speech of its own and never will. Announcements are handed
to whichever screen reader is running, which speaks them in your own
voice, rate and settings.

## What's new

In this version:

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

Also new in testing beta 26.10.02:

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
5. A privacy policy, in [PRIVACY.md](PRIVACY.md).

Also new in testing beta 26.10.09:

1. 22 more interface languages, 50 in all. See Interface languages
   below.
2. Moving through the From list in a new message, reply or forward no
   longer speaks the signature, Cc and Bcc, or encryption notices. Those
   settings still follow the account or identity you choose.

## Features

- Built for screen readers first, tested with NVDA and JAWS.
- Portable: one folder, no installer, nothing written outside it.
- IMAP and SMTP through the Himalaya mail client. Microsoft sign-in
  for Microsoft accounts, app passwords for Gmail and others, and the
  servers filled in for known providers.
- HTML mail through Microsoft Edge WebView2, prepared for screen
  readers, with a plain Text view always one key away.
- Remote content blocked by default, an ad and tracker blocklist, a
  phishing link check, and optional junk rules that never guess.
- Threads, a conversation view, and related messages across folders.
- Unified folders that combine one folder type from every account.
- Message filters, and Block This Sender.
- Extra identities, each with its own settings.
- OpenPGP encryption and signing, and S/MIME reading.
- Private mode for reading mail on a computer that is not yours.
- An address book with groups, vCard and CSV import and export.
- A Search tab across every account, by words, dates, unread,
  flagged and attachments.
- An offline copy of the Inbox and the Archive.
- A formatted editor with spell check, signatures, draft autosave,
  and Undo for delete, archive and move.
- Single-key actions in the message list, which can be turned off.
- App lock with a Windows Hello passkey, a FIDO2 security key or the
  master password.
- Updates found and installed by ZBox itself, put back automatically
  if a new version fails to start.
- Six interface themes, and sound themes you can add your own to.
- Watch Thread and Ignore Thread, with filters for watched and ignored
  threads.
- Move or copy messages by typing part of a folder name. Mute, pause or
  disable an account.
- Subscribe to server folders, mark a folder read, empty Trash and Junk,
  or let ZBox clear old Trash and Junk after the number of days you
  choose.
- Save a message as a file, open saved .eml messages, save every
  attachment at once to a folder you choose, and view or copy the full
  message headers.
- Message headers above the message, a message font size and reading
  font of your own, and the sender announced with Ctrl+U.
- Preview a message as it will be sent, a word count, and spell check
  from the keyboard.
- Per identity: its own signature, Reply-To address, Cc and Bcc, Drafts
  and Sent folders and outgoing server.
- OpenPGP key discovery from a mail provider and from keys.openpgp.org,
  publishing your key, Autocrypt headers, and your public key attached
  on request.
- Export and import your accounts and settings. Passwords are never
  exported.
- Minimize or close to the system tray, start with Windows, stay
  maximized, and a desktop shortcut.
- Tuning for HTML mail with a screen reader: how long the reader stays
  quiet when a message opens, and how long announcements stay.
- Settings for how long cached messages are kept, and for updates: ask
  first, or download and install quietly.
- A Keyboard Shortcuts page in Help.
- 50 interface languages, listed under Interface languages below.

## Getting it

Download the zip from the [Releases](../../releases) page, unzip it
anywhere you like, and run `ZBox.exe`.

Source code and releases: https://github.com/iqdz/ZBox

There is no installer. The whole folder is the app: put it on a USB
stick, an external drive or your desktop, and it writes nothing
outside itself. Nothing is added to the registry, nothing is left
behind if you delete the folder.

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

## Privacy

What ZBox keeps on your computer, what it sends and to whom, and what
its debug logs hold is described in [PRIVACY.md](PRIVACY.md).

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
attachments only by kind, and no email addresses at all. You can read
it before you send it.

## Licence

Free for personal, non-commercial use, with no warranty. The full
text is in `docs\LICENSE.txt` and under Help > About ZBox > View
License, along with the third-party components ZBox uses.

Built on the [Himalaya](https://github.com/pimalaya/himalaya) CLI
email client.
