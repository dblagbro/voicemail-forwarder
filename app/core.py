import os, json, time, threading, sqlite3, hashlib, poplib, ssl, smtplib, traceback, mimetypes
import re, gzip, glob, collections
import email
import email.policy
from email.message import EmailMessage
from datetime import datetime, timedelta

BASE_DIR = os.environ.get("APP_BASE", "/app")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
ATT_DIR = os.path.join(LOGS_DIR, "attachments")
CFG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "state.db")
LOG_FILE = os.path.join(LOGS_DIR, "forwarder.log")
LAST_POLL_FILE = os.path.join(DATA_DIR, "last_poll.txt")

for d in (DATA_DIR, LOGS_DIR, ATT_DIR):
    os.makedirs(d, exist_ok=True)

_poll_thread = None
_poll_stop = threading.Event()
_poll_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized = False


def default_config():
    return {
        "system": {
            "poll_interval_seconds": 60,
            "attachment_retention_months": 24,
            "smtp": {
                "host": "",
                "port": 587,
                "starttls": True,
                "user": "",
                "pass": "",
                "from": "",
                "helo": "",
            },
            "alert_hours": 0,
            "alert_email": ""
        },
        "mailboxes": []
    }


def ensure_initialized():
    global _initialized
    with _init_lock:
        if _initialized:
            return
        if not os.path.exists(CFG_PATH):
            os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
            with open(CFG_PATH, "w") as f:
                json.dump(default_config(), f, indent=2)
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            create table if not exists seen (
                key text primary key,
                mailbox text not null
            )
        """)
        cur.execute("""
            create table if not exists attachments (
                id integer primary key autoincrement,
                filename text not null,
                mailbox text not null,
                subject text,
                ts text not null
            )
        """)
        # Added 2026-10-31. Keyed by (mailbox, uidl) so two mailboxes on the
        # same server can never collide, and so a message can be recognised
        # BEFORE it is fetched. The older `seen` table is keyed by a hash that
        # includes the Subject header, which is only knowable after a RETR --
        # that is what made every poll refetch the whole mailbox. `seen` is
        # still read (and still written) so that an upgrade does not re-send
        # anything already forwarded; see poll_once().
        cur.execute("""
            create table if not exists seen_uidl (
                mailbox text not null,
                uidl text not null,
                ts text not null,
                primary key (mailbox, uidl)
            )
        """)
        conn.commit()
        conn.close()
        _initialized = True


def load_cfg():
    ensure_initialized()
    with open(CFG_PATH, "r") as f:
        return json.load(f)


def save_cfg(cfg):
    ensure_initialized()
    with open(CFG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def log_line(line: str):
    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%F %T")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {line}\n")


def get_recent_log(max_lines: int = 200, newest_first: bool = True) -> str:
    if not os.path.exists(LOG_FILE):
        return ""
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        if newest_first:
            lines = list(reversed(lines))
        return "".join(lines[:max_lines])
    except Exception:
        return ""


def make_key(uidl: str, subj: str, mailbox_name: str, host: str, user: str) -> str:
    h = hashlib.sha256()
    h.update((host or "").encode("utf-8", "ignore"))
    h.update(b"|")
    h.update((user or "").encode("utf-8", "ignore"))
    h.update(b"|")
    h.update((mailbox_name or "").encode("utf-8", "ignore"))
    h.update(b"|")
    h.update((uidl or "").encode("utf-8", "ignore"))
    h.update(b"|")
    h.update((subj or "").encode("utf-8", "ignore"))
    return h.hexdigest()


def seen_has(key: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("select 1 from seen where key=?", (key,))
    found = cur.fetchone() is not None
    conn.close()
    return found


def seen_add(key: str, mailbox_name: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("insert or ignore into seen(key, mailbox) values(?,?)", (key, mailbox_name))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# UIDL-before-RETR state (added 2026-10-31)
#
# Keep deduplication IDs while messages remain on the mail server. Removing a
# live UIDL can cause a voicemail to be fetched and forwarded again. The guard
# below prunes only above the configured limit, only old entries, and only
# entries absent from a complete server UIDL listing.
# ---------------------------------------------------------------------------

SEEN_UIDL_MAX_ROWS = 5000
SEEN_UIDL_KEEP_DAYS = 365

# Mailboxes already warned about missing UIDL support, so the warning is logged
# once per process instead of once per poll (the whole point is to not spam).
_uidl_warned = set()


def message_ids(uidl: str, original, raw: bytes) -> list:
    """Every stable id this message may be recognised by.

    The UIDL is the cheap one: it is known from the UIDL listing, before any
    RETR, which is what makes skipping free. The Message-ID (or, failing that,
    a hash of the raw message) is only knowable after a RETR, but it is what
    lets the UIDL-less fallback path recognise a message this service has
    already forwarded, so it is recorded too.
    """
    ids = []
    if uidl:
        ids.append(uidl)
    mid = ""
    if original is not None:
        try:
            mid = (original.get("Message-ID") or "").strip()
        except Exception:
            mid = ""
    if mid:
        ids.append("msgid:" + mid)
    elif raw:
        ids.append("sha256:" + hashlib.sha256(raw).hexdigest())
    return ids


def seen_uidl_has(mailbox_name: str, uidl: str) -> bool:
    if not uidl:
        return False
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("select 1 from seen_uidl where mailbox=? and uidl=?", (mailbox_name, uidl))
    found = cur.fetchone() is not None
    conn.close()
    return found


def seen_uidl_has_any(mailbox_name: str, uidls) -> bool:
    ids = [u for u in (uidls or []) if u]
    if not ids:
        return False
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    qmarks = ",".join("?" * len(ids))
    cur.execute(f"select 1 from seen_uidl where mailbox=? and uidl in ({qmarks})",
                [mailbox_name] + ids)
    found = cur.fetchone() is not None
    conn.close()
    return found


def seen_uidl_add(mailbox_name: str, uidls):
    """Record one id (str) or several (list) for this mailbox. Idempotent."""
    if isinstance(uidls, str):
        uidls = [uidls]
    ids = [u for u in (uidls or []) if u]
    if not ids:
        return
    now_iso = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executemany("insert or ignore into seen_uidl(mailbox, uidl, ts) values(?,?,?)",
                    [(mailbox_name, u, now_iso) for u in ids])
    conn.commit()
    conn.close()


def prune_seen_uidl(mailbox_name: str, live_uidls) -> int:
    """Guard-rail prune. Only call with a COMPLETE server-side UIDL listing.

    Never removes an id that is still on the server, and never removes anything
    at all until the mailbox is carrying more than SEEN_UIDL_MAX_ROWS rows.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("select count(*) from seen_uidl where mailbox=?", (mailbox_name,))
    total = cur.fetchone()[0]
    if total <= SEEN_UIDL_MAX_ROWS:
        conn.close()
        return 0
    cutoff = (datetime.now() - timedelta(days=SEEN_UIDL_KEEP_DAYS)).isoformat(timespec="seconds")
    live = set(u for u in (live_uidls or []) if u)
    cur.execute("select uidl from seen_uidl where mailbox=? and ts < ?", (mailbox_name, cutoff))
    stale = [u for (u,) in cur.fetchall() if u not in live]
    if stale:
        cur.executemany("delete from seen_uidl where mailbox=? and uidl=?",
                        [(mailbox_name, u) for u in stale])
        conn.commit()
    conn.close()
    if stale:
        log_line(f"PRUNE | mailbox={mailbox_name} | removed={len(stale)} seen_uidl rows "
                 f"(had {total}, cap {SEEN_UIDL_MAX_ROWS}, older than {SEEN_UIDL_KEEP_DAYS}d "
                 f"and gone from the server)")
    return len(stale)


