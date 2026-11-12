## Home Finance 1.2.1: mark debts paid off, and a Windows fix

### New
- **Debt snowball: mark a debt paid off.** Each debt has **✓ Paid off** and **✓ Paid off & closed**
  buttons. The debt drops to $0, leaves the snowball (its payment rolls into the next one) and
  moves to the **Paid off** list. Closing also sets the account's balance to $0 on the Accounts page
  and in net worth, and bank sync stops changing it, so a closed card no longer leaves an old balance
  behind. **Undo** puts a debt back.
- **See how far you've come.** The Paid off list shows each debt's original amount with a full green
  bar and the date it was paid off, and the **All debts** progress bar shows paid-off debts in green.

### Fixed
- **Windows:** the app no longer fails to open with "Failed to resolve
  Python.Runtime.Loader.Initialize" when it was unzipped from **HomeFinance-Windows.zip** instead of
  installed with the Setup exe. (Windows marks unzipped downloads as "from the internet" and refused
  to load the window's files. The app now clears that mark on its own files.)

### Updating
Click **Update now** when the app asks, or choose **Tools → Check for Updates…**.

**If the Windows app won't open at all** (the error above), it can't update itself. Download
**HomeFinance-Windows-Setup.exe** below and run it, then delete the old unzipped Home Finance folder.
Your data isn't in that folder, so nothing is lost.

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
