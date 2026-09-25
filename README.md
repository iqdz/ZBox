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
new one the same way, and enter it in Tools > Account Settings, in
the New password field. Help > About ZBox has a button that opens
Google's own app password instructions.

### Outlook.com, Hotmail, Live and Microsoft 365

Not supported yet. Microsoft has switched off password and app
password sign-in for mail programs on these accounts. They accept
only OAuth2, a sign-in method where you log in on Microsoft's own
page, and ZBox does not support OAuth2 yet. There is no password or
app password that will work in the meantime.

The wizard recognises these addresses, says they are not supported,
and does not add the account. OAuth2 sign-in for Microsoft and
Google is planned for a future version.

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

## HTML mail

Messages render through Microsoft Edge WebView2, which most Windows
machines already have. If HTML mail does not render, Settings has a
legacy engine option and a way to use a bundled runtime instead.
Text view (Ctrl+B toggles) always works regardless.

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