def smtp_send(cfg_smtp: dict, msg: EmailMessage):
    host = cfg_smtp.get("host") or ""
    port = int(cfg_smtp.get("port", 587))
    starttls = bool(cfg_smtp.get("starttls", True))
    user = cfg_smtp.get("user") or ""
    pwd = cfg_smtp.get("pass") or ""
    helo = cfg_smtp.get("helo") or None

    if not host:
        raise RuntimeError("SMTP host not configured")

    with smtplib.SMTP(host, port, local_hostname=helo) as s:
        s.ehlo()
        if starttls:
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
        if user:
            s.login(user, pwd)
        s.send_message(msg)


def smtp_test(cfg_smtp: dict, dest: str):
    msg = EmailMessage()
    msg["Subject"] = "Voicemail Forwarder SMTP Test"
    msg["From"] = cfg_smtp.get("from") or cfg_smtp.get("user") or "noreply@example"
    msg["To"] = dest
    msg.set_content("This is a test message from voicemail-forwarder.")
    smtp_send(cfg_smtp, msg)
    log_line(f"INFO | smtp_test_sent | dest={dest}")


def build_forward(original: email.message.EmailMessage, smtp_from: str, dest_to: str):
    out = EmailMessage()
    subj = original.get("Subject", "") or ""
    out["Subject"] = subj
    out["From"] = smtp_from
    out["To"] = dest_to
    orig_from = original.get("From", "")
    if orig_from:
        out["Reply-To"] = orig_from
    out.set_content("Voicemail forwarded automatically.")

    attachments_added = 0
    if original.is_multipart():
        for part in original.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = part.get("Content-Disposition", "") or ""
            filename = part.get_filename()
            if "attachment" in disp or ctype.startswith("audio/"):
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                maintype = part.get_content_maintype() or "application"
                subtype = part.get_content_subtype() or "octet-stream"
                if not filename:
                    filename = "voicemail." + (subtype or "bin")
                out.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
                attachments_added += 1
    return out, attachments_added


