#!/usr/bin/env python3
"""AzCopy Runner - local web form for running several azcopy transfers in parallel.

Serves http://localhost:<port>/ only. SAS tokens stay in memory and are never written to disk.
Standard library only; works on macOS, Linux and Windows.
"""
import argparse
import fnmatch
import getpass
import html
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.json"


def in_synced_folder(path):
    """True when a path is inside a cloud-synced folder (OneDrive, Dropbox, iCloud, ...)."""
    s = str(path).lower()
    roots = [os.environ.get(v, "") for v in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")]
    if any(r and s.startswith(r.lower()) for r in roots):
        return True
    return any(m in s for m in ("onedrive", "dropbox", "google drive", "googledrive",
                                "mobile documents", "icloud", "box sync", "sharepoint"))


# Logs list account/container names and file paths, so keep them out of synced folders by default.
LOG_DIR = Path.home() / "AzCopyRunner-logs" if in_synced_folder(ROOT_DIR) else ROOT_DIR / "logs"
INDEX_PATH = APP_DIR / "index.html"

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
PLATFORM = "windows" if IS_WIN else "mac" if IS_MAC else "linux"
EXE_NAME = "azcopy.exe" if IS_WIN else "azcopy"

SESSION_KEY = secrets.token_hex(16)
SIG_RE = re.compile(r"(?i)(sig=)[^&\"'\s]+")

jobs = {}
jobs_lock = threading.Lock()
browse_lock = threading.Lock()
next_id = 1


# ---------------------------------------------------------------- helpers

def hide_secrets(s):
    return SIG_RE.sub(r"\1REDACTED", s or "")


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"azcopyPath": ""}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def resolve_azcopy(preferred):
    if preferred:
        p = Path(preferred.strip().strip('"')).expanduser()
        if p.is_dir():
            p = p / EXE_NAME
        return str(p) if p.is_file() else None
    # azcopy dropped into this folder, or the unzipped azcopy_<os>_<arch>_x.y.z folder
    for pattern in (EXE_NAME, f"*/{EXE_NAME}", f"*/*/{EXE_NAME}"):
        for p in sorted(ROOT_DIR.glob(pattern)):
            if p.is_file() and LOG_DIR not in p.parents:
                return str(p)
    return shutil.which("azcopy")


def azcopy_status():
    cfg = load_config()
    exe = resolve_azcopy(cfg.get("azcopyPath", ""))
    version, error = None, None
    if exe:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20, **hidden_window())
            version = (r.stdout or r.stderr).strip() or None
        except PermissionError:
            error = f'{exe} is not executable. Run:  chmod +x "{exe}"'
        except Exception as e:
            error = f"Could not run {exe}: {e}"
    return {"azcopyPath": cfg.get("azcopyPath", ""), "resolved": exe, "version": version,
            "error": error, "platform": PLATFORM}


def hidden_window():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}


def split_args(s):
    """Split extra arguments like a shell would, but keep Windows backslashes intact."""
    tokens = re.findall(r"""(?:"[^"]*"|'[^']*'|[^\s"'])+""", s)
    unquote = re.compile(r""""([^"]*)"|'([^']*)'""")
    return [unquote.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(2), t) for t in tokens]


def show_command(argv):
    line = subprocess.list2cmdline(argv) if IS_WIN else shlex.join(argv)
    return "azcopy " + hide_secrets(line)


def join_sas(url, sas):
    url, sas = url.strip(), (sas or "").strip()
    if not sas:
        return url
    sas = sas.lstrip("?")
    if re.search(r"[?&]sig=", url, re.I):   # URL already carries a SAS
        return url
    return f"{url}&{sas}" if "?" in url else f"{url}?{sas}"


def local_path(p):
    return os.path.expanduser(p.strip().strip('"').strip("'"))


def build_args(j):
    """Turn the form values into an azcopy argv, plus validation messages."""
    errors, warnings = [], []
    op = "sync" if j.get("operation") == "sync" else "copy"
    direction = j.get("direction")
    if direction not in ("local-blob", "blob-local", "blob-blob"):
        direction = "local-blob"
    src_blob = direction in ("blob-local", "blob-blob")
    dst_blob = direction in ("local-blob", "blob-blob")
    src = (j.get("source") or "").strip()
    dst = (j.get("destination") or "").strip()
    auth = auth_mode(j)
    if auth == "spn":
        for field, label in (("spnTenant", "tenant ID"), ("spnClient", "application (client) ID"),
                             ("spnSecret", "client secret")):
            if not (j.get(field) or "").strip():
                errors.append(f"Service principal {label} is empty.")

    if not src:
        errors.append("Source is empty.")
    elif src_blob:
        if not src.startswith("https://"):
            errors.append("Source must be a blob URL starting with https://")
        elif auth == "sas" and not (j.get("sourceSas") or "").strip() and not re.search(r"[?&]sig=", src, re.I):
            warnings.append('No source SAS token - azcopy will use your "azcopy login" session, if any.')
    else:
        src = local_path(src)
        if "*" not in src and not os.path.exists(src):
            errors.append(f"Source path not found: {src}")

    if not dst:
        errors.append("Destination is empty.")
    elif dst_blob:
        if not dst.startswith("https://"):
            errors.append("Destination must be a blob URL starting with https://")
        elif auth == "sas" and not (j.get("destSas") or "").strip() and not re.search(r"[?&]sig=", dst, re.I):
            warnings.append('No destination SAS token - azcopy will use your "azcopy login" session, if any.')
    else:
        dst = local_path(dst)
        if op == "sync" and not os.path.isdir(dst):
            errors.append(f"Sync needs the destination folder to exist: {dst}")

    if src_blob and auth == "sas":
        src = join_sas(src, j.get("sourceSas"))
    if dst_blob and auth == "sas":
        dst = join_sas(dst, j.get("destSas"))

    argv = [op, src, dst, "--recursive=" + ("false" if j.get("recursive") == "false" else "true")]
    if op == "copy":
        ow = j.get("overwrite") if j.get("overwrite") in ("true", "false", "ifSourceNewer") else "true"
        argv.append(f"--overwrite={ow}")
    else:
        dd = "true" if j.get("deleteDestination") == "true" else "false"
        argv.append(f"--delete-destination={dd}")
        if dd == "true":
            warnings.append("Files at the destination that are not in the source will be DELETED.")
    if j.get("dryRun") == "true":
        argv.append("--dry-run")
        warnings.append("Dry run: nothing will be transferred.")
    if j.get("logLevel") in ("INFO", "WARNING", "ERROR", "NONE"):
        argv.append(f"--log-level={j['logLevel']}")
    cap = str(j.get("capMbps") or "")
    if cap.isdigit() and int(cap) > 0:
        argv.append(f"--cap-mbps={cap}")

    # Optional extras - each one is only added when the user picks a non-default value.
    for field, flag in (("includePattern", "--include-pattern"), ("excludePattern", "--exclude-pattern")):
        pattern = (j.get(field) or "").strip().strip('"')
        if pattern:
            argv.append(f"{flag}={pattern}")
    if op == "copy" and j.get("asSubdir") == "false":
        argv.append("--as-subdir=false")
    if op == "copy" and direction == "blob-blob":
        if j.get("preserveTier") == "false":
            argv.append("--s2s-preserve-access-tier=false")
        if j.get("preserveProps") == "false":
            argv.append("--s2s-preserve-properties=false")
    if direction == "local-blob" and j.get("putMd5") == "true":
        argv.append("--put-md5")
    if direction == "blob-local" and j.get("checkMd5") in ("FailIfDifferentOrMissing", "LogOnly", "NoCheck"):
        argv.append(f"--check-md5={j['checkMd5']}")

    extra = (j.get("extraArgs") or "").strip()
    if extra:
        argv += split_args(extra)

    env, env_note = job_env(j)
    command = show_command(argv) + (f"   [{env_note}]" if env_note else "")
    return {"argv": argv, "env": env, "command": command, "errors": errors, "warnings": warnings}


