import os, json, time, threading, sqlite3, hashlib, poplib, ssl, smtplib, traceback, mimetypes
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


def delete_attachment(filename: str):
    path = os.path.join(ATT_DIR, filename)
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
                except poplib.error_proto:
                    log_line(f"ERROR | mailbox={name} | STLS failed, continuing without TLS")

            log_line(f"INFO | mailbox={name} | authenticating as {user}")
            user_resp = pop.user(user)
            log_line(f"INFO | mailbox={name} | USER response={user_resp.decode()}")
            pass_resp = pop.pass_(pwd)
            log_line(f"INFO | mailbox={name} | PASS response={pass_resp.decode()}")

            resp, lst, _ = pop.uidl()
            msg_count = len(lst) if lst else 0
            log_line(f"INFO | mailbox={name} | UIDL response={resp.decode()} | messages={msg_count}")

            if not lst:
                log_line(f"INFO | mailbox={name} | no_messages | closing connection")
                pop.quit()
                continue
            for entry in lst:
                parts = entry.decode().split()
                if len(parts) < 2:
                    continue
                msg_num = int(parts[0])
                uidl = parts[1]
                log_line(f"INFO | mailbox={name} | processing_message | msg_num={msg_num} | UIDL={uidl}")

                raw, octets = fetch_raw(pop, msg_num)
                log_line(f"INFO | mailbox={name} | fetched_message | msg_num={msg_num} | size={octets} bytes")

                original = email.message_from_bytes(raw, policy=email.policy.default)
                subj = original.get("Subject", "") or ""
                key = make_key(uidl, subj, name, host, user)

                if seen_has(key):
                    log_line(f"INFO | mailbox={name} | SKIPPED | msg_num={msg_num} | UIDL={uidl} | reason=already_seen")
                    continue

                log_line(f"INFO | mailbox={name} | forwarding_message | msg_num={msg_num} | UIDL={uidl} | SUBJECT={json.dumps(subj)}")
                try:
                    fwd, attach_count = build_forward(original, smtp_from, dest)
                    fwd["X-Forwarded-By"] = "voicemail-forwarder"
                    smtp_send(smtp_cfg, fwd)
                    archived = archive_attachments(original, name, subj)
                    seen_add(key, name)
                    log_line(f"FORWARDED | mailbox={name} | UIDL={uidl} | SUBJECT={json.dumps(subj)} | SIZE={octets} | ATTACHMENTS={attach_count} | SAVED={archived}")
                except Exception as e:
                    log_line(f"ERROR | mailbox={name} | UIDL={uidl} | err={e}\n{traceback.format_exc()}")
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
