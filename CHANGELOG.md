# Changelog

All notable changes to Syntropy Health. The version is in `app/core/config.py` (`APP_VERSION`); releases are tagged
`vX.Y.Z`, which also builds the Mac and Windows apps.

## Unreleased

- **Upload a lab report, on paper or as a PDF, from Sources, Records or Trends.** Drop in a PDF, a scan or a phone photo (HEIC too)
  and it's read on your computer, with nothing to install: the Mac and Windows apps use the reader built into the system, and the Docker
  image includes Tesseract. Each page is shown beside the rows it was read into, on a phone above them; choosing a row points out its
  line on the page, rows that were hard to read are marked, and results already on file for that test and day are pointed out and left
  out unless you say otherwise. Reports with results side by side in two columns are read in full. The original is kept once with the
  results, opens from any of them, and goes in exports and backups. Saving takes you to what you added. Sending a report to an AI stays
  optional and asks first, as before.
A large release: records import and sync more completely, backups and upgrades are safer, and accounts, the phone
connection and AI access are tighter. Lines under each heading are what changed for someone upgrading from 1.6.0.

**The disclaimer is clearer about what it is.** Syntropy Health isn't medical advice, and that's now also stated in
`NOTICE` as additional terms under section 7 of the license. The first-run checkbox is an acknowledgment, not a condition
of the license, and the Terms on the website cover the website and sign-in relay.

### Your records are encrypted on disk

- **The database can be encrypted now**, with SQLCipher (AES-256), and so can its log, the copies kept before an upgrade or a
  restore, and files on their way into an import. A new install is encrypted from the start. Records already here wait until
  you choose *Encrypt my records* in Settings → Security, so you can make a backup first, and the next start does it. The Mac
  and Windows apps show progress: it took about 40 seconds for a 3 GB database on a recent Mac, including reading it back to check it. It's
  safe to interrupt, and if there isn't room for a second copy, or an AI app has the database open, it carries on
  unencrypted, says why in Settings → Security, and tries again at the next start.
- **The key is kept apart from the data where the system allows:** the Keychain on a Mac, Windows' protected storage on a
  PC. On Linux and Docker it stays in `secret.key` beside the data unless you set `SYNTROPY_KEY_FILE` or
  `SYNTROPY_SECRET_KEY`, and then encryption protects little: use full-disk encryption there. Settings → Security says which
  applies to you.
- **Moving to a new computer now takes a backup.** A copy of the data folder alone won't open elsewhere on a Mac or Windows PC.
  An encrypted backup holds the key, so its passphrase is all you need, whichever computer it's restored on.
- **Losing the key can lose your records for good.** The password of an owner of the server can bring it back, as can any backup
  made with this version (`restore FILE --key-only`) or a copy from `syntropy-health key export`. `syntropy-health key new`
  says plainly that it can't help a database it can't open. Keep a backup somewhere else.
- **Going back to an earlier version:** an update that fails goes back by itself, with your data, for the first ten minutes.
  After that, an earlier version can't read the database: `syntropy-health database decrypt FILE` makes a copy it can.
- **Old copies aren't reached.** Time Machine and other backups made before this update keep the unencrypted database until they
  expire or you delete them, and deleted files can survive on a disk. That's why FileVault or BitLocker still matters.
- **Everything in the data folder is private to the account running the server**, including what earlier versions left readable, and
  on Windows a data folder outside your profile gets an access list saying so.