def auth_mode(j):
    return j.get("auth") if j.get("auth") in ("login", "spn") else "sas"


def job_env(j):
    """Environment variables for azcopy (secrets travel here, not on the command line). Plus a safe description."""
    env, notes = {}, []
    conc = str(j.get("concurrency") or "")
    if conc.isdigit() and int(conc) > 0:
        env["AZCOPY_CONCURRENCY_VALUE"] = conc
        notes.append(f"AZCOPY_CONCURRENCY_VALUE={conc}")
    if auth_mode(j) == "spn":
        env.update({
            "AZCOPY_AUTO_LOGIN_TYPE": "SPN",
            "AZCOPY_TENANT_ID": (j.get("spnTenant") or "").strip(),
            "AZCOPY_SPA_APPLICATION_ID": (j.get("spnClient") or "").strip(),
            "AZCOPY_SPA_CLIENT_SECRET": (j.get("spnSecret") or "").strip(),
        })
        notes.append("service principal sign-in, secret passed privately")
    elif auth_mode(j) == "login":
        notes.append("uses your 'azcopy login' sign-in")
    return env, ", ".join(notes)


def read_tail(path, max_bytes):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def scrub_file(path):
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        clean = hide_secrets(raw)
        if clean != raw:
            Path(path).write_text(clean, encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- connection test

AZ_ERRORS = {
    "AuthenticationFailed": "The SAS token was rejected: wrong signature, expired, or not issued for this account.",
    "AuthorizationPermissionMismatch": "The SAS token is valid but does not grant the permission this check needs.",
    "AuthorizationFailure": "Access denied. Often a storage firewall / network rule is blocking this computer.",
    "AuthorizationSourceIPMismatch": "This computer's IP address is not allowed by the SAS token (sip).",
    "AuthorizationProtocolMismatch": "The SAS token does not allow HTTPS (spr).",
    "AuthorizationResourceTypeMismatch": "The SAS token does not cover this resource type (srt).",
    "AuthorizationServiceMismatch": "The SAS token is not for the Blob service (ss must include b).",
    "ContainerNotFound": "The container does not exist.",
    "ResourceNotFound": "Nothing exists at this path.",
    "BlobNotFound": "The blob does not exist.",
    "NoAuthenticationInformation": "No SAS token was given and the container is not public.",
    "PublicAccessNotPermitted": "No SAS token was given and public access is disabled on this account.",
    "AccountIsDisabled": "The storage account is disabled.",
}


def check(label, status, detail):
    return {"label": label, "status": status, "detail": hide_secrets(detail)}


def side_sas(url, sas):
    """The SAS query string for one side: from the token box, or already inside the URL."""
    if re.search(r"[?&]sig=", url, re.I):
        return urllib.parse.urlsplit(url.strip()).query
    return (sas or "").strip().lstrip("?")


def parse_sas_time(value):
    value = re.sub(r"\.\d+Z$", "Z", value.strip())
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def check_sas_fields(side, sas, need, nice_to_have):
    """Checks expiry and permissions written inside the token itself (no network needed)."""
    out = []
    q = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(sas, keep_blank_values=True).items()}
    if "sig" not in q:
        return [check(f"{side} SAS token", "error", 'This does not look like a SAS token (no "sig=" part).')]
    now = datetime.now(timezone.utc)
    exp = parse_sas_time(q.get("se", ""))
    start = parse_sas_time(q.get("st", "")) if q.get("st") else None
    if exp is None:
        out.append(check(f"{side} SAS expiry", "warn", "Could not read the expiry time (se) from the token."))
    elif exp <= now:
        out.append(check(f"{side} SAS expiry", "error", f"The token EXPIRED on {exp:%Y-%m-%d %H:%M} UTC."))
    elif exp - now < timedelta(hours=1):
        out.append(check(f"{side} SAS expiry", "warn",
                         f"The token expires soon ({exp:%Y-%m-%d %H:%M} UTC). A long transfer may be cut off."))
    else:
        out.append(check(f"{side} SAS expiry", "ok", f"Valid until {exp:%Y-%m-%d %H:%M} UTC."))
    if start and start > now + timedelta(minutes=5):
        out.append(check(f"{side} SAS start", "error", f"The token is not valid yet (starts {start:%Y-%m-%d %H:%M} UTC)."))

    perms = q.get("sp", "")
    names = {"r": "Read", "a": "Add", "c": "Create", "w": "Write", "d": "Delete", "l": "List"}
    missing = [p for p in need if p not in perms]
    optional = [p for p in nice_to_have if p not in perms]
    have = ", ".join(names.get(p, p) for p in perms if p in names) or "none"
    if missing:
        out.append(check(f"{side} SAS permissions", "error",
                         f"Has: {have}. Missing: {', '.join(names[p] for p in missing)}."))
    elif optional:
        out.append(check(f"{side} SAS permissions", "warn",
                         f"Has: {have}. Missing {', '.join(names[p] for p in optional)}, "
                         "which azcopy may need for this transfer."))
    else:
        out.append(check(f"{side} SAS permissions", "ok", f"Has: {have}."))
    if q.get("ss") and "b" not in q["ss"]:
        out.append(check(f"{side} SAS service", "error", "This account SAS does not include the Blob service (ss=b)."))
    return out


