"""NVR Track Telegram bot: status commands, buttons, scan requests and alerts.

Runs as the nvr-telegram service. It connects OUT to api.telegram.org (long
polling), so no port, firewall rule or tunnel is needed, and the bot works
from any phone or network. It has no NVR credentials and never contacts an
NVR: it reads the same saved reports and scanner status as the dashboard
(through app.dashboard), and asks for scans the same way the dashboard does
(a request file for the nvr-scanner service).

Only Telegram user IDs and group chats listed in .env may use it. Anyone else
is told their own ID (so an administrator can add them) and nothing more.
The bot token is never printed: errors are logged by type only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.exceptions import RequestException

from app import telegram_views as views
from app.dashboard import read_dashboard_data, read_scan_status, request_scan

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATTERN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
POLL_TIMEOUT_SECONDS = 25
OLD_MESSAGE_SECONDS = 15 * 60        # Commands sent while the bot was down are skipped.
UNKNOWN_REPLY_SECONDS = 10 * 60      # At most one "not authorized" reply per user.
DIGEST_LATEST_HOURS = 3              # A missed digest is sent at most this late.
PENDING_SCAN_HOURS = 3

COMMANDS = [
    ("status", "Fleet summary"),
    ("problems", "Cameras that need action"),
    ("nvrs", "All NVRs with buttons"),
    ("nvr", "One NVR: /nvr nvr-01"),
    ("camera", "Find a camera: /camera gate"),
    ("offline", "Offline cameras"),
    ("storage", "Disks and storage findings"),
    ("firmware", "NVR models and firmware"),
    ("scan", "Scan now: /scan all or /scan nvr-01 (admins)"),
    ("scanner", "Scanner status and schedule"),
    ("alerts", "Alerts in this chat: /alerts on|off"),
    ("myid", "Show your Telegram ID"),
    ("help", "Commands"),
]


# ---------- settings ----------

def _ids(text: str) -> set[int]:
    found = set()
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if re.fullmatch(r"-?\d{1,20}", part):
            found.add(int(part))
        elif part:
            print(f"telegram: ignoring invalid ID entry {part[:24]!r}", flush=True)
    return found


def load_settings(env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    digest = env.get("TELEGRAM_DIGEST_TIME", "08:30").strip()
    try:
        drift = int(env.get("NVR_CLOCK_DRIFT_WARN_SECONDS", "120"))
        drift = drift if 10 <= drift <= 86400 else 120
    except ValueError:
        drift = 120
    try:
        check = int(env.get("TELEGRAM_ALERT_CHECK_SECONDS", "60"))
        check = check if 20 <= check <= 3600 else 60
    except ValueError:
        check = 60
    admins = _ids(env.get("TELEGRAM_ADMIN_USER_IDS", ""))
    alert_chats = _ids(env.get("TELEGRAM_ALERT_CHAT_IDS", ""))
    return {
        "token": token if TOKEN_PATTERN.match(token) else "",
        "token_set": bool(token),
        "admins": admins,
        "users": _ids(env.get("TELEGRAM_ALLOWED_USER_IDS", "")) | admins,
        # Members of these groups may read status; alert groups count too.
        "chats": _ids(env.get("TELEGRAM_ALLOWED_CHAT_IDS", "")) | alert_chats,
        "alert_chats": alert_chats,
        "digest": TIME.match(digest).groups() if TIME.match(digest) else None,
        "show_ips": env.get("TELEGRAM_SHOW_IPS", "false").strip().lower() == "true",
        "drift": drift,
        "check_seconds": check,
        "state_dir": Path(env.get("TELEGRAM_STATE_DIR", str(ROOT / "state" / "telegram"))),
    }


def role(settings: dict, user_id: int | None, chat_id: int | None) -> str | None:
    """'admin', 'viewer' or None. Admin rights always follow the person."""
    if user_id in settings["admins"]:
        return "admin"
    if user_id in settings["users"] or chat_id in settings["chats"]:
        return "viewer"
    return None


# ---------- Telegram API ----------

class TelegramError(Exception):
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TelegramAPI:
    """Minimal Bot API client. Never lets the token (in the URL) reach a log."""

    def __init__(self, token: str, session: requests.Session | None = None):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._session = session or requests.Session()

    def call(self, method: str, http_timeout: int = 15, **params):
        params = {key: value for key, value in params.items() if value is not None}
        try:
            response = self._session.post(self._base + method, json=params, timeout=(5, http_timeout))
        except RequestException as exc:
            raise TelegramError(f"{method}: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {response.status_code}") from None
        if not isinstance(body, dict) or not body.get("ok"):
            body = body if isinstance(body, dict) else {}
            retry = (body.get("parameters") or {}).get("retry_after")
            raise TelegramError(f"{method}: {body.get('error_code')} {str(body.get('description', ''))[:120]}",
                                retry_after=retry if isinstance(retry, int) else None)
        return body.get("result")


# ---------- state files ----------

def read_state(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_state(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix="." + path.name, suffix=".tmp", delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False)
        temporary = Path(handle.name)
    os.replace(temporary, path)


# ---------- keyboards ----------

def button(text: str, data: str) -> dict | None:
    return {"text": text, "callback_data": data} if len(data.encode()) <= 64 else None


def keyboard(rows: list[list[dict | None]]) -> dict:
    return {"inline_keyboard": [[b for b in row if b] for row in rows if any(row)]}


def menu_keyboard(is_admin: bool) -> dict:
    return keyboard([
        [button("📊 Status", "cmd:status"), button("🚨 Problems", "cmd:problems")],
        [button("🖥 NVRs", "cmd:nvrs"), button("📴 Offline", "cmd:offline")],
        [button("💾 Storage", "cmd:storage"), button("🧩 Firmware", "cmd:firmware")],
        [button("🔄 Scan all", "scan:all") if is_admin else None, button("⏱ Scanner", "cmd:scanner")],
    ])


def nvrs_keyboard(data: dict) -> dict:
    rows, row = [], []
    for nvr in data.get("nvrs") or []:
        row.append(button(f"{views.HEALTH_ICON.get(nvr['overall_status'], '⚪')} {nvr['id']}", f"nvr:{nvr['id']}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([button("« Menu", "cmd:menu")])
    return keyboard(rows)


def nvr_keyboard(nvr_id: str, is_admin: bool) -> dict:
    return keyboard([
        [button("🚨 Problems", f"nvrp:{nvr_id}"), button("📴 Offline", f"nvro:{nvr_id}")],
        [button("🔄 Scan this NVR", f"scan:{nvr_id}") if is_admin else None, button("« All NVRs", "cmd:nvrs")],
    ])


HELP = ("<b>NVR Track bot</b> (read-only)\n"
        + "\n".join(f"/{name} — {views.esc(text)}" for name, text in COMMANDS)
        + "\n\nIn a private chat you can also just type a camera name.")


# ---------- the bot ----------

class Bot:
    def __init__(self, settings: dict, api, *, data_source=read_dashboard_data,
                 scan_status=read_scan_status, scan_request=request_scan, clock=time.time):
        self.settings = settings
        self.api = api
        self.data_source = data_source
        self.scan_status = scan_status
        self.scan_request = scan_request
        self.clock = clock
        self.username = ""
        self.unknown_replies: dict[int, float] = {}
        self.state_dir = settings["state_dir"]

    # ----- sending -----

    def send(self, chat_id: int, text: str, markup: dict | None = None) -> bool:
        parts = views.split_message(text)
        ok = True
        for index, part in enumerate(parts):
            params = {"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                      "disable_web_page_preview": True}
            if markup and index == len(parts) - 1:
                params["reply_markup"] = markup
            try:
                self.api.call("sendMessage", **params)
            except TelegramError as exc:
                print(f"telegram: send to {chat_id} failed: {exc}", flush=True)
                ok = False
                if exc.retry_after:
                    time.sleep(min(exc.retry_after, 30))
        return ok

    def show(self, chat_id: int, message_id: int | None, text: str, markup: dict | None = None) -> None:
        """Edit the message a button belongs to; send new message(s) otherwise."""
        if message_id is not None and len(text) <= views.MESSAGE_LIMIT:
            try:
                self.api.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                              parse_mode="HTML", disable_web_page_preview=True,
                              **({"reply_markup": markup} if markup else {}))
                return
            except TelegramError as exc:
                if "not modified" in str(exc):
                    return
        self.send(chat_id, text, markup)

    # ----- incoming updates -----

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return
        message = update.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("text"), str):
            return
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id, user_id = chat.get("id"), user.get("id")
        if not isinstance(chat_id, int) or not isinstance(user_id, int) or user.get("is_bot"):
            return
        if isinstance(message.get("date"), int) and self.clock() - message["date"] > OLD_MESSAGE_SECONDS:
            return  # Sent while the bot was down; never act on stale commands.
        text = message["text"].strip()
        private = chat.get("type") == "private"
        command, args = "", ""
        if text.startswith("/"):
            head, _, args = text.partition(" ")
            command, _, mention = head[1:].partition("@")
            if mention and self.username and mention.lower() != self.username.lower():
                return  # Addressed to another bot in a group.
            command = command.lower()
        elif not private:
            return  # Groups: only commands.
        who = role(self.settings, user_id, chat_id)
        if who is None:
            self.refuse(chat_id, user_id, private, command)
            return
        print(f"telegram: user {user_id} ({who}) {('/' + command) if command else 'search'}", flush=True)
        if not command:
            self.reply_view(chat_id, None, "camera", text, who)
        elif command in {"start", "menu"}:
            self.send(chat_id, views.fleet_status(self.data_source()), menu_keyboard(who == "admin"))
        elif command == "help":
            self.send(chat_id, HELP, menu_keyboard(who == "admin"))
        elif command == "myid":
            self.send(chat_id, f"Your Telegram user ID: <code>{user_id}</code>\nThis chat ID: <code>{chat_id}</code>")
        elif command == "scan" and not args.strip():
            if who != "admin":
                self.send(chat_id, "Only administrators can start scans.")
                return
            data = self.data_source()
            rows = [[button("🔄 All NVRs", "scan:all")]]
            ids = [nvr["id"] for nvr in data.get("nvrs") or []]
            rows += [[button(i, f"scan:{i}") for i in ids[n:n + 3]] for n in range(0, len(ids), 3)]
            self.send(chat_id, "What should I scan? (read-only; you confirm next)", keyboard(rows))
        elif command == "scan":
            self.start_scan(chat_id, user_id, who, args.strip())
        elif command == "alerts":
            self.set_alerts(chat_id, args.strip().lower())
        elif command in {"status", "problems", "nvrs", "nvr", "camera", "offline", "storage", "firmware", "scanner"}:
            self.reply_view(chat_id, None, command, args, who)
        else:
            self.send(chat_id, "Unknown command. /help lists them.")

    def refuse(self, chat_id: int, user_id: int, private: bool, command: str) -> None:
        if not private and command not in {"myid", "start"}:
            return  # Never chat in groups that were not allowed.
        last = self.unknown_replies.get(user_id, 0)
        if self.clock() - last < UNKNOWN_REPLY_SECONDS:
            return
        self.unknown_replies[user_id] = self.clock()
        print(f"telegram: refused user {user_id} in chat {chat_id}", flush=True)
        chat_note = "" if private else f"\nThis group's chat ID: <code>{chat_id}</code>"
        self.send(chat_id, "🔒 This bot is private.\n"
                  f"Your Telegram user ID: <code>{user_id}</code>{chat_note}\n"
                  "Ask the NVR Track administrator to add it to TELEGRAM_ALLOWED_USER_IDS"
                  + ("" if private else " (or the group to TELEGRAM_ALLOWED_CHAT_IDS)") + ".")

    def handle_callback(self, query: dict) -> None:
        user_id = (query.get("from") or {}).get("id")
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        data = query.get("data") if isinstance(query.get("data"), str) else ""
        try:
            self.api.call("answerCallbackQuery", callback_query_id=query.get("id"))
        except TelegramError:
            pass
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            return
        who = role(self.settings, user_id, chat_id)
        if who is None:
            return
        kind, _, value = data.partition(":")
        message_id = message.get("message_id")
        if kind == "cmd" and value == "menu":
            self.show(chat_id, message_id, views.fleet_status(self.data_source()), menu_keyboard(who == "admin"))
        elif kind == "cmd":
            self.reply_view(chat_id, message_id, value, "", who)
        elif kind == "nvr":
            self.reply_view(chat_id, message_id, "nvr", value, who)
        elif kind == "nvrp":
            self.reply_view(chat_id, message_id, "problems", value, who)
        elif kind == "nvro":
            self.reply_view(chat_id, message_id, "offline", value, who)
        elif kind == "scan":
            if who != "admin":
                self.send(chat_id, "Only administrators can start scans.")
                return
            name = "all NVRs" if value == "all" else views.esc(value)
            self.show(chat_id, message_id, f"Scan {name} now? It takes a few minutes per NVR and is read-only.",
                      keyboard([[button("✅ Yes, scan", f"scanok:{value}"), button("Cancel", "cmd:menu")]]))
        elif kind == "scanok":
            self.start_scan(chat_id, user_id, who, value)

    # ----- views -----

    def reply_view(self, chat_id: int, message_id: int | None, view: str, args: str, who: str) -> None:
        try:
            self._reply_view(chat_id, message_id, view, args, who)
        except TelegramError:
            raise
        except Exception as exc:
            print(f"telegram: view {view} failed: {type(exc).__name__}", flush=True)
            self.send(chat_id, "Could not read the saved reports right now. Try again in a minute.")

    def _reply_view(self, chat_id: int, message_id: int | None, view: str, args: str, who: str) -> None:
        if view == "scanner":
            self.show(chat_id, message_id, views.scan_text(self.scan_status()), menu_keyboard(who == "admin"))
            return
        data = self.data_source()
        show_ips = self.settings["show_ips"]
        markup = None
        if view == "status":
            text, markup = views.fleet_status(data), menu_keyboard(who == "admin")
        elif view == "nvrs":
            text, markup = views.nvr_list(data), nvrs_keyboard(data)
        elif view in {"nvr", "problems", "offline"} and args.strip():
            nvr = views.find_nvr(data, args)
            if nvr is None:
                text, markup = f"No NVR called <b>{views.esc(args.strip())}</b>. Pick one:", nvrs_keyboard(data)
            elif view == "nvr":
                text, markup = views.nvr_detail(data, nvr, show_ips), nvr_keyboard(nvr["id"], who == "admin")
            elif view == "problems":
                text, markup = views.problems_text(data, nvr["id"]), nvr_keyboard(nvr["id"], who == "admin")
            else:
                text, markup = views.offline_text(data, nvr["id"]), nvr_keyboard(nvr["id"], who == "admin")
        elif view == "nvr":
            text, markup = "Which NVR?", nvrs_keyboard(data)
        elif view == "problems":
            text = views.problems_text(data)
        elif view == "offline":
            text = views.offline_text(data)
        elif view == "camera":
            text = views.camera_search(data, args, show_ips)
        elif view == "storage":
            text = views.storage_text(data)
        elif view == "firmware":
            text = views.firmware_text(data)
        else:
            text = HELP
        self.show(chat_id, message_id, text, markup)

    # ----- scans -----

    def start_scan(self, chat_id: int, user_id: int, who: str, target: str) -> None:
        if who != "admin":
            self.send(chat_id, "Only administrators can start scans. Ask one to add your ID to TELEGRAM_ADMIN_USER_IDS.")
            return
        if target != "all":
            nvr = views.find_nvr(self.data_source(), target)
            target = nvr["id"] if nvr else target
        code, body = self.scan_request(target)
        print(f"telegram: user {user_id} requested scan of {target[:64]}: HTTP-like {code}", flush=True)
        if code == 202:
            name = "all NVRs" if target == "all" else views.esc(target)
            self.send(chat_id, f"🔄 Scan of {name} requested. I will tell you here when it finishes.")
            pending = read_state(self.state_dir / "pending_scans.json", [])
            pending.append({"chat_id": chat_id, "target": target,
                            "since": datetime.now(timezone.utc).isoformat()})
            write_state(self.state_dir / "pending_scans.json", pending[-20:])
        else:
            self.send(chat_id, "Scan not started: " + views.esc(body.get("error", f"code {code}")))

    def check_pending_scans(self) -> None:
        pending = read_state(self.state_dir / "pending_scans.json", [])
        if not pending:
            return
        last = self.scan_status().get("last") or {}
        keep = []
        for item in pending:
            try:
                since = datetime.fromisoformat(item["since"])
                finished = datetime.fromisoformat(last["finished_at"]) if last.get("finished_at") else None
            except (KeyError, TypeError, ValueError):
                continue
            if finished and finished >= since and last.get("target") == item.get("target"):
                self.send(item["chat_id"], views.scan_finished_text(last))
            elif datetime.now(timezone.utc) - since < timedelta(hours=PENDING_SCAN_HOURS):
                keep.append(item)
        if keep != pending:
            write_state(self.state_dir / "pending_scans.json", keep)

    # ----- alerts and digest -----

    def subscriptions(self) -> dict:
        subs = read_state(self.state_dir / "subscriptions.json", {})
        return {"on": set(subs.get("on", [])), "off": set(subs.get("off", []))}

    def recipients(self) -> set[int]:
        subs = self.subscriptions()
        return (self.settings["alert_chats"] | subs["on"]) - subs["off"]

    def set_alerts(self, chat_id: int, choice: str) -> None:
        subs = self.subscriptions()
        if choice in {"on", "off"}:
            other = "off" if choice == "on" else "on"
            subs[choice].add(chat_id)
            subs[other].discard(chat_id)
            write_state(self.state_dir / "subscriptions.json",
                        {"on": sorted(subs["on"]), "off": sorted(subs["off"])})
        state = "on" if chat_id in self.recipients() else "off"
        self.send(chat_id, f"🔔 Alerts in this chat are <b>{state}</b>. "
                  "Change with /alerts on or /alerts off. Alerts come only when something changes, "
                  "plus one daily summary.")

    def check_alerts(self) -> None:
        data = self.data_source()
        current, unknown = views.alert_state(data, clock_drift_seconds=self.settings["drift"])
        path = self.state_dir / "alerts.json"
        saved = read_state(path, None)
        if not isinstance(saved, dict) or not isinstance(saved.get("state"), dict):
            # First start: remember the present silently, say hello once.
            write_state(path, {"schema_version": 1, "state": current})
            for chat_id in self.recipients():
                self.send(chat_id, "👋 <b>NVR Track bot started.</b> I will post here when a camera stops "
                          "or starts recording, goes offline, an NVR or its storage has a problem, or "
                          "scans stop.\n\n" + views.fleet_status(data))
            return
        known = {nvr["id"] for nvr in data.get("nvrs") or []}
        merged, opened, resolved = views.compare(saved["state"], current, unknown, known)
        if opened or resolved:
            targets = self.recipients()
            sent = [self.send(chat_id, views.alert_message(opened, resolved)) for chat_id in targets]
            if targets and not any(sent):
                return  # Telegram unreachable: keep the old state and retry next time.
        if merged != saved["state"]:
            write_state(path, {"schema_version": 1, "state": merged})

    def check_digest(self) -> None:
        if not self.settings["digest"]:
            return
        hour, minute = (int(x) for x in self.settings["digest"])
        now = datetime.now(views.LOCAL_TZ)
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not due <= now < due + timedelta(hours=DIGEST_LATEST_HOURS):
            return
        path = self.state_dir / "digest.json"
        if read_state(path, {}).get("date") == now.date().isoformat():
            return
        write_state(path, {"date": now.date().isoformat()})
        data = self.data_source()
        for chat_id in self.recipients():
            self.send(chat_id, "☀️ <b>Daily summary</b> (if this stops arriving, the monitor itself is down)\n\n"
                      + views.fleet_status(data), menu_keyboard(False))

    def periodic(self) -> None:
        for task in (self.check_alerts, self.check_pending_scans, self.check_digest):
            try:
                task()
            except Exception as exc:  # One failing task must not stop the bot.
                print(f"telegram: {task.__name__} failed: {type(exc).__name__}", flush=True)


# ---------- service ----------

def idle(reason: str) -> int:
    print(f"telegram: {reason} The bot is idle; set it in .env, then: "
          "docker compose up -d --force-recreate nvr-telegram", flush=True)
    while True:
        time.sleep(3600)


def main() -> int:
    settings = load_settings()
    if not settings["token"]:
        return idle("TELEGRAM_BOT_TOKEN is missing or not in the expected format."
                    if not settings["token_set"] else "TELEGRAM_BOT_TOKEN has an unexpected format.")
    if not settings["users"] and not settings["chats"]:
        # Still run, so people can send /myid and learn the ID to configure.
        print("telegram: no user or chat IDs allowed yet; everyone only gets their own ID", flush=True)
    api = TelegramAPI(settings["token"])
    bot = Bot(settings, api)
    while True:
        try:
            me = api.call("getMe")
            bot.username = str(me.get("username") or "")
            api.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
            break
        except TelegramError as exc:
            print(f"telegram: cannot reach Telegram yet ({exc}); retrying in 60 s", flush=True)
            time.sleep(60)
    print(f"telegram: running as @{bot.username}; {len(settings['users'])} users, "
          f"{len(settings['admins'])} admins, {len(settings['chats'])} group/alert chats", flush=True)
    offset_path = settings["state_dir"] / "offset.json"
    offset = read_state(offset_path, {}).get("offset")
    next_check = 0.0
    while True:
        try:
            updates = api.call("getUpdates", http_timeout=POLL_TIMEOUT_SECONDS + 10, offset=offset,
                               timeout=POLL_TIMEOUT_SECONDS,
                               allowed_updates=["message", "callback_query"]) or []
        except TelegramError as exc:
            print(f"telegram: polling failed ({exc})", flush=True)
            time.sleep(exc.retry_after or 5)
            updates = []
        for update in updates:
            if isinstance(update, dict) and isinstance(update.get("update_id"), int):
                offset = update["update_id"] + 1
                try:
                    bot.handle_update(update)
                except Exception as exc:
                    print(f"telegram: update failed: {type(exc).__name__}", flush=True)
        if updates:
            write_state(offset_path, {"offset": offset})
        if time.monotonic() >= next_check:
            bot.periodic()
            next_check = time.monotonic() + settings["check_seconds"]


if __name__ == "__main__":
    raise SystemExit(main())