def fetch_raw(pop: poplib.POP3, msg_num: int):
    resp, lines, octets = pop.retr(msg_num)
    raw = b"\r\n".join(lines) + b"\r\n"
    return raw, octets


def archive_attachments(original: email.message.EmailMessage, mailbox_name: str, subject: str) -> int:
    saved = 0
    now_iso = datetime.now().isoformat(timespec="seconds")
    if not original.is_multipart():
        return 0
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for part in original.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = part.get("Content-Disposition", "") or ""
        if not ("attachment" in disp or ctype.startswith("audio/")):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        filename = part.get_filename() or ("voicemail." + (part.get_content_subtype() or "bin"))
        # basename() the sender-supplied filename: it comes straight off the
        # MIME part. The timestamp prefix happens to neutralise a traversal
        # today, because the attacker never controls the first path component,
        # but that is an accident of the format string rather than a check.
        filename = os.path.basename(filename) or "voicemail.bin"
        safe = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{mailbox_name}_{filename}"
        path = os.path.join(ATT_DIR, safe)
        with open(path, "wb") as wf:
            wf.write(payload)
        cur.execute("insert into attachments(filename, mailbox, subject, ts) values(?,?,?,?)",
                    (safe, mailbox_name, subject, now_iso))
        saved += 1
    conn.commit()
    conn.close()
    return saved