def azure_get(url, method="GET"):
    """Calls Azure Storage directly. Returns (http status, error code, body or error text)."""
    req = urllib.request.Request(url, method=method, headers={"x-ms-version": "2021-08-06"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, None, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read(4096).decode("utf-8", "replace") if method != "HEAD" else ""
        code = e.headers.get("x-ms-error-code")
        m = re.search(r"<AuthenticationErrorDetail>(.*?)</AuthenticationErrorDetail>", body, re.S)
        return e.code, code, html.unescape(m.group(1)) if m else ""
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, socket.gaierror):
            return 0, "DNS", "Storage account address not found. Check the account name, or the network / VPN / DNS."
        if isinstance(reason, ssl.SSLError):
            return 0, "TLS", ("TLS certificate check failed in this test. Company SSL inspection or Python missing "
                              "certificates can cause this (Mac: run Python's 'Install Certificates.command'). "
                              "azcopy may still work.")
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return 0, "TIMEOUT", "No answer from the storage account (timeout). A firewall or proxy may be blocking it."
        return 0, "NET", f"Could not connect: {reason}"
    except (TimeoutError, socket.timeout):
        return 0, "TIMEOUT", "No answer from the storage account (timeout). A firewall or proxy may be blocking it."


def check_blob_side(side, url, sas, must_exist):
    """Lists (at most 5 items) at the given blob URL to prove the token and network path work."""
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.netloc.replace(".dfs.", ".blob.")   # ADLS Gen2 URLs are tested through the blob endpoint
    segments = urllib.parse.unquote(parts.path).strip("/").split("/", 1)
    container = segments[0]
    prefix = (segments[1] if len(segments) > 1 else "").split("*")[0]
    if container:
        query = {"restype": "container", "comp": "list", "maxresults": "5"}
        if prefix:
            query["prefix"] = prefix
        target = f"https://{host}/{urllib.parse.quote(container)}"
        what = f"container '{container}'" + (f", path '{prefix}'" if prefix else "")
    else:
        query = {"comp": "list", "maxresults": "5"}
        target = f"https://{host}/"
        what = f"account '{host.split('.')[0]}'"
    test_url = target + "?" + urllib.parse.urlencode(query) + (("&" + sas) if sas else "")
    status, code, text = azure_get(test_url)
    label = f"{side} connection"

    if status == 200:
        names = [html.unescape(n) for n in re.findall(r"<Name>(.*?)</Name>", text)]
        if names:
            shown = ", ".join(names[:3]) + (" …" if len(names) > 3 else "")
            return [check(label, "ok", f"Connected to {what}. Found: {shown}")]
        if must_exist:
            return [check(label, "warn", f"Connected to {what}, but nothing was found there. Check the path.")]
        return [check(label, "ok", f"Connected to {what} (currently empty).")]

    # A token without List permission (or a blob-level SAS) can still read a single blob: try that first.
    if code in ("AuthorizationPermissionMismatch", "AuthenticationFailed", "AuthorizationResourceTypeMismatch") and prefix:
        blob_url = f"https://{host}/{urllib.parse.quote(container)}/{urllib.parse.quote(prefix)}" + (("?" + sas) if sas else "")
        s2, _, _ = azure_get(blob_url, "HEAD")
        if s2 == 200:
            return [check(label, "ok", f"Connected. The blob '{prefix}' is readable.")]
    if code == "AuthorizationPermissionMismatch":
        return [check(label, "warn", "The token is valid (Azure accepted it), but it cannot list here, "
                                     "so the path could not be confirmed.")]
    if code == "ContainerNotFound" and not must_exist:
        return [check(label, "warn", f"The container '{container}' does not exist yet. azcopy will try to create "
                                     "it, which needs an account-level SAS.")]
    if code == "TLS":
        return [check(label, "warn", text)]
    if not sas and status in (401, 403, 404) and code in (None, "NoAuthenticationInformation", "ResourceNotFound",
                                                          "PublicAccessNotPermitted"):
        return [check(label, "warn", "No SAS token, so this could not be tested. azcopy will use your "
                                     "'azcopy login' session, if any.")]
    message = AZ_ERRORS.get(code, text or f"Azure returned HTTP {status} ({code or 'no error code'}).")
    if text and code in AZ_ERRORS and code.startswith("Authentication"):
        message += f" Detail: {text}"
    return [check(label, "error", message)]


def check_local_side(side, path, op, is_source):
    path = local_path(path)
    label = f"{side} folder"
    if is_source:
        if "*" in path:
            return [check(label, "ok", "Wildcard path - azcopy will match the files.")]
        if not os.path.exists(path):
            return [check(label, "error", f"Not found: {path}")]
        if not os.access(path, os.R_OK):
            return [check(label, "error", f"No permission to read: {path}")]
        if os.path.isdir(path):
            try:
                count = sum(1 for _ in zip(range(1001), os.scandir(path)))
            except OSError as e:
                return [check(label, "error", f"Cannot open folder: {e}")]
            if count == 0:
                return [check(label, "warn", "The folder is empty - nothing to transfer.")]
            return [check(label, "ok", f"Readable. {'1000+' if count > 1000 else count} item(s) at the top level.")]
        return [check("Source file", "ok", f"Readable ({os.path.getsize(path):,} bytes).")]
    if os.path.isdir(path):
        if not os.access(path, os.W_OK):
            return [check(label, "error", f"No permission to write to: {path}")]
        return [check(label, "ok", "Exists and is writable.")]
    if os.path.exists(path):
        return [check(label, "error", f"This is a file, not a folder: {path}")]
    parent = os.path.dirname(os.path.abspath(path))
    if op == "sync":
        return [check(label, "error", "Sync needs the destination folder to exist.")]
    if os.path.isdir(parent) and os.access(parent, os.W_OK):
        return [check(label, "ok", "Does not exist yet - azcopy will create it.")]
    return [check(label, "error", f"Cannot create it: the parent folder is missing or read-only ({parent}).")]


def test_connection(j):
    built = build_args(j)
    op = built["argv"][0]
    direction = j.get("direction") if j.get("direction") in ("local-blob", "blob-local", "blob-blob") else "local-blob"
    checks = []

    st = azcopy_status()
    if st["resolved"] and st["version"]:
        checks.append(check("azcopy", "ok", f"{st['version'].splitlines()[0]} ({st['resolved']})"))
        if in_synced_folder(st["resolved"]):
            checks.append(check("azcopy location", "warn", "azcopy is inside a synced/shared folder. Anyone who can "
                                                           "change files there could replace it. Prefer a protected folder."))
    else:
        checks.append(check("azcopy", "error", st["error"] or "azcopy not found. Set its path in Settings."))

    for e in built["errors"]:
        checks.append(check("Form", "error", e))
    if built["errors"]:
        return {"checks": checks, "command": built["command"]}

    src, dst = (j.get("source") or "").strip(), (j.get("destination") or "").strip()
    auth = auth_mode(j)
    if auth == "login":
        checks += check_login_status()
    elif auth == "spn":
        checks += check_spn_token(j)

    if direction == "local-blob":
        checks += check_local_side("Source", src, op, True)
    elif auth == "sas":
        sas = side_sas(src, j.get("sourceSas"))
        if sas:
            checks += check_sas_fields("Source", sas, need="r", nice_to_have="l")
        checks += check_blob_side("Source", src, sas, must_exist=True)
    else:
        checks += check_blob_with_azcopy("Source", built["argv"][1], built["env"], must_exist=True)

    if direction == "blob-local":
        checks += check_local_side("Destination", dst, op, False)
    elif auth == "sas":
        sas = side_sas(dst, j.get("destSas"))
        if sas:
            need = "cw" + ("l" if op == "sync" else "") + ("d" if j.get("deleteDestination") == "true" and op == "sync" else "")
            nice = "l" + ("r" if op == "sync" or j.get("overwrite") in ("false", "ifSourceNewer") else "")
            checks += check_sas_fields("Destination", sas, need=need, nice_to_have=nice)
        checks += check_blob_side("Destination", dst, sas, must_exist=False)
    else:
        checks += check_blob_with_azcopy("Destination", built["argv"][2], built["env"], must_exist=False)

    # The estimate only needs the source side, so destination problems don't block it.
    if j.get("estimate") == "true" and not any(c["status"] == "error" and not c["label"].startswith("Destination")
                                               for c in checks):
        checks += estimate_source(j, built, direction)

    for w in built["warnings"]:
        if "SAS token" not in w:      # the checks above already cover missing tokens
            checks.append(check("Note", "warn", w))
    return {"checks": checks, "command": built["command"]}


# ---------------------------------------------------------------- listing and counting (estimate + post-copy check)

LIST_RE = re.compile(r"^INFO:\s*(.*?);\s*Content Length:\s*([\d.]+)\s*([KMGTPE]?i?B)?", re.I)
SIZE_UNITS = {"b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4, "pib": 1024 ** 5,
              "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4, "pb": 1000 ** 5}


def human_size(n):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024


def filters_of(j):
    split = lambda s: [p.strip() for p in (s or "").strip().strip('"').split(";") if p.strip()]
    return split(j.get("includePattern")), split(j.get("excludePattern")), j.get("recursive") != "false"


def name_matches(name, include, exclude):
    base = name.rstrip("/").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if include and not any(fnmatch.fnmatchcase(base, p) for p in include):
        return False
    return not any(fnmatch.fnmatchcase(base, p) for p in exclude)


def new_tally():
    return {"count": 0, "bytes": 0, "sample": [], "complete": False, "error": None}


def azcopy_list(url, env_extra, timeout, max_items=None, include=(), exclude=(), recursive=True):
    """Runs 'azcopy list' (works with SAS, sign-in and service principal alike) and tallies the matching files."""
    tally = new_tally()
    exe = resolve_azcopy(load_config().get("azcopyPath", ""))
    if not exe:
        tally["error"] = "azcopy not found."
        return tally
    env = dict(os.environ, AZCOPY_LOG_LOCATION=str(LOG_DIR / "azcopy"), **env_extra)
    proc = subprocess.Popen([exe, "list", url, "--machine-readable", "--running-tally"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, text=True,
                            encoding="utf-8", errors="replace", **hidden_window())
    other, totals = [], {}
    done = threading.Event()

    def reader():
        for raw in proc.stdout:
            line = raw.strip()
            m = LIST_RE.match(line)
            if m:
                name = m.group(1).strip()
                if name.endswith("/") or (not recursive and "/" in name.strip("/")) or not name_matches(name, include, exclude):
                    continue
                tally["count"] += 1
                tally["bytes"] += float(m.group(2)) * SIZE_UNITS.get((m.group(3) or "b").lower(), 1)
                if len(tally["sample"]) < 3:
                    tally["sample"].append(name)
                if max_items and tally["count"] >= max_items:
                    break
            elif line:
                t = re.match(r"(?i)file count:\s*(\d+)", line)
                if t:
                    totals["count"] = int(t.group(1))
                other.append(line)
                del other[:-15]
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    finished = done.wait(timeout)
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        pass
    hit_max = bool(max_items and tally["count"] >= max_items)
    if finished and not hit_max and proc.returncode not in (0, None):
        tally["error"] = hide_secrets(" ".join(other[-4:]) or f"azcopy list exited with code {proc.returncode}.")
    tally["complete"] = finished and not hit_max and proc.returncode == 0
    # Older azcopy builds print sizes differently; fall back to azcopy's own total when nothing was parsed.
    if tally["complete"] and tally["count"] == 0 and totals.get("count") and not include and not exclude and recursive:
        tally["count"] = totals["count"]
    return tally


def count_local(path, timeout, include=(), exclude=(), recursive=True):
    tally = new_tally()
    deadline = datetime.now() + timedelta(seconds=timeout)
    if os.path.isfile(path):
        if name_matches(path, include, exclude):
            tally.update(count=1, bytes=os.path.getsize(path), sample=[os.path.basename(path)])
        tally["complete"] = True
        return tally
    if not os.path.isdir(path):
        tally["error"] = f"Not found: {path}"
        return tally
    for root, dirs, files in os.walk(path):
        if not recursive:
            dirs[:] = []
        for f in files:
            if name_matches(f, include, exclude):
                tally["count"] += 1
                try:
                    tally["bytes"] += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
                if len(tally["sample"]) < 3:
                    tally["sample"].append(f)
        if datetime.now() > deadline:
            return tally
    tally["complete"] = True
    return tally


def count_target(target, is_blob, env, j, timeout):
    include, exclude, recursive = filters_of(j)
    if is_blob:
        return azcopy_list(target, env, timeout, include=include, exclude=exclude, recursive=recursive)
    return count_local(target, timeout, include, exclude, recursive)


def strip_query(url):
    return url.split("?", 1)[0]


def check_login_status():
    exe = resolve_azcopy(load_config().get("azcopyPath", ""))
    if not exe:
        return []
    signed_in, text = azcopy_login_status(exe)
    if signed_in:
        return [check("Sign-in", "ok", "Signed in with 'azcopy login'.")]
    return [check("Sign-in", "error", "Not signed in. Open Settings at the top of the page and click 'Sign in to Azure'."
                  + (f" ({text})" if text else ""))]


def check_spn_token(j):
    """Asks Microsoft Entra ID for a storage token, so a wrong ID or secret shows up before the copy."""
    tenant = (j.get("spnTenant") or "").strip()
    if not re.fullmatch(r"[\w.-]+", tenant):
        return [check("Service principal", "error", "The tenant ID does not look valid.")]
    data = urllib.parse.urlencode({
        "client_id": (j.get("spnClient") or "").strip(), "client_secret": (j.get("spnSecret") or "").strip(),
        "scope": "https://storage.azure.com/.default", "grant_type": "client_credentials"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=data), timeout=20):
            return [check("Service principal", "ok", "Signed in to Microsoft Entra ID (token issued).")]
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode("utf-8", "replace")).get("error_description", "")
        except Exception:
            desc = ""
        return [check("Service principal", "error",
                      "Sign-in rejected: " + (desc.splitlines()[0] if desc else f"HTTP {e.code}"))]
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        return [check("Service principal", "warn", f"Could not reach Microsoft Entra ID to test the sign-in ({e}).")]


def check_blob_with_azcopy(side, url, env, must_exist):
    label = f"{side} connection"
    r = azcopy_list(url, env, timeout=60, max_items=3)
    where = strip_query(url)
    if r["error"]:
        msg = r["error"]
        if not must_exist and re.search(r"ContainerNotFound|404", msg):
            return [check(label, "warn", "The container does not exist yet. azcopy will try to create it.")]
        if re.search(r"403|AuthorizationPermissionMismatch|not authorized", msg, re.I):
            msg = ("Access denied. The account needs the 'Storage Blob Data Reader' (source) or "
                   "'Storage Blob Data Contributor' (destination) role, or a firewall blocks this computer. " + msg)
        return [check(label, "error", msg)]
    if r["count"]:
        return [check(label, "ok", f"Connected to {where}. Found: " + ", ".join(r["sample"]))]
    if not r["complete"]:
        return [check(label, "warn", f"No answer from {where} within 60 seconds.")]
    if must_exist:
        return [check(label, "warn", f"Connected to {where}, but nothing was found there. Check the path.")]
    return [check(label, "ok", f"Connected to {where} (currently empty).")]


def estimate_source(j, built, direction):
    label = "Source size"
    if direction == "local-blob":
        r = count_target(built["argv"][1], False, {}, j, timeout=120)
    else:
        r = count_target(built["argv"][1], True, built["env"], j, timeout=180)
    if r["error"]:
        return [check(label, "warn", f"Could not count the files: {r['error']}")]
    include, exclude, _ = filters_of(j)
    note = " (include/exclude filters applied)" if include or exclude else ""
    if not r["complete"]:
        return [check(label, "warn", f"At least {r['count']:,} files, {human_size(r['bytes'])} - stopped counting "
                                     f"at the time limit{note}.")]
    return [check(label, "ok", f"{r['count']:,} files, {human_size(r['bytes'])}{note}.")]


# ---------------------------------------------------------------- post-copy check

def run_verify(j):
    built = build_args(j)
    op, src, dst = built["argv"][0], built["argv"][1], built["argv"][2]
    direction = j.get("direction")
    src_blob, dst_blob = direction in ("blob-local", "blob-blob"), direction in ("local-blob", "blob-blob")
    env = built["env"]

    s = count_target(src, src_blob, env, j, timeout=3600)
    if s["error"] or not s["complete"]:
        return {"status": "warn", "detail": "Could not count the source files: " + (s["error"] or "time limit reached.")}

    # Single-file sources: azcopy's own summary already says whether that file arrived.
    if (not src_blob and os.path.isfile(src)) or \
       (src_blob and s["count"] == 1 and s["sample"] and strip_query(src).rstrip("/").endswith("/" + s["sample"][0])):
        return {"status": "ok", "detail": "Single file - azcopy's own result above confirms it."}

    candidates = [dst]
    if op == "copy" and "*" not in src:
        if src_blob:
            segments = urllib.parse.unquote(urllib.parse.urlsplit(src).path).strip("/").split("/")
            name = segments[-1] if len(segments) > 1 else ""      # a whole container is copied without nesting
        else:
            name = os.path.basename(os.path.normpath(src))
        if name:
            if dst_blob:
                p = urllib.parse.urlsplit(dst)
                sub = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/") + "/" + urllib.parse.quote(name),
                                               p.query, ""))
            else:
                sub = os.path.join(dst, name)
            candidates = [dst, sub] if j.get("asSubdir") == "false" else [sub, dst]

    best_target, best = None, None
    for cand in candidates:
        d = count_target(cand, dst_blob, env, j, timeout=3600)
        if d["error"]:
            continue
        if best is None or d["count"] > best["count"]:
            best_target, best = cand, d
        if d["count"] >= s["count"]:
            break
    if best is None:
        return {"status": "warn", "detail": "Could not count the destination files."}

    where = strip_query(best_target)
    detail = f"Source: {s['count']:,} files ({human_size(s['bytes'])}). Destination ({where}): {best['count']:,} files."
    include, exclude, _ = filters_of(j)
    if include or exclude:
        detail += " Include/exclude filters applied to both counts."
    if (j.get("extraArgs") or "").strip():
        detail += " Filters typed in Extra arguments are not reflected in the counts."
    if best["count"] >= s["count"]:
        return {"status": "ok", "detail": "All source files are at the destination. " + detail}
    return {"status": "warn", "detail": f"{s['count'] - best['count']:,} file(s) missing at the destination. " + detail}


