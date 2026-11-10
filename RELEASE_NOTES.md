## Home Finance 1.0.0: first desktop release

Home Finance is a private household budget app that now installs like a normal app on Mac and
Windows. Everything stays in one file on your computer. Phones on your Wi-Fi can use it while the app
is open.

### Downloads
| File | For |
|---|---|
| **HomeFinance-Mac-AppleSilicon.dmg** | Macs with Apple silicon (M1 or newer), macOS 11+ |
| **HomeFinance-Windows.zip** | Windows 10 and 11 (64-bit) |

**Mac:** open the .dmg and drag Home Finance into Applications. On first open, go to System Settings →
Privacy & Security → **Open Anyway** (the app isn't signed yet).
**Windows:** extract the zip and run **Home Finance.exe**. If SmartScreen appears, click
**More info → Run anyway**. Allow **Private networks** at the firewall prompt so phones can connect.

Each download includes **Read Me First.txt**. The full guide, including setting up **Plaid bank sync**
and the **Claude API**, is in the [README](https://github.com/JPMixing-inc/home-finance-app#readme).

### What's in it
- **Desktop app** with its own window and a **Tools** menu (Open in Web Browser, Phone Access, Show
  Data Folder). Quitting the app stops the server.
- **Phone access:** Settings shows your phone address and a **QR code** to scan. It's on by default
  and can be switched off so only the computer can open the app.
- **Your own setup:** first launch asks you to create a login. Add more logins for the household in
  Settings.
- **Budgeting:** dashboard, transactions with rules, monthly budgets, daily spending pace, net worth,
  and 12-month history.
- **Import** CSV, OFX and QFX bank exports, with duplicate detection and undo.
- **Bank sync** through your own Plaid account (free Trial plan: up to 10 banks), syncing every 2 hours.
- **Debt snowball:** debt-free date, progress per debt, and student-loan import from a statement PDF.
- **Claude AI** with your own Anthropic API key: Ask Claude, category suggestions and monthly
  summaries, with spending shown in Settings.
- **Backups:** download the database or export transactions as CSV from Settings.

### Known limitations
- **Unsigned builds:** macOS and Windows show a warning the first time (see above).
- **No Intel Mac build yet:** Intel Macs can [run from source](https://github.com/JPMixing-inc/home-finance-app#run-from-source).
- **Some banks (such as Chase) won't sign in inside the app window.** Use **Tools → Open in Web
  Browser** and connect the bank from there.
- **Phone access uses plain HTTP** on your local network. Use it on a home network you trust.
- **Bank sync only runs while the app is open.** Anything missed arrives at the next sync, or click
  **Sync all now**.
