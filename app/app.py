import os
import mimetypes
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from . import core

def create_app():
    core.ensure_initialized()
    core.start_poller()

    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET", "voicemail-forwarder-dev")

    @app.before_request
    def maintenance_gate():
        if request.method not in {"GET", "HEAD", "OPTIONS"} and core.maintenance_active():
            return "Service is in maintenance mode\n", 503, {"Retry-After": "1"}

    @app.route("/")
    def index():
        cfg = core.load_cfg()
        order = request.args.get("order", "new")
        newest_first = (order != "old")
        logs = core.get_recent_log(200, newest_first=newest_first)
        return render_template("index.html", cfg=cfg, logs=logs, order=order)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        cfg = core.load_cfg()
        if request.method == "POST":
            syscfg = cfg.setdefault("system", {})
            syscfg["poll_interval_seconds"] = int(request.form.get("poll_interval_seconds", 60))
            syscfg["attachment_retention_months"] = int(request.form.get("attachment_retention_months", 24))
            syscfg["alert_hours"] = int(request.form.get("alert_hours", 0) or 0)
            syscfg["alert_email"] = request.form.get("alert_email", "")

            smtp = syscfg.setdefault("smtp", {})
            smtp["host"] = request.form.get("smtp_host", smtp.get("host", ""))
            smtp["port"] = int(request.form.get("smtp_port", smtp.get("port", 587)))
            smtp["starttls"] = request.form.get("smtp_starttls", "true") == "true"
            smtp["user"] = request.form.get("smtp_user", smtp.get("user", ""))
            smtp["pass"] = request.form.get("smtp_pass", smtp.get("pass", ""))
            smtp["from"] = request.form.get("smtp_from", smtp.get("from", ""))
            smtp["helo"] = request.form.get("smtp_helo", smtp.get("helo", ""))

            core.save_cfg(cfg)
            flash("Settings saved")
            return redirect(url_for("settings"))
        return render_template("settings.html", cfg=cfg)

    @app.route("/smtp-test", methods=["POST"])
    def smtp_test():
        dest = request.form.get("dest", "")
        cfg = core.load_cfg()
        smtp_cfg = cfg.get("system", {}).get("smtp", {})
        try:
            core.smtp_test(smtp_cfg, dest)
            flash("SMTP test sent successfully")
        except Exception as e:
            flash(f"SMTP test failed: {e}")
        return redirect(url_for("settings"))

    @app.route("/poll-now", methods=["POST"])
    def poll_now():
        ok = core.manual_poll_now()
        if ok:
            flash("Manual poll executed")
        else:
            flash("Manual poll FAILED, see logs")
        return redirect(url_for("index"))

    @app.route("/mailboxes", methods=["GET"])
    def mailboxes():
        cfg = core.load_cfg()
        return render_template("mailboxes.html", cfg=cfg)

    @app.route("/mailboxes/add", methods=["POST"])
    def add_mailbox():
        cfg = core.load_cfg()
        mb = {
            "name": (request.form.get("name", "") or request.form.get("pop3_user", "")).strip(),
            "pop3_host": request.form.get("pop3_host", "").strip(),
            "pop3_port": int(request.form.get("pop3_port", "110")),
            "pop3_user": request.form.get("pop3_user", "").strip(),
            "pop3_pass": request.form.get("pop3_pass", "").strip(),
            "use_stls": request.form.get("use_stls", "false") == "true",
            "dest_to": request.form.get("dest_to", "").strip(),
            "enabled": request.form.get("enabled", "true") == "true"
        }
        cfg.setdefault("mailboxes", []).append(mb)
        core.save_cfg(cfg)
        flash("Mailbox added")
        return redirect(url_for("mailboxes"))

    @app.route("/mailboxes/toggle/<name>", methods=["POST"])
    def toggle_mailbox(name):
        cfg = core.load_cfg()
        for m in cfg.get("mailboxes", []):
            if m.get("name") == name:
                m["enabled"] = not m.get("enabled", True)
        core.save_cfg(cfg)
        return redirect(url_for("mailboxes"))

    @app.route("/mailboxes/delete/<name>", methods=["POST"])
    def delete_mailbox(name):
        cfg = core.load_cfg()
        mbs = [m for m in cfg.get("mailboxes", []) if m.get("name") != name]
        cfg["mailboxes"] = mbs
        core.save_cfg(cfg)
        flash("Mailbox removed")
        return redirect(url_for("mailboxes"))

    @app.route("/mailboxes/test/<name>", methods=["POST"])
    def test_mailbox(name):
        res = core.pop3_test_by_name(name)
        if res is None:
            return {"ok": False, "mailbox": name, "stage": "lookup",
                    "error": "no such mailbox", "steps": []}, 404
        return res

    @app.route("/mailboxes/test-all", methods=["POST"])
    def test_all_mailboxes():
        return {"results": core.pop3_test_all()}

    @app.route("/mailboxes/password/<name>", methods=["POST"])
    def change_password(name):
        newpw = request.form.get("new_password", "")
        if not newpw:
            flash("Password cannot be empty")
            return redirect(url_for("mailboxes"))
        cfg = core.load_cfg()
        for m in cfg.get("mailboxes", []):
            if m.get("name") == name:
                m["pop3_pass"] = newpw
        core.save_cfg(cfg)
        flash("Password updated")
        return redirect(url_for("mailboxes"))

    @app.route("/logs")
    def raw_logs():
        order = request.args.get("order", "new")
        newest_first = (order != "old")
        f = {
            "q": request.args.get("q", ""),
            "level": request.args.get("level", ""),
            "event": request.args.get("event", ""),
            "mailbox": request.args.get("mailbox", ""),
            "since": request.args.get("since", ""),
            "until": request.args.get("until", ""),
            "limit": request.args.get("limit", "2000"),
        }
        logs, meta = core.search_log(
            q=f["q"], level=f["level"], event=f["event"], mailbox=f["mailbox"],
            since=f["since"], until=f["until"], max_lines=f["limit"],
            newest_first=newest_first,
        )
        cfg = core.load_cfg()
        mailbox_names = [m.get("name", "") for m in cfg.get("mailboxes", []) if m.get("name")]
        return render_template(
            "logs.html", logs=logs, order=order, meta=meta, f=f,
            levels=core.LOG_LEVELS, events=core.LOG_EVENTS,
            mailbox_names=mailbox_names,
        )

    @app.route("/attachments")
    def list_attachments():
        rows = core.list_attachments_for_ui(1000)
        return render_template("attachments.html", rows=rows)

    @app.route("/attachments/<path:fn>")
    def fetch_attachment(fn):
        path = core.safe_attachment_path(fn)
        if path is None or not os.path.isfile(path):
            return "Not found", 404
        if request.args.get("download") == "1":
            return send_file(path, as_attachment=True, download_name=os.path.basename(path))
        mime, _ = mimetypes.guess_type(path)
        if not mime:
            mime = "audio/wav"
        return send_file(path, mimetype=mime, as_attachment=False, download_name=os.path.basename(path))

    @app.route("/attachments/delete/<path:fn>", methods=["POST"])
    def delete_attachment(fn):
        core.delete_attachment(fn)
        flash("Attachment deleted")
        return redirect(url_for("list_attachments"))

    return app

app = create_app()