def start_verify(job, j):
    job["verify"] = {"status": "running", "detail": "Counting files at the source and the destination…"}

    def work():
        try:
            job["verify"] = run_verify(j)
        except Exception as e:
            job["verify"] = {"status": "warn", "detail": f"The check could not run: {e}"}
        write_manifest(job)
    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- run record (manifest)

SAFE_FIELDS = ("operation", "direction", "auth", "recursive", "overwrite", "deleteDestination", "dryRun", "logLevel",
               "capMbps", "includePattern", "excludePattern", "asSubdir", "preserveTier", "preserveProps", "putMd5",
               "checkMd5", "concurrency", "verifyAfter", "spnTenant", "spnClient")


def run_meta(j):
    """Everything worth recording about a transfer - never tokens or secrets."""
    meta = {"name": j.get("name"), "source": strip_query((j.get("source") or "").strip()),
            "destination": strip_query((j.get("destination") or "").strip()),
            "options": {k: j.get(k) for k in SAFE_FIELDS if j.get(k) not in (None, "")},
            "extraArgs": hide_secrets(j.get("extraArgs") or "")}
    try:
        meta["user"] = getpass.getuser()
    except Exception:
        meta["user"] = None
    meta["computer"] = socket.gethostname()
    return meta


def write_manifest(job):
    try:
        info = job_info(job, record=False)
        record = {
            "tool": "AzCopy Runner", "runnerJob": job["id"], "resumedFrom": job.get("resumedFrom"),
            **job["meta"], "status": info["status"], "azcopyJobId": info["azJobId"], "exitCode": info["exitCode"],
            "started": info["started"], "ended": info["ended"],
            "durationSeconds": int((job["ended"] - job["started"]).total_seconds()) if job["ended"] else None,
            "lastProgress": info["progress"], "finalStatus": info["finalStatus"],
            "postCopyCheck": job.get("verify"), "command": job["command"], "logFile": job["out"],
        }
        Path(job["manifest"]).write_text(hide_secrets(json.dumps(record, indent=2)), encoding="utf-8")
    except Exception as e:
        print(f"Could not write run record for job {job['id']}: {e}", flush=True)


