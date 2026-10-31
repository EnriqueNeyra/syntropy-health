# What protects your data on disk, and what doesn't

This is the threat model for health data at rest in Syntropy Health: who might get at the data, what they could get
before the database was encrypted, what they can get now, and what encryption at rest can't do. The Settings → Security
page says the same thing in fewer words, for the computer you're on.

Reporting a problem: see [SECURITY.md](SECURITY.md).

## The short version

- The database file, its write-ahead log and the copies the app keeps of it are encrypted with AES-256 (SQLCipher).
- The key lives outside the data folder where the system offers somewhere better: the macOS Keychain, or Windows' protected
  storage (DPAPI). On Linux and Docker it stays in a file beside the data unless you put it elsewhere, and then the
  encryption protects little: use full-disk encryption there.
- While the server runs it holds the key, because background sync, phones and AI apps need the records when nobody is
  signed in. So **encryption at rest protects a copy of the files, not a running computer**.
- Everyone on one server shares one encrypted database. Passwords and sharing rules keep people apart. Encryption doesn't.
- Lose the key and every backup, and the records are gone for good. Nobody can bring them back. See [If the key is
  lost](#if-the-key-is-lost).

## Who might get at the data

"Before" is the last version with an unencrypted database. "After" is this version once the first start has finished.

| Who | Before | After |
|---|---|---|
| **A stolen or resold laptop, disk read directly, no FileVault or BitLocker** | Every record, and `secret.key` beside them, which opens saved passwords and sign-ins | **Mac and Windows:** unreadable files, unless the thief also breaks your computer password (Keychain, DPAPI) or guesses your Syntropy Health password ([why](#the-password-can-also-open-it)). Old unencrypted blocks can survive on the disk until something overwrites them, so this doesn't undo the past. **Linux and Docker with the key beside the data:** every record, as before |
| **The same laptop with FileVault or BitLocker on, powered off** | Nothing | Nothing. Keep it on: it also covers old blocks, swap and hibernation, which the app can't |
| **The same laptop, unlocked and signed in** | Everything | Everything. Encryption at rest does nothing here |
| **A Time Machine disk, or any copy of the data folder** (iCloud Drive or Dropbox if you moved the folder there, a folder synced to a NAS) | Every record and the key, in the clear | Unreadable without your Mac's login password or your Syntropy Health password. A Time Machine disk also holds the login keychain, itself encrypted with your login password. **Copies made before this version stay as they were**, until they age out or you delete them |
| **A second macOS or Windows account on the same computer** | Nothing for a standard account (private folder). An administrator could read everything | Nothing for a standard account. An administrator can copy the encrypted files but can't open the key without your password. They could still replace the program or read its memory while you use it, as an administrator can with any program |
| **Someone else on a shared household server** | What their account was given | The same. Encryption doesn't separate people. Access rules do, and they were audited and tightened in this version |
| **Whoever runs the household server** (with access to its account or an administrator's) | Everything | Everything. They hold the key |
| **Root on a Docker host, or whoever copies the volume** | Every record | Default (key in the volume): every record. With the key in a Docker secret or the environment: a copy of the volume alone is unreadable, but root on the host can still read the secret |
| **Malware running as you** | Everything | Everything. It can ask the Keychain or DPAPI as you can, read the server's memory, or use the app's own commands. Encryption at rest can't stop it |
| **Someone who gets a backup file** | Nothing without its passphrase, if it was encrypted. Everything if you chose an unencrypted one | The same. An encrypted backup holds the key, protected by the passphrase, so a long passphrase matters |
| **Someone with your password or a signed-in browser or phone** | What that account sees | The same |

## What's covered

| Holds health data | Before | Now |
|---|---|---|
| The database (records, chats, journal, documents kept with records, search index) | Plain SQLite | Encrypted pages |
| `-wal` log | Plain | Encrypted pages |
| `-shm` | Page numbers only | The same. No record content |
| Sorting and temporary tables | Temporary files for large sorts | In memory only (`temp_store`), so never on disk unencrypted |
| Copies kept before an upgrade or a restore (`pre-upgrade/`, `pre-restore/`) | Plain copies | Encrypted. Older plain copies are converted, or removed if there's no room |
| Backups with a passphrase | Sealed | Sealed, and the database inside is encrypted too |
| Backups without a passphrase | A plain zip | A plain zip, because you asked for one. The app says so plainly |
| Uploads waiting to be imported (`uploads/`) and the web server's spool for large uploads | Plain files, the spool in the system's temp folder | Encrypted with a key held only in memory. A crash leaves them unreadable, and the next start removes them |
| A person's archive while it's being built for download | Plain, in a private folder | Encrypted the same way, and decrypted only as it's sent |
| Logs (`server.log`, `desktop.log`) | No record content by design; search words and tokens are redacted from the access log | The same |
| Browser and phone caches | API answers are `no-store`, so records aren't cached | The same. Phone storage is covered in its own repository |

Not covered, because the app doesn't control them: exports and backups you've saved elsewhere, screenshots, swap and
hibernation files (FileVault and BitLocker cover those), and AI services you choose to use.

Files are private to the account that runs the server: 0700 for the folder and 0600 for what's in it on Mac and Linux,
and on Windows an access list giving only that account, SYSTEM and administrators access to a folder outside the profile.

## Where the key lives

| Where it runs | Where the key is | What that gives you | What can lose it |
|---|---|---|---|
| **Mac** | Login Keychain, read through Apple's own `security` tool | A copy of the folder doesn't include it, and it's protected by your login password | Resetting a forgotten login password, which starts a new keychain. Moving the folder to a new Mac by hand. Migration Assistant and a Time Machine restore bring it along |
| **Windows** | DPAPI-protected, in `key.json` in the data folder | It can only be opened by your Windows account on this PC. Another account or computer can't | An administrator resetting your Windows password. Moving the folder to a new PC by hand |
| **Linux home server, Docker** | `secret.key` beside the data, unless you set `SYNTROPY_KEY_FILE` or `SYNTROPY_SECRET_KEY` | A file somewhere else protects a copy of the data folder alone. It doesn't protect a stolen disk: use LUKS for that | Losing that file or setting |
| **If the Keychain or DPAPI can't be used** (a Mac reached only over SSH, say) | `secret.key` beside the data, and Settings says so | Little, as above | Losing the folder |

**Why not unlock with a password at each start?** It's the obvious way to get more from encryption, and it was rejected.
Sources sync on a schedule, phones upload whenever they can, AI apps ask questions through the MCP server, and all of
that happens with nobody signed in, including after a power cut. A database that waits for someone to type a password
stops all of it, silently, until they notice. Anyone who wants that trade can have it with `SYNTROPY_KEY_FILE` pointing at
a removable or mounted-on-demand volume.

**Why one key rather than one per person?** Per-person encryption was rejected too. Background sync writes each person's
records while they're signed out, so the server would hold their keys anyway. Sharing would mean holding each person's
key for everyone they share with. And the person running the server can already replace the program. It would add a lot of
machinery and a new way to lose records, and protect nobody from the one person it would need to. The plain statement
in Settings and here is more honest: **everyone on a server trusts whoever runs it.**

## The password can also open it

On a Mac or Windows PC, the key is also kept in `key.json`, locked with the Syntropy Health password of each owner of the
server (scrypt, then AES-256-GCM). It's made the next time an owner signs in or changes their password.

That's what lets you recover if the Keychain or Windows account is lost and you have no backup. It also means someone
with a copy of the data folder can try passwords against it offline, at about four guesses per second per core, with 128 MB of
memory for each guess. A good password is out of reach. A weak one isn't. We chose this because
for this kind of record, permanently losing it is the worse outcome. If you'd rather not have it, run
`syntropy-health key recovery off` and rely on your backups.

## If the key is lost

In order of how much is brought back:

1. **The key was in the Keychain or DPAPI and they're gone, and you know your Syntropy Health password:** the start page
   asks for it, or run `syntropy-health key recover`. Everything is back.
2. **You have any backup made since this version, and its passphrase:** `syntropy-health restore BACKUP --key-only` takes
   just the key from it, and today's database opens, with nothing lost. On a new computer, restore the whole backup instead.
3. **You kept a copy of the key** (`syntropy-health key export FILE`): put it back as `secret.key` in the data folder. If
   nothing here opens the database with it, nothing changes.
4. **Otherwise the records can't be read by anyone**, and no support request can change that. Keep a backup somewhere else
   (Settings → Your data), and keep its passphrase somewhere you won't lose.

`syntropy-health key new` is for a database that isn't encrypted yet, or that opens but whose saved sign-ins don't. It
can't make an encrypted database readable, and it says so rather than pretending.

## Encrypting a database that's already here

On the first start of this version (the Mac and Windows apps show progress on the start page), the server:

1. Saves the key in the Keychain or DPAPI and reads it back.
2. Checks there's room for a second copy. If there isn't, or another program has the database open, it carries on
   unencrypted, says so in Settings, and tries again next start.
3. Takes the database for itself and writes an encrypted copy beside it, under a temporary name.
4. Reads the copy back: page checks, the database's own consistency check, every table's row count, and a sample of rows
   compared value for value.
5. Puts the copy in place with one atomic rename. The unencrypted file moves to `pre-upgrade/` as the way back.
6. When the server has run well for ten minutes, that copy and any older unencrypted copies are converted or removed, and
   `secret.key` is deleted if the key is safely in the Keychain or DPAPI.

A crash or power cut at any step leaves working data: before step 5 the unencrypted database is untouched and the temporary
copy is discarded, after it the encrypted one is complete. Step 6's wait exists because an update that fails goes
back to the previous version, which can't read an encrypted database and needs the unencrypted copy.

**"Removed" has limits.** Deleting a file doesn't erase it. On a Mac (APFS) and on most SSDs, old blocks can survive
until the disk reuses them, and Time Machine and local snapshots keep old versions until they expire. Full-disk encryption
is the answer to that, and it's why we still ask you to turn it on.

## What this can't do

- Protect a running, signed-in computer, or a server from whoever runs it.
- Protect records from someone with a password, or from a person you've shared with.
- Reach back and clean copies made before this version, or what you've saved elsewhere.
- Hide how large the database is or when it changed.
- Detect an old copy of the file being put back in place of a new one. Each page is authenticated, so tampering and
  damage show up, but a whole older file looks fine.