- [What this protects, and what it can't](.github/SECURITY-MODEL.md), including a stolen laptop, a second account on the
  computer, a Docker host, and malware.

### People on one server

Everyone on a server shares one encrypted database. Passwords and sharing rules keep people apart, not encryption, and whoever
runs the server can read everything. Every route and AI tool was checked for reaching across people, and these are closed:

- **An invitation code stops working once whoever made it no longer looks after that person.** Redeeming one takes the person
  over and shuts out their carers, and someone removed could still have used a code they'd kept.
- **A phone paired for someone else no longer reads, or adds to, the conversations of the account that paired it**, or shows what
  that account asked an AI. It can't agree to an AI service or change preferences for that account, or export the person's data.
- **Making a backup from a phone's screens takes the password.** A backup holds everyone, and those screens open with the phone's
  token. A browser that signed in isn't asked again.
- **A sign-in at a health portal started before someone's access ended can't finish.**
- **Someone who can only view a person no longer sees in the access log who else has access to them.**

### How it feels to use

- **Trends charts are cleaner and easier to read.** The pointer only has to be near a value: mouse, finger and arrow keys
  all snap to the closest one, with its details beside it rather than over it. Axes follow the calendar. Medications and
  diagnoses are off until you turn them on, and sit in a row above the plot instead of across it. A new range or test keeps
  the chart on screen until the next one is ready.
- **Arrange the Overview by hand.** In Edit, pick a card up from anywhere, or drag any corner to snap it through the
  grid's widths and heights. Everything around it moves out of the way as it goes. Done saves at once.
- **Nothing jumps.** Journal's new-entry dialog keeps one size between kinds, first-run steps and other dialogs ease to
  their new size, filters keep what's on screen until the new answer arrives, and pages are laid out before they appear.
- **Overview's small charts are coloured again, with round dots.**
- **Swiping no longer drags the whole Mac window.** ⌘[ and ⌘] go back and forward.
- **Search shows records as you type,** and the arrow keys and Enter choose one.
- **Removing a source says so straight away,** rather than looking as if nothing happened while a phone's years of samples are deleted.

### Records and portals
- Records' counts by kind change as soon as something is added or deleted, rather than on the next visit.
- On a phone's browser, Back closes Ask's list of conversations rather than leaving Ask.
- **Ranges a lab writes in words are read.** "> OR = 30", "Normal: 30-100" and a lab's bands like "Deficient: <20; Sufficient: 30-100",
  which is how vitamin D often arrives, were kept as text, so the result had no range on its chart and was never compared with it. They're
  read now, including results already stored, and the record still shows what the lab wrote. Where a range can't be read, Trends shows the lab's
  words, and where none came, it says so. Out-of-range points stand out from a test's line: they're larger, and the line is no longer red too.

- **Documents are dated by the visit they document.** A portal's Encounter Summary carries the day it was generated, so a
  first download put years of visits on one day on the Timeline. Records already stored are corrected without another sync.
- **Tidier names from imports and reports.** A lab read from a PDF or photo is saved under the name the dialog shows
  ("Glucose", not "GLUCOSE"), and the visit summary names the clinic that keeps an imported file where the file says.
- **Health systems are easier to find.** Search works by whatever you call the place: the name on your bill, the portal
  you sign in to, your town, a short name like OHSU or MGH, or a misspelling. Saint and St, Mount and Mt, state names
  and apostrophes all match. The best match comes first, places you can connect ahead of the rest, and long lists can be
  narrowed by state. A hospital that belongs to a larger system finds the system and says why. The count above the
  search box says how many places you can connect today and how many more are listed but not available yet.
- **Six more EHR vendors are listed:** TruBridge and MEDHOST hospitals, and Greenway, ModMed, NextGen and Practice
  Fusion practices. Registration with them is under way, so they're marked *Not available yet* and suggest importing a
  record file; each one connects as soon as its client ID is built in.
- **The directory is cleaner and about 39,500 health systems strong.** Test systems, vendors' internal servers,
  placeholders and retired practices are gone, duplicates are merged, and a damaged directory file costs only that
  vendor's listings, with a note in Alerts. Sources shows the real size instead of "20,000+".
- **The VA and Indian Health Service are no longer listed.** Syntropy Health isn't pursuing their registration for now.
- **A records request letter.** If your clinic isn't listed or can't connect yet, Syntropy Health can write a letter
  for you to print and sign. Nothing typed into it is saved or sent.
- **Import C-CDA, CCD and XDM files.** Health summaries downloaded from a patient portal (`.xml` or an XDM zip) become
  ordinary records: allergies, medications, problems, results, vitals, immunizations, procedures and visits. Anything
  that can't be read in detail stays readable under Clinical notes, and the original is kept to download. Older Apple
  Health Records (DSTU2) files and bundles with only full URLs now import too. A file holding two people is refused, and
  an import says what it couldn't read.
- **Importing again is safe.** The same file twice says it was already imported, a newer file from the same place
  updates the earlier import, and two hospitals using the same record id both keep their results. Each Apple Health
  export updates one source.
- **Records can't be wiped by a bad answer.** A hospital answering with nothing, or with an error dressed up as an
  answer, no longer erases your records. A record only leaves when two complete answers agree, and it goes to *Removed
  by the source* under Sources, where you can put it back for a year.
- **Syncs are steadier and complete.** A short outage no longer asks you to sign in again; Syntropy tries again by
  itself. Long histories arrive whole instead of stopping at 2,500 per kind of record, a big chart carries on from where
  it stopped, and one failed page keeps what already came in. Exam, imaging, procedure, activity, therapy, social needs
  and functional status results are brought in where a hospital offers them. Choose *What came in* from a source's menu
  to see how each kind of record fared.
- **Reconnecting to someone else's chart changes nothing.** Syntropy says whose chart it was and offers to add it as a
  separate source. A twin's or sibling's chart asks what to do instead of replacing the first child's records. Visits
  that share an account number stay separate records.
- **Custom FHIR servers** added by anyone but the owner must use https on the public internet. An older custom source on
  a private address needs the owner to sign in to it again.
- **Lab results read true.** Large values no longer lose digits (a CK of 1250 no longer shows as 125), and results stay
  in the unit the lab reported next to the lab's own range. A result like "<0.5" stays a limit everywhere and is only
  flagged when the whole limit is out of range. Charts never mix units, and different spellings of one unit stay on one
  chart. Titers, urine ranges and other results that aren't a single number are kept. A urinalysis result titled just
  "Glucose" no longer joins your blood results.
- **Reference ranges fit the person.** A range for another sex or age, or a critical range, is no longer used as the
  normal range, and with several equal candidates no flag is computed. A test's history joins its different codes.
  Flagged results more than 18 months old are marked as older and left out of the bell.
- **Medications and problems follow one rule.** Older orders, holds and plans are listed as *May be out of date* with
  the reason, drafts and proposals under *Planned*, and strengths of one drug sit side by side. Possible diagnoses and
  ones with no stated status are shown under their own names. A medication, problem or allergy that one source marks
  inactive stays shown while another says active, with a note saying who said what.
- **Smaller record fixes.** Refused vaccines read as not given, upcoming visits show as upcoming, dates known only to the
  month or year are shown that way, amended notes and final reports update on the next sync, RTF and non-UTF-8 notes
  read properly, and records already stored are corrected once in the background after updating.
- **No more raw record IDs in place of names.** A clinician, location or organization named only by reference is left
  out instead of shown as an ID.
- **Correct your record.** Mark a medication as not taken or taken differently, mark any entry as wrong or not you, or
  add a note. What the source says is never changed; the visit summary, Ask, AI apps and exports show both, with yours
  labelled patient-reported. You can also add medications, supplements and allergies no source lists.
- **Documents are kept.** PDFs and images attached to notes and reports are kept with the record to open or download,
  and text in a PDF can be searched and read by Ask. Records of a kind with no special view, such as family history or
  referrals, are kept under *Other records*. A health summary that rules out only drug or food allergies says exactly
  that rather than "No known allergies".
- **Filter and search.** Records can be filtered by source, report type, note type, author and date, and a search finds
  several words in any order across note text, report text and clinicians. A visit links to its notes and results, and a
  result shows the one before it and links to its history.
- A file for someone born on a different day than the birth date held for this person is called out when it's imported, on Overview and on the visit summary, which no longer prints that patient's name beside this person's birth date.
- Records and Timeline can be narrowed to entries with a document or to your corrections, and Timeline can show other records.
- A medication or allergy name made only of spaces is stopped on the page.

### The visit summary and Overview

- **Ranges are stated honestly.** The visit summary and Overview count results as in range, out of range or not
  assessed instead of calling a result in range when no range exists, and simple written ranges like 70-99 are
  compared, including on results already stored. A lab's own flag, such as critical, is shown worded as what the lab
  reported.
- **A fuller summary.** It shows source and date on its rows and each result's previous value, says how much it left
  out, and adds home blood pressure and glucose, past surgery, imaging, smoking status and logged symptoms. Procedures
  not marked completed are listed separately with their status. Choose which sections it includes (remembered for each
  person), print it with cleaner page breaks, and on a phone the tables stack so dates stay visible.
- **Prepare for a visit** lists what's on record since a visit or date and keeps your own questions, which print on the
  summary. It covers allergies, vaccines, vital signs, reports, upcoming visits and every other kind of record.
- **New since you last looked** lists new visits, diagnoses, medications, notes and results with dates; a source's first
  import is one line. Your people shows everyone you can see, with what's new, what's out of range and how their sources
  are.
- **Sources say how current they are.** The Overview, alerts, Sources and visit summary state how old each source is and
  which kinds of records it couldn't fetch. After connecting, a panel says what came in and where to go next.
- **Overview starts with your medical record:** out-of-range results, medications, conditions and allergies come before
  wearable numbers. An Overview you've already arranged stays as it is. Timeline has year headings that stay on screen,
  Records is broken up by month, and a record that gives only a year sits under that year.
- Charts have a period control, home readings appear on the clinic blood pressure chart, and medications and diagnoses can
  be marked on any chart. Ages under 2 show in months, every vaccine is listed, and symptoms and doses you've logged can be compared with any
  measure, by days with and without them.
- Prepare for a visit now includes other records and documents kept with a record, and shows your own words beside an entry.
- An entry you marked as wrong or not you no longer counts as new, and in Prepare for a visit it is listed apart from the facts.
- The visit summary shows what you said about an entry on every table of entries, and has a section you can include for entries you corrected.
- A visit summary printed from a phone prints as a table.

### Insights and trends

- **What's changed states facts and connects nothing.** Notes that set sleep against mood or symptoms, hard training
  against HRV, or a measure against a medication's start are gone, and so is the Journal's Patterns list. Compare still
  puts any two things side by side, with the numbers and no verdict, for you to judge.
- **What's changed is quieter and more careful.** A change only appears when the numbers support it. Slow, sustained
  changes, such as weight moving steadily over two months, show again. Lab notes give the right direction, ignore tiny
  changes, describe slow drift over years, and mention when a result moves closer to its range or comes back in range.
  Old lab results stay on the out-of-range list marked with their age but no longer lead *What's changed*.
- **A new watch or ring isn't a change in you.** Syntropy Health says the device changed and compares each device only
  with itself. Adding your phone to your watch's steps doesn't count as a new device.
- **Trends shows your usual range** and marks when your device changed. On long ranges the big number is labeled as
  the latest day, and blood pressure with only one number shows a dash instead of an invented pair. Compare shows the
  numbers on the days that have both, with no verdict.
- **Training load** waits for four weeks of history before comparing, one glitched heart rate can't change your zones, a
  week off no longer makes a regular exerciser look new to training, and you can set your own maximum heart rate.
- Medications are marked on charts at their first order on record, not each renewal, and only say *Started* when the
  record says you took them. Hiding an insight hides it on your other devices. Blood oxygen in text sent to AI apps
  reads 96.5%, not 9650%.
- A lab trend shows one lab's range, never one lab's low limit with another lab's high limit.
- With no range on record, What's changed says a result was flagged by the lab rather than calling it outside a range.

### Ask and AI apps

- **Conversations have a panel of their own.** It sits beside the chat and folds away (⌘B or Ctrl+B), or slides over on a
  narrow window, with Pinned first, then Today, Yesterday and on back by month. Search looks through titles and what was
  said (⌘K or Ctrl+K). Rename, pin, export as Markdown or delete from each one's ⋯ or by right-clicking; a name you chose
  stays through later answers, and a pinned conversation is never the one the limit removes. In the phone app, Rename and
  Pin are in its Conversations menu.
- **More you can do with an answer.** Copy any answer, code block or table, regenerate the last answer, edit your last
  question and send it again, and jump back to the latest message after scrolling up. You can read another conversation
  while one is being answered, and what you were typing stays with its conversation.
- **Ask checks where your question is going.** If the AI changed in another window, nothing is sent and you can try
  again, and each answer names the AI that wrote it. Agreeing to send data outside your network is per company (or per
  server for an address you typed), asks again for a new one, and can be taken back in Settings → AI. A model that can't
  look things up is sent a summary of the record, so Ask asks you first and says when that's happening.
- **Answers are clearer about themselves.** One that was cut short says so and offers Continue; one the AI declined is
  removed with a plain message; answers that used no lookups say they weren't checked against the record. Errors keep
  your question and offer Try again. Ask no longer fails from the 13th question in a chat with Claude. Answers ending in
  a table keep their last row.
- **Better with small local models.** They get fewer lookups and shorter results so your question isn't pushed out, and
  Settings explains how to give them more room. Servers that don't report a model's room get full lookups.
- **Everyone's access is separate.** Someone who isn't the server's owner chooses an AI for their own questions only;
  when the owner turns the assistant off, it's off for everyone. Each lookup Ask makes appears in the access log, and
  what was searched for shows only to the account that asked. The person's details no longer appear on Claude Code's
  command line, Gemini CLI won't run without its read-only policy, a saved key only goes to the address it was saved for,
  and changing a custom server's address without typing a key forgets the saved key.
- **MCP and AI apps read only who they should.** An AI app can only read the people its account can see, including when
  it looks up a record by id, and loses access when sharing ends. One that can read several people must say whose records
  it wants, and every result names the person. The access log shows whose records an app read, whether the lookup
  worked, and (to that member only) what was searched for.
- **Tokens and connections.** AI app tokens can stop working after 30, 90 or 365 days and are limited to 120 lookups a
  minute; existing tokens keep working until revoked. Requests from a web page on another site are refused, and without
  a password `/mcp` answers only to local names (add others to `SYNTROPY_ALLOWED_HOSTS`). The Claude Desktop setup reads
  one person's records by default, and `syntropy-health mcp --profile NAME` does the same from the command line.
- **Better results for AI apps.** Long answers come in parts instead of cut off, with older data reachable by date or by
  asking for the next part; long notes and lab text can be read to the end. Search finds a word inside a longer one
  ("diabetes" finds Prediabetes). Tools say when a metric, date or name is wrong and what would work. The Anthropic
  default is now Claude Sonnet 5.5. Text inside records is flagged to the AI as information, never instructions.
- The access log says Ask looked something up to answer a question.
- The question before using an outside AI says it sees your name and birth date too.

### Wearables and phone data

- **Later data corrects earlier data.** A later sync or phone upload now corrects a day's steps, revised sleep and
  readiness scores, WHOOP strain and edited workouts. After updating, cloud wearables re-fetch stored history a stretch
  per sync to fix older days. Totals re-sent by a Health export automation replace the last value instead of adding to it.
- **Units are right.** Glucose in mmol/L, weight in pounds, distances in miles or yards, height and waist in inches,
  water in fluid ounces and energy in kJ now show correct values, and stored rows are corrected.
- **Impossible readings are set aside.** A heart rate of 5000 or a date in 2105 is kept but left out of charts and
  averages. Trends says how many were left out and why, lists them, and lets you remove any for good, including from
  each measure's *Readings* button.
- **Sleep is cleaner.** Naps are separate from the night and appear as their own figure, a night that arrives in two
  parts lands on one day, and a doze in the evening or a long daytime sleep doesn't count as last night. WHOOP recovery,
  HRV, strain and energy land on the morning of the sleep they belong to. Each person can have a home time zone.
- **Daily numbers add up.** Daily heart rate isn't dominated by one workout, daily totals and detailed readings from one
  source don't add together, steps across midnight are shared between days, and totals sent an hour at a time all count.
- **Apple Health exports import fully.** Every kind of measurement the iPhone app sends comes in, the broken files iOS 16
  wrote are handled, a file that stops early keeps what was read, and the import says what it skipped. An export and the
  iPhone app no longer double-count distance or energy.
- **A refused sign-in shows.** If Oura, WHOOP or Google refuses access, Sources says so and the missing days are fetched
  once access is back. A Google project in Testing gets a plain explanation when its sign-in ends. A connected wearable
  can bring in older history from its menu.
- **Simulated wearables follow your time zone**, and simulated figures no longer fill gaps once a real device reports.
- One odd item or impossible date from a phone no longer fails a whole upload, and a nearly full disk raises an alert.

### Accessibility and design

- **Works from the keyboard and with screen readers.** Records, timeline events and list rows open with Enter, dialogs
  keep focus, menus follow the arrow keys, there's a skip link, and moving to a new section is announced. Charts describe
  their values in words and can be shown as a table; limit results keep their < or >.
- **Easier to read.** Muted and status text meet WCAG AA contrast in both themes, links in text are underlined, and
  health systems listed in capitals read normally. Red is used less, small trend lines are grey, and Sources has one
  main button.
- **Fewer surprises.** A dialog with something typed in it no longer closes on a stray click, an expired session shows
  the usual sign-in and keeps your check-in, a failed search says so with Try again, and double-clicking Save doesn't add
  a person twice. Switching sections no longer flickers. On phones, Back closes a record or panel instead of leaving the
  page. Dates with no time no longer show an invented 12:00 PM.
- **Plainer words.** Setup and Sources say "phone" rather than "iPhone" and link to the Android app; the empty Overview
  and Sources start with medical records and say your data is saved on the computer running Syntropy Health. Records
  show High and Low badges rather than "critical_low", and the access log names sources, people and AI apps.
- "Try simulated data" appears only in developer mode and asks first; Overview, Trends, Ask and Workouts say "Includes
  simulated data" until it's removed.

### Accounts and security

- **Phones pair to a person.** A phone paired by someone else no longer opens or feeds your record once you join, or
  when the person who paired it loses access; pair it again with your own sign-in. A phone paired for someone opens only
  that person and can't download a backup, create AI app access, change the password or share their data.
- **Owners and members.** The owner can hand the server to someone else or add another owner (Settings → People or
  `syntropy-health accounts make-owner NAME`), sees everyone who signs in without their health data, and can remove them.
  Anyone can delete their own account and data with their password. *Change access* now says exactly what it allows.
- **Forgotten passwords.** *Forgot your password?* shows the command that sets a new one on the computer running the
  server. People with the same name can each sign in, and `syntropy-health accounts` prints ids.
- **Sign-in is sturdier.** Passwords are stored with stronger settings and upgrade at the next sign-in, very common
  passwords are refused, wrong guesses only use up tries for that address, and browsers sign in again after six months.
  Settings → Security signs out one browser or all the others. Changing your password asks for it twice.
- **A new server can't be claimed from afar.** A server with no password yet no longer shows names or birth dates to the
  network. The first password is chosen nearby, or with a one-time setup code printed in the server's log, which also lets
  you set up a remote or Docker server from elsewhere. Behind a reverse proxy, a visitor can't pass as nearby by naming
  the proxy's address.
- **Sign-in links ask first.** Signing in to a health system, Oura or WHOOP stops and asks before sending anything to an
  address that isn't this computer or your own network, including Tailscale names ending in .ts.net. Addresses on your
  network stop on a page naming the address. Own domains and short names like `http://nas:8000` now finish a sign-in;
  behind a reverse proxy, pass on `X-Forwarded-Proto` and `X-Forwarded-Host`.
- **Wearable sign-ins are separate.** Setting up Google Health no longer breaks Oura and WHOOP; each has its own redirect
  address under Settings → Wearables, and Google refresh tokens only go to Google. If you keep your own Oura or WHOOP
  secret on your own relay, name it under *Your own relay* (https only). An Oura or WHOOP outage no longer asks you to
  reconnect.
- **Safer uploads.** Very large requests are refused before they're read, uploads need you to be signed in, and booby-
  trapped Apple Health files are refused or their oversized parts left out, with a message.
- **Cleaner logs and conversations.** Removing someone is logged with who did it, the access log shows backups and
  command-line password resets, AI conversations are deleted when their author is removed or loses access, and members
  no longer see folder paths from the owner's computer. The test simulator and API route map answer only in developer
  mode.
- **Syntropy Labs' registrations come with its releases only.** The Mac and Windows apps, the installers and the Docker
  image carry a key the sign-in relay and callback page check; a copy built from source, such as a fork, uses its own
  registrations and says how. The relay keeps serving releases from before the key for now.
- People who can only view no longer see controls they aren't allowed to use.
- Invited people are told their records are on the computer that runs Syntropy Health, not on their own device.
- Deleting a person also clears their words from the search index, so they don't travel in later backups.
- An archive that would unpack to more than the computer's memory can hold is refused.
- Test names, doctors' names and note types stay out of the server's log, as search terms already did.

### Backups, restoring and upgrades

- **One encrypted backup.** From Settings or `syntropy-health backup`, one file protected by a passphrase you choose
  holds everything, including the encryption key, so saved sign-ins come back on a new computer. Leaving it unencrypted
  takes an explicit choice and shows a warning. Pointing `backup` at an empty or wrong data folder is refused.
- **Restore with a safety net.** Restore from Settings, the first-run screen or `syntropy-health restore FILE`
  (`--check` tests a file and changes nothing; `--key-only` takes just the key). Nothing changes until you say so, and
  what it replaces is kept beside the data. Older backup zips still restore.
- **Scheduled backups** run daily or weekly to a folder you choose, keep the last few, and Alerts says when they fail or
  stop.
- **New exports.** Everything for one person in one file that another Syntropy Health can import, and an International
  Patient Summary (a FHIR document). FHIR exports keep medication names, codes and note text, say which source each
  record came from, and stay valid with hand-typed labs. CSV exports gain the missing columns, and the records CSV adds
  value as reported, comparator and reference as reported.
- **Deleting means deleting.** Deleted data is overwritten in the database file and cleared from the log soon after, new
  backups don't carry it, and deleting a person, their data or a source also deletes the pre-upgrade copies. Backups you
  already made still contain it, and the dialogs say so. *Delete all data* asks you to type DELETE.
- **Upgrades keep a copy.** Before upgrading its database, Syntropy Health keeps a copy in a pre-upgrade folder beside it
  and won't upgrade if it can't. Data from a newer version, a damaged database or an unusable data folder gets a plain
  explanation instead of a crash. A lost or wrong encryption key is explained as the key, not the password; a new one is
  only made with `syntropy-health key new`.
- **Health checks.** The database is checked for damage at start-up and daily, with a note in Alerts, and a damaged file
  isn't backed up. `/health` really checks the database can be read and written. If the data folder is moved or its drive
  unplugged, Syntropy says so instead of creating an empty database.
- **Steadier and faster.** The server keeps answering during long writes, phone uploads survive a power cut once
  acknowledged, and a first phone backfill no longer leaves gigabytes of extra disk in use. The Overview's numbers, Trends
  and insights are kept until the data behind them changes (63 ms to 2 ms with 1.9 million samples).
- On Linux, `sudo syntropy-health backup /a/folder` works and the file belongs to you.

### Installing and updating

- **Updates are checked before they install.** The install scripts and `syntropy-health update` check downloads
  against the release's published checksums, and the Mac app's signature is checked too. The Mac and Windows apps keep a
  copy of themselves until the new version has started; if it doesn't, they go back, restore your database, tell you,
  and don't retry that release for a day. `syntropy-health update` waits for the server to come back before saying it
  worked, and on the Linux service puts the previous version back if it didn't. Installers and `update` no longer fall
  back to installing source code when they can't find a release.
- **Windows updates work again.** The app couldn't read its own signature, so it refused every new version. Updates no
  longer turn Open at Login back on, the installer requires Windows 10 or later, and the background server no longer
  stops after 72 hours or when a laptop is unplugged.
- **Docker has the command.** `docker exec -it syntropy-health syntropy-health backup /data/backups` and `docker exec -it
  syntropy-health syntropy-health accounts reset-password NAME` work, and files they write stay usable by the server. The
  container no longer crash-loops on files restored as root, `docker-compose.yml` works on older Compose versions, and
  the image is rebuilt weekly with security fixes (`docker compose pull`). Make the data folder first with `mkdir data`.
- **Release files are verifiable.** Every release fixes the exact versions of its dependencies, release files carry a
  signed attestation (`gh attestation verify FILE -R EnriqueNeyra/syntropy-health`), and Pillow and pypdf moved past
  versions with published vulnerabilities.
- **Installers are safer to re-run.** If the connection drops mid-download nothing runs. Running an installer again keeps
  the server's port and address. `service stop` on a Mac really stops the server, `info` and `service status` no longer
  say "not installed" while the Linux service runs, and uninstalling removes only what the installer added.
- **Uninstalling asks about your data.** The Windows uninstaller asks (keeping it is the default); on a Mac, *Syntropy
  Health → Uninstall Syntropy Health…* quits, removes the login item and moves the app to the Trash. The README lists
  how to remove every kind of install.
- **The apps explain themselves.** If the app can't start it says why in the window; for a damaged database you can
  restore a backup, set the file aside and start fresh, or open the data folder, and nothing is deleted. If the usual port
  is busy it uses another until the next start. The first start after an update shows it's working. A full disk is
  named instead of the app closing silently.
- **Another site in the app's window** can't start programs, reach the app or pass itself off as the app, and always
  shows its name and a way back. The Mac app's way back from a sign-in works, and exports ask where to save.
- **Connecting to a health system no longer jumps.** On a Mac the sign-in opens in a sheet over the window, which stays on
  Syntropy Health, with the site's name, a Cancel button and nothing flashing white; when it's done the sheet closes and
  the page shows the result. In a browser and in the Windows and phone apps, a short message says where you're going
  first, and the page fades in when you come back.
- A Mac can join a household server by its Tailscale name. Reading photos of lab reports says what the apps need and
  works as soon as Tesseract is installed. Logs rotate instead of growing forever, and the server keeps running when its
  log can't be written. `syntropy-health --help` no longer shows "==SUPPRESS==".

## 1.6.0

- **The iPhone app feels like an iPhone app.** Pages tell the app what to draw natively, when the app says it can:
  title-bar buttons and menus, a message field above the keyboard, menus as action sheets, haptics. The app builds its
  tabs and More from the sections the server lists, so a new section reaches the app without an app update, and
  apps and servers of different versions keep working together.
- **Ask in the iPhone app is a native chat.** The conversation scrolls with the screen instead of inside a box, the
  message field sits above the keyboard and grows as you type, and the model, past conversations and New chat are
  title-bar menus.
- **The model menu opens at once.** Ask fetches it as the page loads, and the server reuses what it found (models on
  your network, AI apps signed in on this computer) while it looks again in the background.
- **Trends in the iPhone app no longer gets stuck on a chart.** Refreshing the Trends tab (or a change on another
  screen) showed the selected test's chart in place of the list, with no way back.
- In the iPhone app: no pinch or double-tap zoom, and no zoom on a focused field; a dragged Overview card stays under
  your finger; Sources is laid out like iOS settings; a workout opens as its own screen; dialogs end above the tab bar;
  Journal, Records and the visit summary have their main action in the title bar. Date fields fit their form on
  iPhone and iPad.

## 1.5.0

- **The Windows app opens.** It handed its window a PNG icon, which Windows' window toolkit can only read as an
  .ico: creating the window failed and nothing appeared, from the installer or the Start menu. Without the Microsoft
  Edge WebView2 Runtime it now opens in your browser (and says where to get the runtime), and a start-up problem shows
  a message instead of exiting silently. The release build now opens the Windows app and checks its window.
- **The Mac app leaves the Dock when its window is closed.** It keeps running in the menu bar, as before, but its Dock
  icon stayed too. It's now in the Dock (and ⌘-Tab) only while the window is open, and opened at login it starts in the
  menu bar alone.
- **Wearable syncs fill the gap after failures.** An Oura, WHOOP or Google Health sync started from the last attempt,
  so after more than three days of failed syncs (an expired sign-in, reconnected later) the days in between were never
  fetched. It starts from the last sync that worked; an Oura endpoint that errors fails the sync instead of counting
  as an empty success.
- **No more missing lab results in the Timeline.** A page that ended partway through a lab panel skipped the rest of
  it.
- **A long workout no longer stops the iPhone's workouts syncing.** A route of more than 100,000 points (a day-long
  hike) was refused, and the phone sent the same page again on every sync. Routes and heart rate are thinned to 20,000
  points, and over-long text is cut to fit.