def job_info(job, record=True):
    proc = job["proc"]
    code = proc.poll()
    newly_ended = False
    text = read_tail(job["out"], 131072)
    lines = [l for l in re.split(r"\r\n|\n|\r", text) if l != ""]

    percent = progress = None
    for line in reversed(lines):
        m = re.match(r"^\s*([\d.]+)\s*%", line)
        if m:
            percent, progress = float(m.group(1)), line.strip()
            break
    m = re.search(r"Final Job Status:\s*(\w+)", text)
    final = m.group(1) if m else None
    m = re.search(r"Job ([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})", text)
    az_job_id = m.group(1) if m else job.get("azJobId")
    job["azJobId"] = az_job_id

    if code is None:
        status = "running"
    else:
        if not job["ended"]:
            job["ended"] = datetime.now()
            scrub_file(job["out"])
            newly_ended = True
        if job["stopped"] or final == "Cancelled":
            status = "stopped"
        elif final and final.startswith("CompletedWithErrors"):
            status = "warning"
        elif code == 0:
            status = "completed"
        else:
            status = "failed"
        if status == "completed":
            percent = 100

    fmt = "%Y-%m-%d %H:%M:%S"
    info = {
        "id": job["id"], "status": status, "percent": percent, "progress": progress,
        "finalStatus": final, "azJobId": az_job_id, "exitCode": code,
        "started": job["started"].strftime(fmt),
        "ended": job["ended"].strftime(fmt) if job["ended"] else None,
        "command": job["command"], "logFile": job["out"],
        "log": hide_secrets("\n".join(lines[-400:])),
        "verify": job.get("verify"), "manifest": job.get("manifest"), "resumedFrom": job.get("resumedFrom"),
        "canResume": bool(az_job_id) and status in ("stopped", "failed", "warning") and not job.get("dryRun"),
    }
    if newly_ended and record:
        write_manifest(job)
    return info