def get_attachments_rows(limit: int = 1000):
    ensure_initialized()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("select filename, mailbox, subject, ts from attachments order by id desc limit ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [r for r in rows if r[0].lower().endswith(".wav")]


def prune_old_attachments(retention_months: int):
    keep_days = int(retention_months) * 30
    cutoff_dt = datetime.now() - timedelta(days=keep_days)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("select id, filename, ts from attachments")
    rows = cur.fetchall()
    removed = 0
    for _id, fn, ts in rows:
        try:
            ts_dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        if ts_dt < cutoff_dt:
            path = os.path.join(ATT_DIR, fn)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            cur.execute("delete from attachments where id=?", (_id,))
            removed += 1
    conn.commit()
    conn.close()
    if removed:
        log_line(f"PRUNE | removed={removed} attachments older than {retention_months} months")


def safe_attachment_path(filename: str):
    """Resolve filename inside ATT_DIR, or None if it escapes.

    The web UI routes use Flask's <path:> converter, which accepts slashes, so
    a bare os.path.join(ATT_DIR, fn) was satisfied by "../../data/config.json"
    and reached any file this process can read or unlink -- config.json holds
    the relay password and every mailbox password, and gunicorn runs as root.
    realpath both ends and require the result to sit under the real ATT_DIR so
    a symlink inside the directory cannot be used to step out either.
    """
    root = os.path.realpath(ATT_DIR)
    path = os.path.realpath(os.path.join(root, filename))
    if path != root and os.path.commonpath((root, path)) != root:
        return None
    return path


def delete_attachment(filename: str):
    path = safe_attachment_path(filename)
    if path is None:
        log_line(f"ATTACHMENT_DELETE_REJECTED | path_escape | {filename}")
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("delete from attachments where filename=?", (filename,))
    conn.commit()
    conn.close()
    log_line(f"ATTACHMENT_DELETED | {filename}")


def poll_once():
    ensure_initialized()
    cfg = load_cfg()
    syscfg = cfg.get("system", {})
    smtp_cfg = syscfg.get("smtp", {})
    smtp_from = smtp_cfg.get("from") or smtp_cfg.get("user") or "noreply@example"
    retention_months = int(syscfg.get("attachment_retention_months", 24))

    mailboxes = cfg.get("mailboxes", [])
    log_line(f"INFO | poll_start | checking {len(mailboxes)} mailbox(es)")

    for mbox in mailboxes:
        if not mbox.get("enabled", True):
            continue
        name = mbox.get("name") or mbox.get("pop3_user") or "mbox"
        host = mbox.get("pop3_host") or ""
        port = int(mbox.get("pop3_port", 110))
        user = mbox.get("pop3_user") or ""
        pwd = mbox.get("pop3_pass") or ""
        dest = mbox.get("dest_to") or ""
        use_stls = bool(mbox.get("use_stls", False))

        log_line(f"INFO | mailbox_check_start | mailbox={name} | host={host}:{port} | user={user}")

        if not host or not user or not pwd or not dest:
            log_line(f"ERROR | mailbox={name} | config_incomplete")
            continue

        try:
            log_line(f"INFO | mailbox={name} | connecting to {host}:{port}")
            pop = poplib.POP3(host, port, timeout=30)
            log_line(f"INFO | mailbox={name} | connected | response={pop.getwelcome().decode()}")
            if use_stls:
                try:
                    pop.stls(context=ssl.create_default_context())
                    log_line(f"INFO | mailbox={name} | STLS enabled")
                except Exception:
                    # Requested TLS is mandatory: never send USER/PASS in cleartext.
                    pop.close()
                    raise

            log_line(f"INFO | mailbox={name} | authenticating as {user}")
            user_resp = pop.user(user)
            log_line(f"INFO | mailbox={name} | USER response={user_resp.decode()}")
            pass_resp = pop.pass_(pwd)
            log_line(f"INFO | mailbox={name} | PASS response={pass_resp.decode()}")

            # --- enumerate the mailbox ---------------------------------------
            # UIDL first, and skip on the UIDL alone. A UIDL is stable per
            # message, so an already-forwarded message costs nothing: no RETR,
            # no SMTP, no log line. Only a message whose UIDL is unknown is
            # fetched. (Until 2026-10-31 this loop RETR'd every message on
            # every poll because the de-dupe key needed the Subject header,
            # which is only readable after the fetch.)
            #
            # NO DELE anywhere in here, by design: removing voicemails from the
            # AAM mailbox is a separate operator decision.
            msgs = []          # [(msg_num, uidl_or_empty)]
            uidl_ok = True
            try:
                resp, lst, _ = pop.uidl()
                msg_count = len(lst) if lst else 0
                log_line(f"INFO | mailbox={name} | UIDL response={resp.decode()} | messages={msg_count}")
                for entry in (lst or []):
                    parts = entry.decode().split()
                    if len(parts) < 2:
                        continue
                    try:
                        msgs.append((int(parts[0]), parts[1]))
                    except ValueError:
                        continue
            except poplib.error_proto as e:
                # The server answered -ERR: it does not implement UIDL. Fall
                # back to the pre-2026-10-31 behaviour (LIST, then RETR every
                # message and de-dupe after the fetch) so mail still gets
                # forwarded, and warn exactly once per mailbox per process.
                # Only error_proto is treated this way -- a timeout or socket
                # error still aborts the mailbox via the outer handler, because
                # treating a transient failure as "no UIDL support" would make
                # the fallback path re-enumerate a mailbox it cannot cheaply
                # recognise.
                uidl_ok = False
                if name not in _uidl_warned:
                    _uidl_warned.add(name)
                    log_line(f"WARN | mailbox={name} | UIDL unsupported by {host}:{port} ({e}) | "
                             f"falling back to RETR-then-de-dupe: every poll will refetch the whole "
                             f"mailbox for this mailbox only. Forwarding still works and nothing is "
                             f"sent twice.")
                _resp, llst, _ = pop.list()
                for entry in (llst or []):
                    parts = entry.decode().split()
                    if not parts:
                        continue
                    try:
                        msgs.append((int(parts[0]), ""))
                    except ValueError:
                        continue
                log_line(f"INFO | mailbox={name} | LIST (UIDL fallback) | messages={len(msgs)}")

            if not msgs:
                log_line(f"INFO | mailbox={name} | no_messages | closing connection")
                pop.quit()
                continue

            live_ids = []
            skipped_no_retr = 0
            for msg_num, uidl in msgs:
                if uidl:
                    live_ids.append(uidl)
                    if seen_uidl_has(name, uidl):
                        skipped_no_retr += 1
                        continue

                log_line(f"INFO | mailbox={name} | processing_message | msg_num={msg_num} | UIDL={uidl or '-'}")

                raw, octets = fetch_raw(pop, msg_num)
                log_line(f"INFO | mailbox={name} | fetched_message | msg_num={msg_num} | size={octets} bytes")

                original = email.message_from_bytes(raw, policy=email.policy.default)
                subj = original.get("Subject", "") or ""
                ids = message_ids(uidl, original, raw)
                live_ids.extend(ids)
                key = make_key(uidl, subj, name, host, user)

                # Two post-fetch recognisers, both needed:
                #  - seen_uidl: the Message-ID/content id, which is how the
                #    UIDL-less fallback path recognises its own history.
                #  - seen: the legacy subject-hash key, which is the ONLY
                #    record of everything forwarded before this change. Without
                #    this check the first poll after an upgrade would re-send
                #    the entire mailbox.
                if seen_uidl_has_any(name, ids) or seen_has(key):
                    seen_uidl_add(name, ids)
                    log_line(f"INFO | mailbox={name} | SKIPPED | msg_num={msg_num} | UIDL={uidl or '-'} | "
                             f"reason=already_seen | indexed=1")
                    continue

                log_line(f"INFO | mailbox={name} | forwarding_message | msg_num={msg_num} | UIDL={uidl or '-'} | SUBJECT={json.dumps(subj)}")
                try:
                    fwd, attach_count = build_forward(original, smtp_from, dest)
                    fwd["X-Forwarded-By"] = "voicemail-forwarder"
                    smtp_send(smtp_cfg, fwd)
                    archived = archive_attachments(original, name, subj)
                    seen_add(key, name)
                    seen_uidl_add(name, ids)
                    log_line(f"FORWARDED | mailbox={name} | UIDL={uidl or '-'} | SUBJECT={json.dumps(subj)} | SIZE={octets} | ATTACHMENTS={attach_count} | SAVED={archived}")
                except Exception as e:
                    log_line(f"ERROR | mailbox={name} | UIDL={uidl or '-'} | err={e}\n{traceback.format_exc()}")
            if skipped_no_retr:
                log_line(f"INFO | mailbox={name} | skipped_without_retr={skipped_no_retr} | "
                         f"already forwarded, not fetched")
            if uidl_ok:
                prune_seen_uidl(name, live_ids)
            try:
                pop.quit()
            except Exception:
                try:
                    pop.close()
                except Exception:
                    pass
        except Exception as e:
            log_line(f"ERROR | mailbox={name} | connect_failed | err={e}")
    prune_old_attachments(retention_months)

    try:
        with open(LAST_POLL_FILE, "w") as f:
            f.write(datetime.now().isoformat())
    except Exception:
        pass


def manual_poll_now():
    try:
        log_line("INFO | manual_poll_requested")
        poll_once()
        log_line("INFO | manual_poll_completed")
        return True
    except Exception as e:
        log_line(f"ERROR | manual_poll_failed | err={e}")
        return False


def poll_loop():
    while not _poll_stop.is_set():
        start = time.time()
        log_line("INFO | poller_tick_start")
        with _poll_lock:
            try:
                poll_once()
                log_line("INFO | poller_tick_ok")
            except Exception as e:
                log_line(f"ERROR | poll_once | err={e}\n{traceback.format_exc()}")

        cfg2 = load_cfg()
        syscfg2 = cfg2.get("system", {})
        smtp_cfg2 = syscfg2.get("smtp", {})
        alert_email = syscfg2.get("alert_email", "")
        alert_hours = int(syscfg2.get("alert_hours", 0))

        if alert_email and alert_hours > 0:
            try:
                if os.path.exists(LAST_POLL_FILE):
                    with open(LAST_POLL_FILE, "r") as f:
                        last = f.read().strip()
                    if last:
                        last_dt = datetime.fromisoformat(last)
                        if datetime.now() - last_dt > timedelta(hours=alert_hours):
                            msg = EmailMessage()
                            msg["Subject"] = "Voicemail Forwarder Warning: Poller stalled"
                            msg["From"] = smtp_cfg2.get("from", smtp_cfg2.get("user", ""))
                            msg["To"] = alert_email
                            msg.set_content(f"No poll has succeeded for more than {alert_hours} hours.")
                            smtp_send(smtp_cfg2, msg)
                            log_line(f"ALERT | Poller inactive for {alert_hours} hours | email={alert_email}")
            except Exception as e:
                log_line(f"ERROR | poll_health_check | err={e}")

        interval = int(syscfg2.get("poll_interval_seconds", 60))
        elapsed = time.time() - start
        sleep_for = max(1, interval - int(elapsed))
        _poll_stop.wait(timeout=sleep_for)


def start_poller():
    global _poll_thread
    ensure_initialized()
    if _poll_thread and _poll_thread.is_alive():
        return
    _poll_stop.clear()
    _poll_thread = threading.Thread(target=poll_loop, name="poller", daemon=True)
    _poll_thread.start()
    log_line("INFO | poller_thread_started")


def list_attachments_for_ui(limit: int = 1000):
    return get_attachments_rows(limit)


# ---------------------------------------------------------------------------
# Log search across rotated files (added 2026-10-26)
#
# logrotate keeps forwarder.log plus forwarder.log.1 and forwarder.log.N.gz,
# but get_recent_log() only ever opened the live file, so the UI could not see
# anything older than the most recent rotation. search_log() walks the whole
# retained set, transparently decompressing .gz, and filters as it goes.
#
# Memory is bounded by max_lines: each file is STREAMED and only the matching
# lines we intend to show are retained. forwarder.log.1 is routinely 32MB, so
# never f.readlines() these.
# ---------------------------------------------------------------------------

LOG_LEVELS = ["INFO", "WARN", "ERROR", "FORWARDED", "PRUNE"]

LOG_EVENTS = [
    "FORWARDED",
    "ERROR",
    "SKIPPED",
    "skipped_without_retr",
    "UIDL unsupported",
    "connect_failed",
    "connected",
    "processing_message",
    "fetched_message",
    "forwarding_message",
    "poller_tick_ok",
    "manual_poll_requested",
    "smtp_test_sent",
    "pop3_test_ok",
    "pop3_test_failed",
]

_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\]")
_LEVEL_RE = re.compile(r"^\[[^\]]*\]\s+(\S+)")


