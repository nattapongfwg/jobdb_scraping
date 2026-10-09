# Moving the Recruitment Board to a Server

Written 2026-10-09. Planning document; no server has been chosen yet.
Online copy: https://claude.ai/code/artifact/27f0b58f-d89a-447d-b507-08e53484ffab

## Summary

The Recruitment Board can move to a server; the easiest target is a Windows server that IT keeps switched on, and the move takes about one working day once the server exists.

Today the board runs on one office PC (`E:\jobdb_multiuser`, port 2777). HR colleagues open it over the office network at http://10.33.10.51:2777. It only works while that PC is on and its owner is logged in, and that PC's address can change.

A server fixes this: it stays on day and night, has a fixed address, and is backed up by IT. Nothing changes for HR users except the address they type. No server has been chosen yet, so this document lists what the board needs, what to ask IT, and the steps for moving.

## What has to move

The board is more than one program: seven parts move together, and three of them need a one-time sign-in on the server.

| Part | What it is | Size today | On the server |
| --- | --- | --- | --- |
| Web app | The Python program HR opens in the browser | Small | Copied, plus its Python packages |
| Database | SQL Server database `jobdb_multiuser`: candidates, jobs, requests, users, email templates | 1,266 candidates | Restored from a backup |
| Résumés | PDF files downloaded from JobDB, or added by hand | 1,296 files, 413 MB | Copied |
| Forms and attachments | Team evaluation forms, exam attachment, generated Evaluation and Report files | About 10 MB | Copied |
| Settings (`.env`) | Passwords and keys for SEEK, OpenAI and Microsoft mail | 1 file | Copied; keep private |
| SEEK session | The scraper's saved browser login | 720 MB | Sign in to SEEK once on the server |
| Recruit mailbox sign-in | Lets the board send exams and find replies | 1 file | An Admin signs in once on the Email Templates page |

The shared OneDrive folders (Shortlists, Email_Reply_Exam) stay where they are, but the server must be able to write to them (see Changes needed).

## Server options

Recommended: a Windows server run by IT. The board was built on Windows, so it moves with the fewest changes.

| Option | Effort to move | Good | Watch out |
| --- | --- | --- | --- |
| Windows server run by IT (recommended) | Low | Always on, fixed address, IT backs it up | IT must approve installing Python and SQL Server, or give a database on an existing SQL Server |
| Always-on Windows PC in the office | Low | No IT project; could start next week | Someone must keep it on, logged in and updated; no IT backups unless arranged |
| Linux server | High | Common and cheap for IT | Startup, file paths and OneDrive handling must be rewritten; SQL Server on Linux works but is less familiar |
| Cloud (Azure, AWS) | High | Reachable from anywhere | Monthly cost, internet security, HTTPS required; candidate data leaves the office |

If IT can't provide a server soon, the always-on office PC is a good stepping stone: the same steps apply, and moving again later to an IT server is the same job.

## Questions to ask IT

Bring this list to the first conversation with IT; their answers decide which option above applies.

- [ ] Can IT provide a server (or a virtual machine) for an internal HR web app? Windows or Linux?
- [ ] What does it need: 2 CPU cores, 4 GB RAM and 20 GB of disk are plenty for today's data (about 1.2 GB) plus growth.
- [ ] Is there an existing SQL Server we can get a database on, or may we install SQL Server Express on the server?
- [ ] Which account will the board run as? It needs a Windows account (a "service account") that can log in to SQL Server and write to the shared Recruit OneDrive / SharePoint library.
- [ ] Can that account sync the "Recruit's files - Recruitment" library with OneDrive, or should the board upload files through Microsoft 365 instead?
- [ ] What address will HR use? Ask for a fixed name such as `recruitment.<company domain>` or at least a fixed IP, and which port is allowed (2777 today).
- [ ] Is the server reachable only inside the office network / VPN? (Recommended: yes.)
- [ ] Does IT require HTTPS (a security certificate) for internal sites?
- [ ] Who backs up the server and the database, and how often? Who installs Windows updates?
- [ ] May the server open the SEEK employer website and the OpenAI API on the internet (the scraper and AI summaries need both)?
- [ ] How do we get remote access to install and update the board (Remote Desktop)?

## Changes needed in the app