def start_job(j):
    exe = azcopy_status()["resolved"]
    if not exe:
        raise ValueError("azcopy not found. Set its path in Settings at the top of the page.")
    built = build_args(j)
    if built["errors"]:
        raise ValueError(" ".join(built["errors"]))
    return launch(exe, built["argv"], built["env"], built["command"], j)


def resume_job(old, j):
    """Continues an interrupted azcopy job (azcopy jobs resume), passing the tokens again from the form."""
    exe = azcopy_status()["resolved"]
    if not exe:
        raise ValueError("azcopy not found.")
    if not old.get("azJobId"):
        raise ValueError("This transfer has no azcopy job ID to resume.")
    if old["proc"].poll() is None:
        raise ValueError("This transfer is still running.")
    argv = ["jobs", "resume", old["azJobId"]]
    if auth_mode(j) == "sas":
        direction = j.get("direction")
        if direction in ("blob-local", "blob-blob"):
            sas = side_sas((j.get("source") or "").strip(), j.get("sourceSas"))
            if sas:
                argv.append(f"--source-sas={sas}")
        if direction in ("local-blob", "blob-blob"):
            sas = side_sas((j.get("destination") or "").strip(), j.get("destSas"))
            if sas:
                argv.append(f"--destination-sas={sas}")
    env, env_note = job_env(j)
    command = show_command(argv) + (f"   [{env_note}]" if env_note else "")
    return launch(exe, argv, env, command, j, resumed_from=old["id"])