def log_files_newest_first():
    """[forwarder.log, forwarder.log.1, forwarder.log.2.gz, ...] in that order.

    logrotate numbers ascending with age, so a numeric sort on the suffix is
    the age order. A plain glob sort would put .10 before .2.
    """
    rotated = []
    for p in glob.glob(LOG_FILE + ".*"):
        tail = os.path.basename(p)[len(os.path.basename(LOG_FILE)) + 1:]
        if tail.endswith(".gz"):
            tail = tail[:-3]
        try:
            rotated.append((int(tail), p))
        except ValueError:
            continue
    rotated.sort(key=lambda t: t[0])
    files = [LOG_FILE] if os.path.exists(LOG_FILE) else []
    return files + [p for _, p in rotated]


def _open_log(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", errors="replace")
    return open(path, "r", errors="replace")


def _file_start_day(path):
    """Date of the first timestamped line, or None. Used to skip whole files."""
    try:
        with _open_log(path) as f:
            for _ in range(50):
                line = f.readline()
                if not line:
                    break
                m = _TS_RE.match(line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def _line_matches(line, q, level, event, mb_re, since, until):
    if q and q not in line.lower():
        return False
    if level:
        m = _LEVEL_RE.match(line)
        if not m or m.group(1) != level:
            return False
    if event and event not in line:
        return False
    if mb_re and not mb_re.search(line):
        return False
    if since or until:
        m = _TS_RE.match(line)
        if not m:
            return False
        day = m.group(1)
        if since and day < since:
            return False
        if until and day > until:
            return False
    return True


def search_log(q="", level="", event="", mailbox="", since="", until="",
               max_lines=2000, newest_first=True, time_budget=20.0):
    """Filtered read across the live log and every rotated/gzipped log.

    Returns (text, meta). meta carries what was actually scanned so the UI can
    be honest about truncation instead of silently showing a partial answer.
    """
    q = (q or "").strip().lower()
    level = (level or "").strip()
    event = (event or "").strip()
    mailbox = (mailbox or "").strip()
    since = (since or "").strip()
    until = (until or "").strip()
    try:
        max_lines = max(1, min(int(max_lines), 20000))
    except Exception:
        max_lines = 2000

    mb_re = re.compile(r"\bmailbox=" + re.escape(mailbox) + r"\b") if mailbox else None

    all_files = log_files_newest_first()
    files = all_files if newest_first else list(reversed(all_files))

    out = []
    scanned = []
    skipped = []
    matched = 0
    truncated = False
    budget_hit = False
    deadline = time.time() + max(1.0, float(time_budget))

    for path in files:
        base = os.path.basename(path)
        remaining = max_lines - len(out)
        if remaining <= 0:
            truncated = True
            break

        # A file whose first line is already past `until` can hold nothing we
        # want. (We cannot cheaply bound the other end, so `since` still scans.)
        if until:
            start = _file_start_day(path)
            if start and start > until:
                skipped.append(base)
                continue

        kept = collections.deque(maxlen=remaining)
        n_file = 0
        try:
            with _open_log(path) as f:
                for i, line in enumerate(f):
                    if (i & 0x1FFF) == 0 and time.time() > deadline:
                        budget_hit = True
                        break
                    if not _line_matches(line, q, level, event, mb_re, since, until):
                        continue
                    n_file += 1
                    kept.append(line)
                    # Oldest-first wants the FIRST n matches, so we can stop as
                    # soon as the window is full. Newest-first wants the LAST n,
                    # so the deque has to keep sliding to the end of the file.
                    if not newest_first and len(kept) >= remaining:
                        break
        except Exception as e:
            out.append("[log-search] could not read %s: %s\n" % (base, e))
            continue

        scanned.append({"file": base, "matches": n_file})
        matched += n_file
        # Reverse within the file, not globally: files are already walked in the
        # requested age order, so a global reverse would undo that and surface
        # the oldest file first.
        out.extend(reversed(kept) if newest_first else kept)
        if n_file > len(kept):
            truncated = True
        if budget_hit:
            break

    if matched > len(out):
        truncated = True

    meta = {
        "shown": len(out),
        "matched": matched,
        "truncated": truncated,
        "budget_hit": budget_hit,
        "scanned": scanned,
        "skipped": skipped,
        "available": [os.path.basename(p) for p in all_files],
        "max_lines": max_lines,
    }
    return "".join(out), meta


# ---------------------------------------------------------------------------
# Per-mailbox POP3 test (added 2026-10-26)
# ---------------------------------------------------------------------------

def pop3_test(mbox: dict, timeout: int = 15) -> dict:
    """Read-only reachability + auth check for one mailbox.

    Mirrors the connect sequence in poll_once() so a pass here means the poller
    can do its job, but it stops after UIDL/STAT. It never issues RETR or DELE,
    so running a test can neither consume a voicemail nor trigger a forward.

    Returns a dict the UI renders directly; it never raises.
    """
    name = mbox.get("name") or mbox.get("pop3_user") or "mbox"
    host = mbox.get("pop3_host") or ""
    port = int(mbox.get("pop3_port", 110) or 110)
    user = mbox.get("pop3_user") or ""
    pwd = mbox.get("pop3_pass") or ""
    dest = mbox.get("dest_to") or ""
    use_stls = bool(mbox.get("use_stls", False))

    res = {
        "mailbox": name, "host": host, "port": port, "user": user,
        "dest": dest, "use_stls": use_stls, "enabled": bool(mbox.get("enabled", True)),
        "ok": False, "stage": "config", "error": "", "steps": [],
        "messages": None, "bytes": None, "newest": None, "elapsed_ms": 0,
    }

    def step(label, ok, note=""):
        res["steps"].append({"label": label, "ok": bool(ok), "note": note})

    required_fields = ("pop3_host", "pop3_user", "pop3_pass", "dest_to")
    required_values = (host, user, pwd, dest)
    missing = [key for key, value in zip(required_fields, required_values) if not value]
    if missing:
        res["error"] = "config incomplete, missing: " + ", ".join(missing)
        step("config", False, res["error"])
        log_line(f"ERROR | mailbox={name} | pop3_test_failed | stage=config | err={res['error']}")
        return res
    step("config", True, f"{host}:{port} as {user} -> {dest}")

    t0 = time.time()
    pop = None
    try:
        res["stage"] = "connect"
        pop = poplib.POP3(host, port, timeout=timeout)
        step("connect", True, pop.getwelcome().decode(errors="replace"))

        if use_stls:
            res["stage"] = "stls"
            pop.stls(context=ssl.create_default_context())
            step("STLS", True, "TLS negotiated")

        res["stage"] = "auth"
        pop.user(user)
        pop.pass_(pwd)
        step("authenticate", True, f"accepted for {user}")

        res["stage"] = "uidl"
        _resp, lst, _ = pop.uidl()
        count = len(lst) if lst else 0
        res["messages"] = count
        newest = ""
        if lst:
            try:
                newest = lst[-1].decode(errors="replace").split(" ", 1)[1]
            except Exception:
                newest = ""
        res["newest"] = newest or None
        step("UIDL", True, f"{count} message(s)" + (f", newest {newest}" if newest else ""))

        res["stage"] = "stat"
        try:
            nmsg, nbytes = pop.stat()
            res["bytes"] = nbytes
            step("STAT", True, f"{nmsg} message(s), {nbytes} bytes")
        except Exception as e:
            step("STAT", False, str(e))

        res["ok"] = True
        res["stage"] = "done"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        step(res["stage"], False, res["error"])
    finally:
        if pop is not None:
            try:
                pop.quit()
            except Exception:
                try:
                    pop.close()
                except Exception:
                    pass
        res["elapsed_ms"] = int((time.time() - t0) * 1000)

    if res["ok"]:
        log_line(f"INFO | mailbox={name} | pop3_test_ok | messages={res['messages']} | {res['elapsed_ms']}ms")
    else:
        log_line(f"ERROR | mailbox={name} | pop3_test_failed | stage={res['stage']} | err={res['error']}")
    return res


def pop3_test_by_name(name: str, timeout: int = 15):
    """Test the named mailbox. Returns None if no mailbox has that name."""
    for m in load_cfg().get("mailboxes", []):
        if m.get("name") == name:
            return pop3_test(m, timeout=timeout)
    return None


def pop3_test_all(timeout: int = 15) -> list:
    return [pop3_test(m, timeout=timeout) for m in load_cfg().get("mailboxes", [])]
