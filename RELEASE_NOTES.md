## Home Finance 1.1.0: automatic updates

### New
- **Automatic updates.** Home Finance checks GitHub for a new version each time it starts. When there
  is one, it asks **"Update now?"** and shows a banner. One click downloads the update, checks it
  against GitHub's SHA-256 checksum, installs it and reopens the app. Your data isn't touched.
  - **Tools → Check for Updates…** checks any time.
  - **Settings → Updates** shows your version and turns the startup check on or off.
  - Updates can only be started on the computer running the app, not from a phone.
- **MIT license:** the code is now free to use, change and share.

### Fixed
- After a restart, Home Finance sometimes moved to port 8091, which broke the address saved on phones.
  It now keeps port 8090.

### Updating from 1.0.0
1.0.0 can't update itself, so install 1.1.0 by hand once:
- **Mac:** download the .dmg below and drag Home Finance into Applications, replacing the old one.
- **Windows:** extract the zip and copy its **Home Finance** folder over your old one.

Your data stays where it is. From 1.1.0 on, updates install themselves.

### Downloads
| File | For |
|---|---|
| **HomeFinance-Mac-AppleSilicon.dmg** | Macs with Apple silicon (M1 or newer), macOS 11+ |
| **HomeFinance-Windows.zip** | Windows 10 and 11 (64-bit) |

First time installing? See the [setup guide](https://github.com/JPMixing-inc/home-finance-app#install),
including [Plaid bank sync](https://github.com/JPMixing-inc/home-finance-app#bank-sync-with-plaid) and
the [Claude API](https://github.com/JPMixing-inc/home-finance-app#ai-features-with-claude).

### Known limitations
- The apps are unsigned, so macOS and Windows warn the first time you open them.
- No Intel Mac build yet. Intel Macs can [run from source](https://github.com/JPMixing-inc/home-finance-app#run-from-source).
- Phone access uses plain HTTP on your local network. Use it on a home network you trust.