def launch(exe, argv, env_extra, command, j, resumed_from=None):
    global next_id
    with jobs_lock:
        job_id = next_id
        next_id += 1
    safe = re.sub(r"[^\w-]+", "_", j.get("name") or "").strip("_") or f"transfer{job_id}"
    if resumed_from:
        safe += "_resume"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = str(LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}_job{job_id}.log")

    # azcopy's own detailed logs go into logs/azcopy inside this folder
    env = dict(os.environ, AZCOPY_LOG_LOCATION=str(LOG_DIR / "azcopy"), **env_extra)
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WIN \
        else {"start_new_session": True}
    with open(out, "wb") as fh:
        proc = subprocess.Popen([exe] + argv, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, **kwargs)

    job = {"id": job_id, "proc": proc, "out": out, "started": datetime.now(), "ended": None,
           "stopped": False, "command": command, "meta": run_meta(j), "resumedFrom": resumed_from,
           "dryRun": j.get("dryRun") == "true", "manifest": re.sub(r"\.log$", ".run.json", out)}
    with jobs_lock:
        jobs[job_id] = job
    print(f"[{datetime.now():%H:%M:%S}] Started job {job_id}: {command}", flush=True)
    return job


# ---------------------------------------------------------------- Microsoft Entra ID sign-in (azcopy login)

login = {"proc": None, "lines": [], "state": "idle"}
login_lock = threading.Lock()


def azcopy_login_status(exe):
    try:
        r = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=30, **hidden_window())
    except Exception as e:
        return False, str(e)
    text = (r.stdout + r.stderr).strip()
    signed_in = r.returncode == 0 and not re.search(r"not (logged|signed) in|no.*token|failed", text, re.I)
    return signed_in, hide_secrets(text.splitlines()[-1] if text else "")


def start_login(tenant):
    exe = resolve_azcopy(load_config().get("azcopyPath", ""))
    if not exe:
        raise ValueError("azcopy not found. Set its path first.")
    tenant = (tenant or "").strip()
    if tenant and not re.fullmatch(r"[\w.-]+", tenant):
        raise ValueError("The tenant ID does not look valid.")
    with login_lock:
        if login["proc"] and login["proc"].poll() is None:
            return
        argv = [exe, "login"] + (["--tenant-id", tenant] if tenant else [])
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", **hidden_window())
        login.update(proc=proc, lines=[], state="waiting")

    def reader():
        for raw in proc.stdout:
            if raw.strip():
                login["lines"].append(raw.rstrip())
                del login["lines"][:-30]
        proc.wait()
        if login["state"] == "waiting":      # not cancelled meanwhile
            login["state"] = "done" if proc.returncode == 0 else "failed"
    threading.Thread(target=reader, daemon=True).start()


def login_info():
    text = "\n".join(login["lines"])
    url = re.search(r"https://(?:microsoft\.com/devicelogin|login\.microsoft(?:online)?\.com/\S*)", text)
    code = re.search(r"code\s+([A-Z0-9-]{6,})", text)
    return {"state": login["state"], "output": hide_secrets(text),
            "url": url.group(0).rstrip(".,") if url else None, "code": code.group(1) if code else None}


def cancel_login():
    with login_lock:
        if login["proc"] and login["proc"].poll() is None:
            login["proc"].kill()
        login["state"] = "idle"


def stop_job(job):
    proc = job["proc"]
    if proc.poll() is not None:
        return
    job["stopped"] = True
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, **hidden_window())
    else:
        # Ctrl+C lets azcopy cancel cleanly (the job stays resumable); force-kill if it hangs.
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            return

        def force():
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        threading.Thread(target=force, daemon=True).start()
    print(f"[{datetime.now():%H:%M:%S}] Stopped job {job['id']}", flush=True)