- **The server stays responsive during big jobs.** Health-system and wearable syncs, FHIR file imports, changing the
  time zone, and deleting a person or a source's data no longer hold up every other request while they run.
- Dialogs close when you go to another page (one could stay over the next page); the goal dialog shows its choice;
  Overview's number tiles line up when one shows a goal; Records, Timeline and the health-system search show the
  answer to what was typed last; Workouts' weekly volume is charted in hours; Settings → Security names browsers, and
  the access log reads as words and names who connected a health system or wearable.
- Profiles: a birth date can be cleared, and names, dates and colors are checked when changed. The FHIR export's file
  name works for any name. A health system's access token is never sent to an attachment on another host.
- **Android phones send their health data too.** The new Syntropy Health Android app reads Health Connect (Fitbit,
  Pixel Watch, Samsung Health, Oura and other apps that write to it) and sends it to this server like the iPhone app
  sends Apple Health, with nothing in between. Pair it from **Sources → Phones & watches**; it shows as
  **Health Connect · *phone name***. Fitbit data from a phone is ranked with Oura and WHOOP for sleep and overnight
  vitals, and Samsung Health with the watches. Without Google's paid yearly security review, this is how Fitbit and
  Pixel Watch data reaches a server that doesn't bring its own Google Cloud app.
- **An iPhone paired again for someone else sends them its data.** Samples, workouts and journal entries keep
  HealthKit's own ids, which were unique across the whole server, so a phone first paired to the wrong person and then
  to the right one had everything counted as duplicates: the second person got nothing. They're now stored for each
  person, and still only once per person.
