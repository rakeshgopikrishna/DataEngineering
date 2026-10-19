AzCopy Runner
=============

A small local web form for running azcopy transfers. You can run several transfers at the same time.
Works on macOS, Linux, and Windows (through Git Bash). Started from bash; the form is served by a
small Python 3 script that only uses Python's built-in modules (nothing to pip install).

REQUIREMENTS
- bash (built into macOS/Linux; on Windows use Git Bash)
- Python 3.8 or newer
    macOS:   if "python3" is missing, run:  xcode-select --install
    Windows: https://www.python.org/downloads/  (tick "Add python.exe to PATH")
- AzCopy v10: https://learn.microsoft.com/azure/storage/common/storage-use-azcopy-v10
    Unzip it and copy the azcopy program (azcopy on Mac/Linux, azcopy.exe on Windows) into this
    AzCopyRunner folder. Or keep it elsewhere and enter its path under "Settings" in the page.
    Mac/Linux: if it says "not executable", run:  chmod +x azcopy

START
- macOS:   open Terminal, then run:   bash /path/to/AzCopyRunner/start.sh
           (tip: type "bash " and drag start.sh into the Terminal window, then press Enter)
           Or double-click "Start AzCopyRunner.command". If macOS refuses, run once in Terminal:
               chmod +x "/path/to/AzCopyRunner/Start AzCopyRunner.command"
- Windows: right-click the folder > "Open Git Bash here", then run:   bash start.sh
- Linux:   bash start.sh

Your browser opens the form. Keep the Terminal window open while transfers run.

USE
1. Fill in a transfer card:
   - Operation:  Copy (transfer files) or Sync (make the destination match the source)
   - Direction:  Local -> Blob (upload), Blob -> Local (download), Blob -> Blob
   - Source / Destination: a local folder (use Folder... / File...) or a blob URL
       https://<account>.blob.core.windows.net/<container>/<optional-folder>
   - SAS tokens: paste the SAS for each blob side separately (with or without the leading "?").
       Source needs Read + List. Destination needs Write + Create + List (plus Delete if you
       sync with "Delete them").
   - Options: every dropdown shows what it does and which azcopy flag it sets.
2. Click "Test connection". Nothing is copied yet. It checks:
   - azcopy is found and runs
   - local folders exist and can be read / written
   - each SAS token: expiry date and permissions written inside the token
   - each blob URL: connects to the storage account with the token and lists a few items
     (catches wrong tokens, wrong account/container names, firewall / network / proxy problems)
   The form is locked while you review the results, so what runs is exactly what was tested.
3. Review the results and the "Command that will run", then click "Start transfer"
   ("Back to edit" to change something). If the test found problems the button becomes
   "Start anyway" and asks you to confirm.
4. Click the round + button (or "+ New transfer") to add another card and start it as well.
   Each card runs its own azcopy process in parallel. "Duplicate" copies a card, including its tokens.

Note: the connection test talks to Azure from Python, not from azcopy. Rarely, company SSL
inspection or proxies treat the two differently; such cases show as a warning, not a failure.

AUTHENTICATION (per transfer card)
- SAS tokens (default) - as described above.
- My Microsoft account (Entra ID) - no tokens at all. Open Settings > "Azure sign-in", click
  "Sign in to Azure", open the link shown and enter the code. azcopy remembers the sign-in.
  Your account needs the "Storage Blob Data Reader" (source) / "Storage Blob Data Contributor"
  (destination) role on the storage account.
- Service principal - tenant ID, application (client) ID and client secret. The secret is handed
  to azcopy through its environment, NOT on the command line, so it does not show in the process
  list. It is never saved. The connection test checks the sign-in with Microsoft Entra ID first.
For these two modes the connection test runs "azcopy list" with the same sign-in azcopy will use.

ADLS GEN2 (DATA LAKE)
Both https://<account>.blob.core.windows.net/... and https://<account>.dfs.core.windows.net/...
addresses work in every field.

MORE OPTIONS (collapsed section on each card - the defaults change nothing)
- Only include / Skip files named      --include-pattern / --exclude-pattern, e.g. *.parquet;*.csv
- Folder placement (copy)              --as-subdir=false copies a folder's contents straight in
- Access tier, properties (Blob->Blob) --s2s-preserve-access-tier / --s2s-preserve-properties
- MD5 checksum (upload / download)     --put-md5 / --check-md5
- Parallel connections                 AZCOPY_CONCURRENCY_VALUE
- Connection test: "Also count files and total size" shows how much will be copied before you start.
- After the transfer: "Check that every file arrived" counts the files at the source and the
  destination (with the same include/skip filters) and shows whether anything is missing.
  It compares counts, not file contents (use the MD5 options for contents).

RESUME
If a transfer fails or you stop it, the card shows "Resume". It continues the same azcopy job
where it stopped (azcopy jobs resume), passing the tokens or sign-in again from the card.

RUN RECORD
Every transfer also writes <log name>.run.json next to its log: who ran it, on which computer,
when, source and destination (without tokens), all options, the result and the post-copy check.
Useful as an audit trail. Tokens and secrets are never written to it.

SECURITY
- SAS tokens are never saved to disk. They exist only in the open page and in the running azcopy process.
- The token boxes are masked text fields, so browsers do not offer to save tokens in their
  password manager.
- Tokens are shown as sig=REDACTED in the command preview, the Terminal, and the saved logs.
- The page is reachable only from this computer (localhost). The browser is opened with a private
  session key in the address (#k=...); without it the page cannot do anything. Don't share that link.
- The page cannot be embedded in other websites.
- Logs are kept outside cloud-synced folders (OneDrive, Dropbox, iCloud...) - see LOGS.
- Important: azcopy only accepts SAS tokens inside the URL on its command line. While a transfer
  runs, the full URL with the token is visible in the process list:
    macOS / Linux: to ANY user logged in to the same computer (ps aux)
    Windows:       to the same user and to administrators
  Use short-lived, least-privilege tokens, and avoid shared / multi-user machines.
  For the strongest setup use "azcopy login" (Microsoft Entra ID) and leave the SAS boxes empty.
- Keep azcopy in a folder only you (or IT) can change. A replaced azcopy would receive the tokens.

LOGS
Logs list storage account / container names and file paths, so they are kept out of synced folders:
- If AzCopyRunner is inside a synced folder (OneDrive, Dropbox, iCloud, ...), logs go to
  <your home folder>/AzCopyRunner-logs. Otherwise they go to AzCopyRunner/logs.
  The Terminal shows the exact folder on start, and each card shows its log file.
  Choose another place with:   bash start.sh --log-dir /path/to/logs
- <time>_<name>_jobN.log   azcopy output for each transfer
- <time>_<name>_jobN.run.json   run record (see RUN RECORD)
- azcopy/                  azcopy's own detailed logs
- If you stop a transfer, you can resume it later from a terminal:
     azcopy jobs resume <job id> --source-sas "<sas>" --destination-sas "<sas>"
  The job id is shown on the card.

TROUBLESHOOTING
- "azcopy not found": open Settings in the page and enter the full path to azcopy.
- "Can't reach the AzCopy Runner server": the Terminal window was closed. Start it again.
- "Opened without its session key": open the link printed in the Terminal (ending in #k=...).
- If you close the Terminal while transfers run, azcopy keeps running in the background and
  writes to the logs folder.
- Port 8765 busy? It tries 8765-8775 automatically. To choose another port:
     bash start.sh --port 9000