def browse(kind, start):
    """Open a native file/folder picker on this computer and return the chosen path (or None)."""
    start = local_path(start or "")
    start_dir = start if os.path.isdir(start) else os.path.dirname(start) if os.path.isfile(start) else ""
    with browse_lock:
        if IS_MAC:
            what = "file" if kind == "file" else "folder"
            script = f'POSIX path of (choose {what} with prompt "Select a {what}"'
            if start_dir:
                script += ' default location (POSIX file "{}")'.format(start_dir.replace("\\", "\\\\").replace('"', '\\"'))
            script += ")"
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            path = r.stdout.strip() if r.returncode == 0 else ""
            return (path.rstrip("/") or "/") if path else None
        if not IS_WIN and shutil.which("zenity"):
            cmd = ["zenity", "--file-selection"] + ([] if kind == "file" else ["--directory"])
            if start_dir:
                cmd += ["--filename", start_dir.rstrip("/") + "/"]
            r = subprocess.run(cmd, capture_output=True, text=True)
            return r.stdout.strip() or None
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError:
            raise ValueError("No file picker available here - type the path instead.")
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if kind == "file":
                path = filedialog.askopenfilename(parent=root, title="Select a file", initialdir=start_dir or None)
            else:
                path = filedialog.askdirectory(parent=root, title="Select a folder", initialdir=start_dir or None)
        finally:
            root.destroy()
        return os.path.normpath(path) if path else None


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "AzCopyRunner"

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "img-src data:; connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def allowed(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("localhost", "127.0.0.1"):
            self.json({"error": "Forbidden"}, 403)
            return False
        if self.path.startswith("/api/") and self.headers.get("X-Session-Key") != SESSION_KEY:
            # Only the browser opened by this server knows the key, so other websites and programs cannot drive the API.
            self.json({"error": "Forbidden"}, 403)
            return False
        return True

    def do_GET(self):
        try:
            if not self.allowed():
                return
            if self.path in ("/", "/index.html"):
                # The page itself holds no key: the browser gets it from the #k=... part of the address it was
                # opened with, which never reaches this server, so other local programs cannot read it from here.
                self.send(200, INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/config":
                self.json(azcopy_status())
            elif self.path == "/api/login":
                self.json(login_info())
            elif self.path == "/api/jobs":
                with jobs_lock:
                    current = sorted(jobs.values(), key=lambda x: x["id"])
                self.json({"jobs": [job_info(x) for x in current]})
            else:
                self.json({"error": "Not found"}, 404)
        except Exception as e:
            self.json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            if not self.allowed():
                return
            path = self.path
            if path == "/api/config":
                b = self.body()
                save_config({"azcopyPath": (b.get("azcopyPath") or "").strip().strip('"')})
                self.json(azcopy_status())
            elif path == "/api/preview":
                built = build_args(self.body())
                self.json({k: built[k] for k in ("command", "errors", "warnings")})
            elif path == "/api/test":
                self.json(test_connection(self.body()))
            elif path == "/api/browse":
                b = self.body()
                self.json({"path": browse(b.get("kind"), b.get("start"))})
            elif path == "/api/jobs":
                try:
                    self.json({"job": job_info(start_job(self.body()))})
                except (ValueError, OSError) as e:
                    self.json({"error": str(e)}, 400)
            elif re.fullmatch(r"/api/jobs/\d+/stop", path):
                job = jobs.get(int(path.split("/")[3]))
                if not job:
                    self.json({"error": "Unknown job"}, 404)
                    return
                stop_job(job)
                self.json({"job": job_info(job)})
            elif re.fullmatch(r"/api/jobs/\d+/(resume|verify)", path):
                job = jobs.get(int(path.split("/")[3]))
                if not job:
                    self.json({"error": "Unknown job"}, 404)
                    return
                try:
                    if path.endswith("/resume"):
                        self.json({"job": job_info(resume_job(job, self.body()))})
                    else:
                        start_verify(job, self.body())
                        self.json({"job": job_info(job)})
                except (ValueError, OSError) as e:
                    self.json({"error": str(e)}, 400)
            elif path == "/api/login":
                try:
                    start_login(self.body().get("tenantId"))
                    self.json(login_info())
                except (ValueError, OSError) as e:
                    self.json({"error": str(e)}, 400)
            elif path == "/api/login/cancel":
                cancel_login()
                self.json(login_info())
            elif path in ("/api/login/status", "/api/logout"):
                exe = resolve_azcopy(load_config().get("azcopyPath", ""))
                if not exe:
                    self.json({"error": "azcopy not found."}, 400)
                    return
                if path == "/api/logout":
                    subprocess.run([exe, "logout"], capture_output=True, timeout=30, **hidden_window())
                signed_in, text = azcopy_login_status(exe)
                self.json({"signedIn": signed_in, "detail": text})
            elif path == "/api/shutdown":
                if self.body().get("stopJobs"):
                    for job in list(jobs.values()):
                        stop_job(job)
                self.json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.json({"error": "Not found"}, 404)
        except Exception as e:
            self.json({"error": str(e)}, 500)


def main():
    global LOG_DIR
    ap = argparse.ArgumentParser(description="AzCopy Runner")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--log-dir", help=f"where to keep transfer logs (default: {LOG_DIR})")
    args = ap.parse_args()
    if args.log_dir:
        LOG_DIR = Path(args.log_dir).expanduser().resolve()

    # Safety net: if an earlier run was killed mid-transfer, its logs were never cleaned of SAS signatures.
    if LOG_DIR.is_dir():
        for f in LOG_DIR.glob("*.log"):
            scrub_file(f)

    server = None
    for port in range(args.port, args.port + 11):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not server:
        print(f"Could not open a local port between {args.port} and {args.port + 10}.")
        sys.exit(1)

    url = f"http://localhost:{port}/#k={SESSION_KEY}"
    st = azcopy_status()
    print()
    print("  AzCopy Runner is running")
    print(f"  Open: {url}")
    print("  (this link is your private key for this session - do not share it)")
    print(f"  azcopy: {st['resolved'] or 'NOT FOUND - set the path in the page Settings'}")
    print(f"  Logs:   {LOG_DIR}")
    if st["error"]:
        print(f"  {st['error']}")
    print('  Keep this window open while transfers run. Press Ctrl+C or use "Quit" in the page to stop.')
    print(flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        left = [j for j in jobs.values() if j["proc"].poll() is None]
        if left:
            print(f"{len(left)} azcopy transfer(s) are still running in the background. "
                  "Their output keeps going to the logs folder.")


if __name__ == "__main__":
    main()
