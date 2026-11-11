## Home Finance 1.2.0: a real Windows installer

### New
- **Windows installer.** Windows now has **HomeFinance-Windows-Setup.exe**: run it, click
  **Install**, done. Nothing to unzip. It installs just for you (no admin prompt), adds Home Finance
  to the Start menu (and the desktop, if you tick the box), and can be uninstalled from
  **Settings → Apps**. Your data folder is kept either way.
- **Updates on Windows use the installer.** The app downloads the new installer, runs it quietly in
  the background and reopens itself.

### Fixed
- **Updates:** clicking **Update now** right after opening the app could leave the update stuck on
  "Restarting…". The app now always closes so the new version can go in.
- **Daily:** fixed monthly bills no longer make a day look like you're spending too fast, and the
  per-day amounts line up on phones.
- **Net worth:** negative amounts stay on one line (the minus sign used to wrap in the Debt tile).
- **Settings → Phone access:** the QR code and the instructions sit side by side.

### Updating from 1.1.0
Click **Update now** when the app asks, or choose **Tools → Check for Updates…**. On Windows, your
existing Home Finance folder is upgraded in place and gets a Start menu entry and an uninstaller.
There's no need to download anything by hand.

If you'd rather install fresh on Windows, run the Setup exe, then delete your old Home Finance
folder. Your data isn't in that folder, so nothing is lost.

### Downloads
| File | For |
|---|---|
| **HomeFinance-Mac-AppleSilicon.dmg** | Macs with Apple silicon (M1 or newer), macOS 11+ |
| **HomeFinance-Windows-Setup.exe** | Windows 10 and 11 (64-bit) |
| HomeFinance-Windows.zip | Not needed. Version 1.1.0 uses it to update itself. |

First time installing? See the [setup guide](https://github.com/JPMixing-inc/home-finance-app#install),
including [Plaid bank sync](https://github.com/JPMixing-inc/home-finance-app#bank-sync-with-plaid) and
the [Claude API](https://github.com/JPMixing-inc/home-finance-app#ai-features-with-claude).

### Known limitations
- The apps are unsigned, so macOS and Windows warn the first time you open them (on Windows, click
  **More info → Run anyway** on the installer).
- No Intel Mac build yet. Intel Macs can [run from source](https://github.com/JPMixing-inc/home-finance-app#run-from-source).
- Phone access uses plain HTTP on your local network. Use it on a home network you trust.