- **The iPhone app's companion mode feels more like the rest of iOS.** A chart picked in Trends opens as its own screen
  with the chart's name as its title and the app's back button, instead of replacing the list with an extra back link;
  the Visit summary drops its own back link too. Records' search runs across the screen with its filters in one row
  that scrolls sideways, like Journal's and Timeline's. Rows and buttons no longer stay highlighted after a tap on a
  touch screen. Screens that redraw in the background no longer lose a chart's line, a page that can't reach the
  server shows the app's own "can't reach your server" screen, an accent chosen on one screen recolors the others, and
  an account that joins from the iPhone goes straight to its data rather than a first-run welcome on every tab. In the
  app, Settings no longer offers a theme (it follows the iPhone's) and pairing no longer offers a link back into the
  app itself.
- The usual bedtime in the irregular-bedtime insight is shown in your own clock (8:57 PM rather than 20:57), and
  Settings → AI says "Choose an AI for Ask" when one is found on the computer but none is chosen yet, instead of "No AI
  connected yet" above it.
- **Workout humidity is a percentage again.** The iPhone app sent humidity from Apple's Workout app in hundredths of a
  percent (65% arrived as 6500); the server now reads both, and workouts already stored are corrected.
- **eClinicalWorks (healow) connections are one-time imports, and say so.** healow gives refresh tokens only to
  confidential apps, so access from a sign-in ends within minutes. A connection whose access has ended now shows as
  **Imported** with an **Update** button that signs in again, instead of a "Reconnect needed" warning that came back
  after every sign-in. Scheduled syncs leave it alone, a reminder to update arrives once a month and can be dismissed
  until the next one, and choosing an eClinicalWorks practice explains this before you sign in. Any other connection
  that can't renew its access (such as an Epic one whose registration failed) is treated the same way.

## 1.4.0

- **Oracle Health and eClinicalWorks (healow) health systems connect for real.** Syntropy Health's client IDs for both
  are built in, so choosing one takes you to its own sign-in, with nothing to set up. On October 3, 1,311 of Oracle
  Health's 1,319 organizations and 95% of a sample of 300 eClinicalWorks practices reached their sign-in; most of the
  rest are eClinicalWorks practices that don't offer patients online access. Whether eClinicalWorks keeps a connection
  syncing after the first sign-in (with a refresh token) isn't confirmed yet; if it doesn't, a later sync asks you to
  reconnect.
- The weekly sign-in check covers Oracle Health and eClinicalWorks too, so their health systems whose sign-in is broken
  are marked before you try, like Epic's.
- **athenahealth practices connect for real.** Syntropy Health's production client ID is built in, so choosing
  athenahealth takes you to its patient sign-in with nothing to set up.
- **athenahealth connections keep syncing.** Renewing access now sends the scope athenahealth requires, so a connection
  stays live past its first hour (athenahealth's refresh token lasts 90 days from its last use, and each sync renews
  it). The sign-in no longer asks for insurance coverage, which athenahealth refused outright, and medications are
  imported: athenahealth only answers a medication search that names the order's intent.

## 1.3.0

- **Health systems whose sign-in isn't working say so before you try.** A weekly check opens every Epic health
  system's sign-in, the way a browser would, and records the ones that fail. Choosing one now explains what's wrong
  (its sign-in page is broken, its server isn't answering, or it hasn't finished setting up Syntropy Health), offers to
  import a record file, and still lets you try. When another listing of the same health system works, it's suggested:
  UPMC's broken listing points to **UPMC Patient Portal**. Listings that work come first in search.
- **Kaiser Permanente connects again** in every region. Kaiser's sign-in page for all regions but Washington has been
  failing with "Page Not Found", while the same page spelled in lowercase works, so sign-in now goes there. This is a
  temporary workaround: it switches itself off once Kaiser fixes the page.
- **Kaiser's later pages of clinical notes are imported.** Kaiser writes its server name in capitals in the links to
  the next page (FHIR.KP.ORG), and a sync stopped there as if the link led to another server, marking it partial.
- **The health-system directory is current again**, from Epic's, Oracle Health's and eClinicalWorks's lists of
  October 3: 3 Epic health systems and 509 eClinicalWorks practices are new, and SGMC's address changed. A weekly job
  keeps it current from now on.
- Refreshing the directory no longer swaps two health systems that share a name (Memorial Health in Ohio and in Georgia),
  which would have shown a saved connection under the other one.

## 1.2.1

- **A way back from a health system's sign-in in the Mac app.** While the window shows another site, it has an
  ordinary title bar naming the site and a **‹ Syntropy Health** button that returns to the app (also Go → Back to
  Syntropy Health, ⇧⌘H). Before, the page filled the window with no way back but swiping or ⌘[.

## 1.2.0

- **Health systems connect for real.** Choosing Kaiser Permanente, or any Epic health system, takes you to its own
  MyChart sign-in. Before, a sandbox mode or client ID left in Settings sent it to Epic's test server instead.
- **Developer mode** (Settings → Developer) is now one switch. Off, every platform connects for real with its production
  client ID. On, each platform uses the mode you choose: the simulator, the vendor's sandbox, or production. Each platform
  has a separate sandbox and production client ID, and a client ID saved before this version moves to the mode it was
  saved for. In the environment, `EPIC_CLIENT_ID` and the like are production IDs; sandbox IDs go in
  `EPIC_SANDBOX_CLIENT_ID` and the like. **Verify** checks a production ID against a real health system's sign-in.
- **Health system search** shows full names instead of cutting them off. Health systems that can't be connected yet
  (vendors Syntropy Health isn't registered with) are marked, listed after those that can, and suggest importing a
  record file instead. Popular shows only ones that connect, and the SMART test server appears in developer mode only.
- Releases carry each app once, under its fixed name (`Syntropy-Health-mac-arm64.dmg`,
  `Syntropy-Health-windows-x64-setup.exe`), instead of a second copy with the version in its name.

## 1.1.2

- **Updates download again in the Mac app.** It couldn't verify GitHub's certificate, so installing an update failed.
  The app also reaches a Syntropy Health server at an `https://` address when joining one. Updating to 1.1.2 itself
  needs [downloading it](https://github.com/EnriqueNeyra/syntropy-health/releases/latest) once; later updates
  install themselves.
- Releasing is a pull request: merging a change to `APP_VERSION` tags it and publishes the release, with the changelog
  as its notes. Pull requests need one CI check, **CI passed**, which covers every job, and the tests also run on the
  newest Python.

## 1.1.1

- **Epic (MyChart) connects for real.** Syntropy Health is registered with Epic, so choosing an Epic health system
  takes you to its MyChart sign-in, with no client ID to set up. Health systems receive the app from Epic over time;
  until yours has it, signing in there won't work yet. Settings → Developer still switches Epic to
  Simulated or Sandbox.

## 1.1.0

### Insights across your sources
- The Overview opens with **What's changed**: a number drifting from your own normal (resting heart rate, HRV, sleep,
  steps, weight…), how your sleep lines up with the mood, energy and stress you check in with, symptoms against the
  night before, training-load jumps and HRV after hard days, lab results moving further out of range, measurements
  before and after a medication started, and goals kept. Each says what lined up, with its numbers, never a diagnosis.
- **Compare** under every Trends chart: any two measures day by day (wearables, check-ins, workouts), the same day, the
  day before or the next, with a scatter plot and a plain-language summary.
- Trends charts mark when medications started and conditions were diagnosed, including lab charts.
- **Goals** for any daily measure (at least or at most): a line on the chart, days met, streaks, a Goals card and
  progress on the Overview's number tiles.
- Trends goes back as far as your data (All), in weekly or monthly averages for long spans.

### Sleep and training
- **Sleep, night by night**: stages for each night, bedtime and wake time and how much they vary, the latest night
  stage by stage, and how your nights divide up against typical ranges.
- **Workouts**: training load this week against your usual (heart-rate-weighted), heart-rate zones, weekly volume,
  pace and heart rate over time and best efforts for each kind of workout, an icon per kind, and pace, zones and
  splits in each workout.

### Journal, Timeline and Ask
- The Journal shows patterns with your sleep and training, and offers your current medications when you log a dose.
- The Timeline includes symptoms, doses, notes and device alerts (ECGs, notifications) alongside records, with filters
  for workouts and check-ins.
- Ask keeps past conversations (**History**) on the server, per person and account, so they follow you between the
  computer and the iPhone app. The conversation kept in this browser before becomes the first one.
- AI apps get new tools: `get_insights`, `compare_measures`, `get_sleep_nights`, `get_training_summary`, `get_goals`.
- On phones, Journal is in the tab bar; Records moved under More.

### Updates and installing
- **Update notices**: once a day Syntropy Health asks GitHub whether a newer release is out (nothing about you or your
  records is sent) and tells the server's owner in Alerts and **Settings → General → Updates**, with how to update this
  installation. Checks can be turned off there, or for good with `SYNTROPY_UPDATE_CHECK=false`.
- The Mac and Windows apps **install updates themselves**: from Settings, from **Check for Updates…** in the app and
  menu bar or tray menus, or automatically while the window is closed with **Install updates automatically** on. The
  download is checked against the release's SHA-256 checksums (and, once the apps are signed, the developer's
  signature) before it's installed, and the app opens again the way it was.
- The Linux installer adds a daily timer that installs new releases when automatic updates are on.
- Installers and `syntropy-health update` install the **latest release** instead of the newest code on `main`.
- A **Docker image** for amd64 and arm64 (Raspberry Pi, ARM NAS boxes) at `ghcr.io/enriqueneyra/syntropy-health`:
  `docker compose pull` updates it, and no clone of the repository is needed.
- Releases include the Python wheel and `SHA256SUMS`, and a release's tag has to match the version in the code.

### Project
- A shorter README, with detailed install and configuration moved to [the setup guides](https://health.syntropylabs.io/setup/), a
  code of conduct, and richer package metadata.
- Release files and the Docker image carry signed build provenance (`gh attestation verify`, see
  [SECURITY.md](.github/SECURITY.md)), and the relay's `/health` names the commit it's running.
- Python 3.12 or newer is required. Dependencies are listed once, in `pyproject.toml`: `pip install -e ".[desktop]"`
  for the desktop app, `pip install --group dev` for tests and linting, and `./run.sh lint`.
- When a sign-in server can't be reached or an update fails unexpectedly, Settings shows a plain sentence; the details
  go to the log.

### Removed
- The upgrade for databases from the prototype that came before 1.0.
- The relay's older hand-off that put tokens in the address's query string.

### Ask
- Answers stream in as they're written, from API keys (Anthropic and OpenAI-compatible services, including models on
  your own computer) and from Claude Code and Gemini CLI. Text appears word by word with a caret, at a pace that keeps
  up with the model; tables appear a row at a time and bold or code never flashes raw symbols. Codex CLI answers arrive
  whole and are revealed the same way.
- **Stop** ends an answer mid-way (and the AI app running it on this computer); what was written so far is kept.
- Messages and lookups ease in; the conversation follows the answer unless you've scrolled up to read.
- The conversation fills the window and scrolls on its own: no second scrollbar for the page, in the browser and in
  the Mac app. A saved conversation opens at its latest message.

### Motion and finishing touches
- Switching sections cross-fades the window while the new page slides in; tabs and filters within a page cross-fade.
- Light and dark cross-fade instead of snapping, and changing the theme or accent no longer redraws the page or jumps
  back to the top. The system switching between light and dark fades too.
- Dialogs, drawers, menus and toasts ease in and out (toasts can be clicked away); buttons and chips respond to a
  press; keyboard focus moves into a dialog and returns to where it was when it closes.
- Charts draw themselves in the first time they appear: lines trace across, bars grow, sparklines wipe in. Redraws with
  fresh data don't replay it.
- Scrollbars are thin and follow the theme where the browser allows it.
- Everything above is turned off for people who ask their system for reduced motion.

### Fixed
- Settings → General → Time zone showed the first zone in the list (Africa/Abidjan) on a server running on UTC, and
  saving the page would have switched to it. UTC is now listed first, and the zone in use always appears.
- Lab results keep the precision the lab reported: a series reads 5.9, 6.0, 6.1 % rather than 5.9, 6, 6.1 %, and the
  reference range matches (4.0–5.6 %).
- The last date on daily bar charts no longer overlaps the one before it.
- The sleep chart's average and tooltips read in hours and minutes (avg 7h 13m) like the rest of the page, and a
  duration can no longer round to "7h 60m".
- Lab insights keep the lab's precision ("up from 6.0 %", not "6 %").
- The Overview's vitals card link reads "Vital trends" instead of "Vital trends 8".
- The visit summary capitalizes allergy criticality and insurance status.
- The step dots on first-run setup line up with the wordmark.
- Signing in again after locking no longer starts a second copy of the background alert checks.

### Improved
- When the server can't be reached (it's off, asleep, or you're on another network), pages say so in words and offer
  **Try again**, instead of "Failed to fetch". The start screen retries on its own when the network comes back, and a
  page whose code didn't load after an update offers **Reload**.
- Unexpected server errors answer with a sentence the app can show and are logged with their details, rather than a bare
  "Internal Server Error".
- `/` and `/health` answer `HEAD` requests, for uptime monitors (Uptime Kuma, Healthchecks and the like).

### Security
- The Content-Security-Policy now covers every address that shows the app, not only `/` (other addresses fall back to
  the same page), and the OAuth hand-off page at `/callback` gets its own strict policy allowing only its script, by
  hash.
- `Permissions-Policy` turns off the camera, microphone, location, payment and other browser features the app never
  uses; `Cross-Origin-Opener-Policy: same-origin` isolates its window.

### Project
- [SECURITY.md](.github/SECURITY.md): how to report a vulnerability privately, and what's in scope.
- Issue forms for bugs and ideas (with a reminder never to post health data), a pull request checklist, and Dependabot
  for Python packages and GitHub Actions.
- CI lints the Python code with Ruff (settings in `pyproject.toml`).

## 1.0.0

The first release: a self-hosted personal health record.

- **Health systems** through SMART on FHIR patient access (Epic, Oracle Health, eClinicalWorks and any FHIR R4 server),
  with PKCE, paged download, normalization to one set of units and de-duplication across institutions. A built-in EHR
  simulator with synthetic patients runs the whole pipeline until vendor registrations are in place.
- **Wearables**: Oura, WHOOP and Google Health (Fitbit, Pixel Watch), through a relay that completes the sign-in and
  keeps nothing; Apple Health through the iPhone app or an export file. Daily roll-ups choose one device per day.
- **Pages**: a customizable Overview, Ask, Trends (labs, vitals, heart, sleep, activity, body), Workouts, Journal,
  Timeline, Records, Sources, a printable visit summary and Settings.
- **Lab reports** from PDFs and photos of paper reports, read on this computer with Tesseract.
- **Households**: each person signs in with their own password and shares with others to view or edit.
- **AI**: models on your own computer, your own API key, Claude Code or Codex already signed in, or any MCP app, all
  read-only and logged.
- **Export**: FHIR R4 bundles, CSV, and full database backups.
- **Installs**: Mac and Windows apps, a Linux home-server installer (systemd), Docker, and `uv`/`pipx`.