Three things were built for "one PC with someone logged in" and need work before the move; the first is required, the other two depend on IT's answers.

| Change | Why | Size |
| --- | --- | --- |
| Start without anyone logged in (required) | Today the board starts when its owner logs on to Windows. A server usually has nobody logged on, so it must start with the machine, as a Windows service or a task set to "run whether user is logged on or not". | Small: a change to `service.ps1` |
| OneDrive folders | Shortlists and exam replies are saved into a OneDrive folder synced on the PC. On a server, either that account syncs the library too, or the board uploads through Microsoft 365 directly. | None if syncing works; medium if uploads must replace it |
| HTTPS | The board uses plain HTTP, so passwords travel unencrypted. Fine inside the office network; required if IT asks for it or the board is ever opened from outside. | Small to medium: IT usually adds it in front of the board |

Also worth doing first, though not required: merge the code into the `master` branch so the server installs from the main branch, and set up a separate test copy so tests never touch real data.

## Move plan

The move runs in three stages: prepare while the board keeps running, install and test on the server, then switch over in one short quiet window (about an hour).

**Before: prepare (board keeps running)**

1. Agree the option with IT and get the answers to the questions above.
2. Make the app changes that apply (start without login; OneDrive; HTTPS).
3. Merge the code into `master` and push it to GitHub, so the server installs a known version.

**Install and test on the server (board keeps running on the PC)**

4. Install Python, ODBC Driver 18 for SQL Server, Git and SQL Server (or get a database from IT).
5. Download the code from GitHub, create the Python environment, and install the scraper's browser.
6. Restore a test copy of the database and run the board on the server with a test name; check pages open from another PC.

**Switch-over day (about 1 hour, board unavailable)**

7. Tell HR the board is down for an hour; stop the board on the PC.
8. Back up the database and restore it on the server.
9. Copy résumés, forms, attachments and `.env` to the server; update résumé file paths in the database to the new folder.
10. Start the board as a service on the server; sign in to the Recruit mailbox and to SEEK once.
11. Check: sign in, open a candidate's résumé, send a test exam email to yourself, run one scrape.
12. Send HR the new address; update the desktop launcher; remove the board's task from the PC.

The same steps were used on 2026-10-09 to move from the single-user board to this one (see `MULTIUSER.md` §0 and `debug\migrate_from_live.py`), so they are tested on a smaller scale.

## Risks and rollback

The move is low-risk because the PC keeps its copy: if anything fails on switch-over day, the board is started on the PC again within minutes.

| Risk | What happens | How to handle it |
| --- | --- | --- |
| OneDrive can't be synced on the server | Shortlist folders and exam replies can't be saved | Ask IT early; fall back to uploads through Microsoft 365 |
| SEEK blocks or questions a login from a new machine | Scraping stops until someone confirms the login | Sign in to SEEK by hand on the server during switch-over |
| Work done on the PC after the backup | Lost on the server | Stop the board on the PC before the final backup (step 7) |
| `.env` passwords exposed | Someone could use the SEEK or OpenAI accounts | Keep `.env` readable only by the service account and Admins |

**Rollback:** stop the board on the server and start it again on the PC with `service.ps1 install`. Anything done on the server after switch-over is not in the PC's copy.

**What stays the same for HR:** same accounts and passwords, same pages, same candidates and history. Only the address changes.

## Words used in this document

| Word | Meaning |
| --- | --- |
| Server | A computer that stays on all the time so others can use what runs on it. Often a virtual machine inside IT's hardware. |
| Virtual machine (VM) | A "computer inside a computer" that IT creates; it behaves like a normal server. |
| SQL Server | Microsoft's database program; it stores the board's candidates, jobs and users. Express is its free edition. |
| Service / service account | A program that starts with the computer, and the Windows account it runs under. |
| Port | The number after the colon in an address (`:2777`); one machine can run several web apps on different ports. |
| HTTPS | The padlock in the browser: traffic is encrypted. Plain HTTP is not. |
| `.env` | The board's settings file, holding passwords and keys. Never shared or put in Git. |
| Git / GitHub / master | Git keeps the code's history; GitHub stores a copy online; `master` is the main version of the code. |
| Backup / restore | Saving a full copy of the database to a file, and loading that file into a database elsewhere. |
