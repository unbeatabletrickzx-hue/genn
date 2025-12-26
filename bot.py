import os
import logging
import asyncio
import subprocess
import signal
import sys
import json
import threading
import shutil
import time
import secrets
from urllib.parse import quote, unquote
from pathlib import Path

import psutil
from flask import Flask, request, render_template_string, jsonify
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
)

# ================= CONFIG =================
TOKEN = os.environ.get("TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")

UPLOAD_DIR = "scripts"
os.makedirs(UPLOAD_DIR, exist_ok=True)

USERS_FILE = "allowed_users.json"
OWNERSHIP_FILE = "ownership.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

running_processes = {}  # tid -> {"process": Popen, "log": path, "started_at": epoch, "last_alert": epoch}

# ---------- ALERT SETTINGS ----------
ENABLE_ALERTS = os.environ.get("ENABLE_ALERTS", "1") == "1"
HEALTHCHECK_INTERVAL_SEC = int(os.environ.get("HEALTHCHECK_INTERVAL_SEC", "20"))
ALERT_COOLDOWN_SEC = int(os.environ.get("ALERT_COOLDOWN_SEC", "180"))
CPU_ALERT_PERCENT = float(os.environ.get("CPU_ALERT_PERCENT", "85"))
RAM_ALERT_MB = float(os.environ.get("RAM_ALERT_MB", "350"))


# ================= HELPERS =================
def is_user_file_id(tid: str) -> bool:
    return (
        isinstance(tid, str)
        and tid.startswith("u")
        and ("|" in tid)
        and tid.count("|") == 1
        and tid.split("|", 1)[0][1:].isdigit()
    )

def is_repo_id(tid: str) -> bool:
    return ("|" in tid) and (not is_user_file_id(tid))

def safe_q(s: str) -> str:
    return quote(s, safe="")

def safe_status_url(tid: str, key: str) -> str:
    return f"{BASE_URL}/status?script={safe_q(tid)}&key={safe_q(key)}"


# ================= JSON STORE =================
def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path: str, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)

def get_allowed_users():
    return _read_json(USERS_FILE, [])

def save_allowed_user(uid: int) -> bool:
    users = get_allowed_users()
    if uid not in users:
        users.append(uid)
        _write_json(USERS_FILE, users)
        return True
    return False

def remove_allowed_user(uid: int) -> bool:
    users = get_allowed_users()
    if uid in users:
        users.remove(uid)
        _write_json(USERS_FILE, users)
        return True
    return False

def load_ownership():
    return _read_json(OWNERSHIP_FILE, {})

def save_ownership_record(tid: str, record: dict):
    data = load_ownership()
    data[tid] = record
    _write_json(OWNERSHIP_FILE, data)

def delete_ownership(tid: str):
    data = load_ownership()
    if tid in data:
        del data[tid]
        _write_json(OWNERSHIP_FILE, data)

def get_owner(tid: str):
    return load_ownership().get(tid, {}).get("owner")

def get_app_key(tid: str):
    return load_ownership().get(tid, {}).get("key")

def get_entry(tid: str):
    return load_ownership().get(tid, {}).get("entry")

def set_last_run(tid: str, value: bool):
    data = load_ownership()
    if tid in data:
        data[tid]["last_run"] = bool(value)
        _write_json(OWNERSHIP_FILE, data)


# ================= PATHS =================
def resolve_paths(tid: str):
    """
    user file: u<uid>|filename.py -> scripts/<uid>/
    repo: repoName|path/to/file.py -> scripts/<repoName>/
    """
    if is_user_file_id(tid):
        u, filename = tid.split("|", 1)
        uid = u[1:]
        work_dir = os.path.join(UPLOAD_DIR, uid)
        env_path = os.path.join(work_dir, ".env")
        req_path = os.path.join(work_dir, "requirements.txt")
        full_script_path = os.path.join(work_dir, filename)
        return work_dir, filename, env_path, req_path, full_script_path

    if is_repo_id(tid):
        repo, file = tid.split("|", 1)
        work_dir = os.path.join(UPLOAD_DIR, repo)
        env_path = os.path.join(work_dir, ".env")
        req_path = os.path.join(work_dir, "requirements.txt")
        full_script_path = os.path.join(work_dir, file)
        return work_dir, file, env_path, req_path, full_script_path

    # legacy fallback
    work_dir = UPLOAD_DIR
    env_path = os.path.join(work_dir, f"{tid}.env")
    req_path = os.path.join(work_dir, f"{tid}_req.txt")
    full_script_path = os.path.join(work_dir, tid)
    return work_dir, tid, env_path, req_path, full_script_path


def within_dir(base: str, p: str) -> bool:
    base_abs = os.path.abspath(base)
    p_abs = os.path.abspath(p)
    return p_abs.startswith(base_abs + os.sep) or p_abs == base_abs

def list_files_safe(work_dir: str, max_files: int = 400):
    out = []
    base = Path(work_dir)
    if not base.exists():
        return out
    for path in base.rglob("*"):
        if len(out) >= max_files:
            break
        if path.is_file():
            rel = str(path.relative_to(base))
            if rel.startswith(".git/") or rel.startswith("node_modules/"):
                continue
            if rel.endswith(".pyc"):
                continue
            out.append(rel)
    out.sort()
    return out


# ================= RUN COMMAND =================
def resolve_run_command(work_dir: str, script_rel: str | None):
    pkg = os.path.join(work_dir, "package.json")
    if os.path.exists(pkg):
        try:
            with open(pkg, "r", encoding="utf-8") as f:
                pkgj = json.load(f)
            scripts = pkgj.get("scripts", {})
            if "start" in scripts and (script_rel is None or script_rel.endswith(".js")):
                return ["npm", "start"], None
        except Exception:
            pass

    def by_ext(path_rel: str):
        ext = path_rel.split(".")[-1].lower()
        if ext == "js":
            return ["node", path_rel]
        if ext == "sh":
            return ["bash", path_rel]
        return ["python", "-u", path_rel]

    if script_rel:
        return by_ext(script_rel), script_rel

    candidates = ["main.py", "app.py", "server.py", "bot.py", "index.js", "server.js", "start.sh"]
    for c in candidates:
        if os.path.exists(os.path.join(work_dir, c)):
            return by_ext(c), c

    for f in list_files_safe(work_dir, max_files=200):
        if f.endswith((".py", ".js", ".sh")):
            return by_ext(f), f

    return None, None


# ================= PROCESS MGMT =================
def build_env(env_path: str):
    env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8", errors="ignore") as f:
            for l in f:
                l = l.strip()
                if not l or l.startswith("#") or "=" not in l:
                    continue
                k, v = l.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def restart_process_background(tid: str):
    work_dir, script_path, env_path, _, _ = resolve_paths(tid)

    # stop previous
    if tid in running_processes:
        try:
            os.killpg(os.getpgid(running_processes[tid]["process"].pid), signal.SIGTERM)
        except Exception:
            pass

    entry = get_entry(tid)
    if is_repo_id(tid):
        cmd, chosen = resolve_run_command(work_dir, entry)
    else:
        cmd, chosen = resolve_run_command(work_dir, script_path)

    if not cmd:
        logger.error("No runnable entry found for %s", tid)
        return

    if is_repo_id(tid):
        data = load_ownership()
        if tid in data:
            data[tid]["entry"] = chosen
            _write_json(OWNERSHIP_FILE, data)

    os.makedirs(work_dir, exist_ok=True)
    env = build_env(env_path)

    log_path = os.path.join(UPLOAD_DIR, f"{tid.replace('|','_')}.log")
    log_file = open(log_path, "a", encoding="utf-8")

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=work_dir,
        preexec_fn=os.setsid,
    )
    running_processes[tid] = {"process": proc, "log": log_path, "started_at": time.time(), "last_alert": 0}
    set_last_run(tid, True)

def stop_process(tid: str):
    if tid in running_processes:
        try:
            os.killpg(os.getpgid(running_processes[tid]["process"].pid), signal.SIGTERM)
        except Exception:
            pass
        running_processes.pop(tid, None)
    set_last_run(tid, False)

def clear_log(tid: str):
    log_path = os.path.join(UPLOAD_DIR, f"{tid.replace('|','_')}.log")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass

def tail_log(tid: str, lines: int = 200) -> str:
    log_path = os.path.join(UPLOAD_DIR, f"{tid.replace('|','_')}.log")
    if not os.path.exists(log_path):
        return "No logs."
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read().splitlines()[-max(50, min(lines, 800)):]
    return "\n".join(data) if data else "(empty)"

def auto_start_last_run_apps():
    data = load_ownership()
    for tid, meta in data.items():
        if meta.get("last_run") is True:
            try:
                restart_process_background(tid)
            except Exception as e:
                logger.error("Auto-start failed for %s: %s", tid, e)


# ================= INSTALL (async, faster) =================
async def run_cmd_async(cmd: list[str], cwd: str):
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")

async def install_dependencies(work_dir: str, update: Update):
    msg = await update.message.reply_text("⏳ Checking deps...")
    try:
        req = os.path.join(work_dir, "requirements.txt")
        if os.path.exists(req):
            await msg.edit_text("⏳ Installing Python deps...")
            code, _, err = await run_cmd_async([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=work_dir)
            if code != 0:
                await msg.edit_text(f"❌ pip failed:\n{err[:1500]}")
                return

        pkg = os.path.join(work_dir, "package.json")
        if os.path.exists(pkg):
            await msg.edit_text("⏳ Installing Node deps (npm install)...")
            code, _, err = await run_cmd_async(["npm", "install"], cwd=work_dir)
            if code != 0:
                await msg.edit_text(f"❌ npm failed:\n{err[:1500]}")
                return

        await msg.edit_text("✅ Dependencies Installed!")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


# ================= ACCESS CONTROL =================
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if uid != ADMIN_ID and uid not in get_allowed_users():
            await update.message.reply_text("⛔ Access Denied.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped


# ================= KEYBOARDS =================
def main_menu_keyboard(uid: int):
    rows = [
        ["📤 Upload File", "🌐 Clone from Git"],
        ["📂 My Hosted Apps", "📊 Server Stats"],
        ["🆘 Help"],
    ]
    if uid == ADMIN_ID:
        rows.insert(2, ["🛠 Owner Panel"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def extras_keyboard():
    return ReplyKeyboardMarkup([["➕ Add Deps", "📝 Type Env Vars"], ["🚀 RUN NOW", "🔙 Cancel"]], resize_keyboard=True)

def git_extras_keyboard():
    return ReplyKeyboardMarkup([["📝 Type Env Vars"], ["📂 Select File to Run", "🔙 Cancel"]], resize_keyboard=True)


# ================= FLASK =================
app = Flask(__name__)

@app.route("/")
def home():
    return "🤖 Bot Host is Alive!", 200

@app.route("/status")
def status():
    script = request.args.get("script", "")
    key = request.args.get("key", "")
    if not script:
        return "Specify script", 400
    real_key = get_app_key(script)
    if not real_key or key != real_key:
        return "⛔ Forbidden", 403
    if script in running_processes and running_processes[script]["process"].poll() is None:
        return f"✅ {script} is running.", 200
    return f"❌ {script} is stopped.", 404

LOGS_HTML = """
<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Logs</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{margin:0;font-family:sans-serif;background:#0b0d10;color:#e8e8e8}
.header{padding:10px;background:#161a20;display:flex;gap:8px;align-items:center;position:sticky;top:0}
.btn{padding:8px 10px;border:0;border-radius:8px;background:#2b90ff;color:#fff;font-weight:700}
pre{margin:0;padding:12px;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,monospace;font-size:12px}
</style></head>
<body>
<div class="header">
  <button class="btn" onclick="loadLogs()">🔄 Refresh</button>
</div>
<pre id="logbox">Loading...</pre>
<script>
var tg=window.Telegram.WebApp; tg.expand();
async function loadLogs(){
  const r = await fetch('/api/logs?id={{tid}}&uid={{uid}}&lines={{lines}}');
  const t = await r.text();
  document.getElementById('logbox').textContent=t;
}
loadLogs();
</script></body></html>
"""

@app.route("/logs")
def logs_ui():
    tid = request.args.get("id", "")
    uid = int(request.args.get("uid", "0"))
    lines = int(request.args.get("lines", "250"))
    owner = get_owner(tid)
    if uid != ADMIN_ID and uid != owner:
        return "⛔ Access Denied", 403
    return render_template_string(LOGS_HTML, tid=safe_q(tid), uid=uid, lines=lines)

@app.route("/api/logs")
def logs_api():
    tid = unquote(request.args.get("id", ""))
    uid = int(request.args.get("uid", "0"))
    lines = int(request.args.get("lines", "250"))
    owner = get_owner(tid)
    if uid != ADMIN_ID and uid != owner:
        return "⛔ Access Denied", 403
    return tail_log(tid, lines), 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


# ================= WATCHDOG (JobQueue-safe) =================
def _can_alert(tid: str) -> bool:
    meta = running_processes.get(tid, {})
    last = meta.get("last_alert", 0)
    return (time.time() - last) >= ALERT_COOLDOWN_SEC

def _mark_alerted(tid: str):
    if tid in running_processes:
        running_processes[tid]["last_alert"] = time.time()

async def watchdog_check(application):
    if not ENABLE_ALERTS:
        return

    ownership = load_ownership()
    watch_list = [tid for tid, meta in ownership.items() if meta.get("last_run") is True]

    for tid in watch_list:
        rp = running_processes.get(tid)
        is_running = rp and rp["process"].poll() is None

        if not is_running:
            if _can_alert(tid):
                owner_id = ownership.get(tid, {}).get("owner", ADMIN_ID)
                msg = f"⚠️ App DOWN\nApp: {tid}\nOwner: {owner_id}\nAction: Restarting now..."
                try:
                    await application.bot.send_message(chat_id=ADMIN_ID, text=msg)
                    if owner_id and owner_id != ADMIN_ID:
                        await application.bot.send_message(chat_id=owner_id, text=msg)
                except Exception as e:
                    logger.error("Alert send failed: %s", e)
                _mark_alerted(tid)

            # auto-restart
            try:
                restart_process_background(tid)
            except Exception as e:
                logger.error("Restart failed %s: %s", tid, e)
            continue

        # resource check
        try:
            pid = rp["process"].pid
            proc = psutil.Process(pid)
            cpu = proc.cpu_percent(interval=0.0)
            ram_mb = proc.memory_info().rss / (1024 * 1024)

            if (cpu >= CPU_ALERT_PERCENT or ram_mb >= RAM_ALERT_MB) and _can_alert(tid):
                owner_id = ownership.get(tid, {}).get("owner", ADMIN_ID)
                msg = (
                    f"🚨 High Resource Usage\nApp: {tid}\n"
                    f"CPU: {cpu:.2f}% (>= {CPU_ALERT_PERCENT}%)\n"
                    f"RAM: {ram_mb:.2f} MB (>= {RAM_ALERT_MB} MB)"
                )
                await application.bot.send_message(chat_id=ADMIN_ID, text=msg)
                if owner_id and owner_id != ADMIN_ID:
                    await application.bot.send_message(chat_id=owner_id, text=msg)
                _mark_alerted(tid)
        except Exception:
            pass


# ================= TELEGRAM STATES =================
WAIT_FILE, WAIT_EXTRAS, WAIT_ENV_TEXT = range(3)
WAIT_URL, WAIT_GIT_EXTRAS, WAIT_GIT_ENV_TEXT, WAIT_SELECT_FILE = range(3, 7)


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Mega Hosting Bot", reply_markup=main_menu_keyboard(update.effective_user.id))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.", reply_markup=main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# ---- Upload Flow ----
@restricted
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📤 Send file (.py, .js, .sh)", reply_markup=ReplyKeyboardMarkup([["🔙 Cancel"]], resize_keyboard=True))
    return WAIT_FILE

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel":
        return await cancel(update, context)

    doc = update.message.document
    if not doc:
        return WAIT_FILE

    tgfile = await doc.get_file()
    fname = doc.file_name
    uid = update.effective_user.id

    if not fname.endswith((".py", ".js", ".sh")):
        await update.message.reply_text("❌ Only .py/.js/.sh allowed.")
        return WAIT_FILE

    user_dir = os.path.join(UPLOAD_DIR, str(uid))
    os.makedirs(user_dir, exist_ok=True)

    path = os.path.join(user_dir, fname)
    await tgfile.download_to_drive(path)

    tid = f"u{uid}|{fname}"
    key = secrets.token_urlsafe(16)

    save_ownership_record(tid, {"owner": uid, "type": "file", "key": key, "last_run": False, "entry": fname, "created_at": int(time.time())})
    context.user_data.update({"type": "file", "target_id": tid, "work_dir": user_dir})

    await update.message.reply_text("✅ Saved.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🚀 RUN NOW":
        return await execute_logic(update, context)
    if txt == "🔙 Cancel":
        return await cancel(update, context)

    if txt == "📝 Type Env Vars":
        await update.message.reply_text("Send env lines (KEY=VALUE)", reply_markup=ReplyKeyboardMarkup([["🔙 Cancel"]], resize_keyboard=True))
        return WAIT_ENV_TEXT

    if txt == "➕ Add Deps":
        await update.message.reply_text("Send requirements.txt or package.json")
        context.user_data["wait"] = "deps"
        return WAIT_EXTRAS

    return WAIT_EXTRAS

async def receive_env_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel":
        return await cancel(update, context)

    tid = context.user_data.get("target_id")
    work_dir, _, env_path, _, _ = resolve_paths(tid)
    os.makedirs(work_dir, exist_ok=True)
    with open(env_path, "a", encoding="utf-8") as f:
        if os.path.exists(env_path) and os.path.getsize(env_path) > 0:
            f.write("\n")
        f.write(update.message.text.strip())

    await update.message.reply_text("✅ Saved.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("wait") != "deps":
        return WAIT_EXTRAS

    doc = update.message.document
    if not doc:
        return WAIT_EXTRAS

    tgfile = await doc.get_file()
    fname = doc.file_name
    tid = context.user_data.get("target_id")
    work_dir = context.user_data.get("work_dir") or resolve_paths(tid)[0]

    if fname not in ("requirements.txt", "package.json"):
        await update.message.reply_text("❌ Only requirements.txt / package.json")
        return WAIT_EXTRAS

    await tgfile.download_to_drive(os.path.join(work_dir, fname))
    context.user_data["wait"] = None

    # install non-blocking
    await install_dependencies(work_dir, update)
    await update.message.reply_text("Done. Next?", reply_markup=extras_keyboard())
    return WAIT_EXTRAS


# ---- Git Flow ----
@restricted
async def git_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 Send Git URL", reply_markup=ReplyKeyboardMarkup([["🔙 Cancel"]], resize_keyboard=True))
    return WAIT_URL

async def receive_git_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if url == "🔙 Cancel":
        return await cancel(update, context)

    uid = update.effective_user.id
    base_repo = url.split("/")[-1].replace(".git", "")
    repo_name = f"{base_repo}_u{uid}"
    repo_path = os.path.join(UPLOAD_DIR, repo_name)

    if os.path.exists(repo_path):
        shutil.rmtree(repo_path, ignore_errors=True)

    msg = await update.message.reply_text("⏳ Cloning repo...")
    code, _, err = await run_cmd_async(["git", "clone", url, repo_path], cwd=UPLOAD_DIR)
    if code != 0:
        await msg.edit_text(f"❌ Clone failed:\n{err[:1500]}")
        return ConversationHandler.END

    await msg.edit_text("✅ Cloned. Installing deps...")
    await install_dependencies(repo_path, update)

    placeholder_tid = f"{repo_name}|PLACEHOLDER"
    key = secrets.token_urlsafe(16)
    save_ownership_record(placeholder_tid, {"owner": uid, "type": "repo", "key": key, "last_run": False, "entry": None, "created_at": int(time.time())})

    context.user_data.update({"repo_path": repo_path, "repo_name": repo_name, "target_id": placeholder_tid, "type": "repo", "work_dir": repo_path})
    await update.message.reply_text("Now select file to run.", reply_markup=git_extras_keyboard())
    return WAIT_GIT_EXTRAS

async def receive_git_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🔙 Cancel":
        return await cancel(update, context)
    if txt == "📝 Type Env Vars":
        await update.message.reply_text("Send env lines (KEY=VALUE)", reply_markup=ReplyKeyboardMarkup([["🔙 Cancel"]], resize_keyboard=True))
        return WAIT_GIT_ENV_TEXT
    if txt == "📂 Select File to Run":
        return await show_file_selection(update, context)
    return WAIT_GIT_EXTRAS

async def show_file_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo_path = context.user_data.get("repo_path")
    if not repo_path:
        await update.message.reply_text("❌ Repo not found.")
        return ConversationHandler.END

    files = [f for f in list_files_safe(repo_path) if f.endswith((".py", ".js", ".sh"))]
    if not files:
        await update.message.reply_text("❌ No runnable files found.")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(f, callback_data=f"sel_run__{f}")] for f in files[:40]]
    await update.message.reply_text("👇 Select file to RUN:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAIT_SELECT_FILE

async def select_git_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    filename = q.data.split("sel_run__")[1]

    repo_name = context.user_data.get("repo_name")
    old_tid = context.user_data.get("target_id")
    new_tid = f"{repo_name}|{filename}"

    data = load_ownership()
    old = data.get(old_tid)
    if old:
        data[new_tid] = old
        data[new_tid]["entry"] = filename
        del data[old_tid]
        _write_json(OWNERSHIP_FILE, data)
    else:
        save_ownership_record(new_tid, {"owner": update.effective_user.id, "type": "repo", "key": secrets.token_urlsafe(16), "last_run": False, "entry": filename, "created_at": int(time.time())})

    context.user_data["target_id"] = new_tid
    await q.edit_message_text(f"✅ Selected: {filename}")
    return await execute_logic(update, context)

async def execute_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = context.user_data.get("target_id", context.user_data.get("fallback_id"))
    if not tid:
        if update.message:
            await update.message.reply_text("❌ No target selected.")
        else:
            await update.callback_query.message.reply_text("❌ No target selected.")
        return ConversationHandler.END

    restart_process_background(tid)
    key = get_app_key(tid) or "no-key"
    msg = f"🚀 Launched!\n🔒 Secure URL:\n{safe_status_url(tid, key)}"

    if update.message:
        await update.message.reply_text(msg, reply_markup=main_menu_keyboard(update.effective_user.id))
    else:
        await update.callback_query.message.reply_text(msg, reply_markup=main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# ================= MANAGE APPS =================
@restricted
async def list_hosted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ownership = load_ownership()
    if not ownership:
        return await update.message.reply_text("📂 Empty.")

    keyboard = []
    for tid, meta in ownership.items():
        owner_id = meta.get("owner")
        if uid == ADMIN_ID or uid == owner_id:
            is_running = tid in running_processes and running_processes[tid]["process"].poll() is None
            status = "🟢" if is_running else "🔴"
            keyboard.append([InlineKeyboardButton(f"{status} {tid}", callback_data=f"man__{tid}")])

    await update.message.reply_text("📂 Select App:", reply_markup=InlineKeyboardMarkup(keyboard))

def app_manage_buttons(tid: str, uid: int):
    owner = get_owner(tid)
    is_running = tid in running_processes and running_processes[tid]["process"].poll() is None
    key = get_app_key(tid) or ""
    status = "🟢 Running" if is_running else "🔴 Stopped"
    text = f"⚙️ App: {tid}\nStatus: {status}"
    if uid == ADMIN_ID:
        text += f"\nOwner: {owner}"
    if key:
        text += f"\nSecure URL:\n{safe_status_url(tid, key)}"

    btns = []
    row = []
    if is_running:
        row.append(InlineKeyboardButton("🛑 Stop", callback_data=f"stop__{tid}"))
    row.append(InlineKeyboardButton("🚀 Restart", callback_data=f"rerun__{tid}"))
    btns.append(row)

    btns.append([
        InlineKeyboardButton("📜 Logs (Web)", web_app=WebAppInfo(url=f"{BASE_URL}/logs?id={safe_q(tid)}&uid={uid}&lines=250")),
        InlineKeyboardButton("🧹 Clear Logs", callback_data=f"clrlog__{tid}"),
    ])
    btns.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"del__{tid}")])
    return text, InlineKeyboardMarkup(btns)

async def manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    data = q.data

    if data.startswith("man__"):
        tid = data.split("man__")[1]
        owner = get_owner(tid)
        if uid != ADMIN_ID and uid != owner:
            return await q.message.reply_text("⛔ Not yours.")
        text, markup = app_manage_buttons(tid, uid)
        return await q.edit_message_text(text, reply_markup=markup)

    if data.startswith("stop__"):
        tid = data.split("stop__")[1]
        owner = get_owner(tid)
        if uid != ADMIN_ID and uid != owner:
            return await q.message.reply_text("⛔ Not yours.")
        stop_process(tid)
        return await q.edit_message_text(f"🛑 Stopped: {tid}")

    if data.startswith("rerun__"):
        tid = data.split("rerun__")[1]
        owner = get_owner(tid)
        if uid != ADMIN_ID and uid != owner:
            return await q.message.reply_text("⛔ Not yours.")
        restart_process_background(tid)
        return await q.edit_message_text(f"✅ Restarted: {tid}")

    if data.startswith("clrlog__"):
        tid = data.split("clrlog__")[1]
        owner = get_owner(tid)
        if uid != ADMIN_ID and uid != owner:
            return await q.message.reply_text("⛔ Not yours.")
        clear_log(tid)
        return await q.message.reply_text("✅ Logs cleared.")

    if data.startswith("del__"):
        tid = data.split("del__")[1]
        owner = get_owner(tid)
        if uid != ADMIN_ID and uid != owner:
            return await q.message.reply_text("⛔ Not yours.")

        stop_process(tid)
        delete_ownership(tid)

        work_dir, script_path, _, _, _ = resolve_paths(tid)
        try:
            if is_repo_id(tid):
                shutil.rmtree(work_dir, ignore_errors=True)
            elif is_user_file_id(tid):
                os.remove(os.path.join(work_dir, script_path))
        except Exception:
            pass

        return await q.edit_message_text(f"🗑️ Deleted: {tid}")


# ================= OWNER PANEL =================
@restricted
async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("⛔ Owner only.")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 View Access List", callback_data="own__access")],
        [InlineKeyboardButton("🧾 View Apps & Owners", callback_data="own__apps")],
        [InlineKeyboardButton("🟢 Running", callback_data="own__running"),
         InlineKeyboardButton("🔴 Down", callback_data="own__down")],
        [InlineKeyboardButton("🛑 Stop ALL", callback_data="own__stopall"),
         InlineKeyboardButton("🔄 Restart ALL last-run", callback_data="own__restartall")],
    ])
    await update.message.reply_text("🛠 Owner Panel", reply_markup=kb)

async def owner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if update.effective_user.id != ADMIN_ID:
        return await q.message.reply_text("⛔ Owner only.")

    ownership = load_ownership()

    if q.data == "own__access":
        allowed = get_allowed_users()
        text = "👥 Access List\n"
        text += f"Owner (ADMIN_ID): {ADMIN_ID}\n"
        if allowed:
            text += "Allowed users:\n" + "\n".join([f"• {u}" for u in allowed])
        else:
            text += "Allowed users: none"
        return await q.message.reply_text(text)

    if q.data == "own__apps":
        if not ownership:
            return await q.message.reply_text("No apps.")
        lines = ["🧾 Apps & Owners"]
        for tid, meta in ownership.items():
            lines.append(f"• {tid} -> {meta.get('owner')} | last_run={meta.get('last_run')}")
        return await q.message.reply_text("\n".join(lines[:120]))

    if q.data == "own__running":
        lines = ["🟢 Running Apps"]
        any_ = False
        for tid in ownership.keys():
            ok = tid in running_processes and running_processes[tid]["process"].poll() is None
            if ok:
                any_ = True
                lines.append(f"• {tid}")
        if not any_:
            lines.append("None")
        return await q.message.reply_text("\n".join(lines))

    if q.data == "own__down":
        lines = ["🔴 Down Apps (last_run=True but not running)"]
        any_ = False
        for tid, meta in ownership.items():
            if meta.get("last_run") is True:
                ok = tid in running_processes and running_processes[tid]["process"].poll() is None
                if not ok:
                    any_ = True
                    lines.append(f"• {tid}")
        if not any_:
            lines.append("None")
        return await q.message.reply_text("\n".join(lines))

    if q.data == "own__stopall":
        for tid in list(running_processes.keys()):
            stop_process(tid)
        return await q.message.reply_text("🛑 Stopped all apps.")

    if q.data == "own__restartall":
        auto_start_last_run_apps()
        return await q.message.reply_text("🔄 Restart requested for last-run apps.")


# ================= STATS / HELP =================
@restricted
async def server_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = len(load_ownership())
    running = sum(1 for tid in running_processes if running_processes[tid]["process"].poll() is None)
    await update.message.reply_text(f"📊 Apps: {total}\n🟢 Running: {running}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 Help\n"
        "• Upload File / Clone Repo\n"
        "• My Hosted Apps -> Manage\n"
        "• Owner Panel (owner only)\n"
    )


# ================= MAIN =================
if __name__ == "__main__":
    # Start Flask server for Render
    threading.Thread(target=run_flask, daemon=True).start()

    if not TOKEN:
        print("❌ ERROR: TOKEN env var not set")
        sys.exit(1)

    app_bot = ApplicationBuilder().token(TOKEN).build()

    # Auto-start apps that were running before restart
    auto_start_last_run_apps()

    # Schedule watchdog properly (NO event loop error now)
    async def watchdog_job(context: ContextTypes.DEFAULT_TYPE):
        await watchdog_check(context.application)

    if ENABLE_ALERTS:
        app_bot.job_queue.run_repeating(watchdog_job, interval=HEALTHCHECK_INTERVAL_SEC, first=10)

    # Conversations
    conv_file = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📤 Upload File$"), upload_start)],
        states={
            WAIT_FILE: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.Document.ALL, receive_file),
            ],
            WAIT_EXTRAS: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.Regex("^(🚀 RUN NOW|➕ Add Deps|📝 Type Env Vars)$"), receive_extras),
                MessageHandler(filters.Document.ALL, receive_extra_files),
            ],
            WAIT_ENV_TEXT: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.TEXT, receive_env_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    conv_git = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🌐 Clone from Git$"), git_start)],
        states={
            WAIT_URL: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.TEXT, receive_git_url),
            ],
            WAIT_GIT_EXTRAS: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.Regex("^(📝 Type Env Vars|📂 Select File to Run)$"), receive_git_extras),
            ],
            WAIT_GIT_ENV_TEXT: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.TEXT, receive_env_text),
            ],
            WAIT_SELECT_FILE: [CallbackQueryHandler(select_git_file, pattern=r"^sel_run__")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    # Handlers
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(conv_file)
    app_bot.add_handler(conv_git)

    app_bot.add_handler(MessageHandler(filters.Regex("^📂 My Hosted Apps$"), list_hosted))
    app_bot.add_handler(MessageHandler(filters.Regex("^📊 Server Stats$"), server_stats))
    app_bot.add_handler(MessageHandler(filters.Regex("^🛠 Owner Panel$"), owner_panel))
    app_bot.add_handler(MessageHandler(filters.Regex("^🆘 Help$"), help_command))

    app_bot.add_handler(CallbackQueryHandler(owner_callback, pattern=r"^own__"))
    app_bot.add_handler(CallbackQueryHandler(manage_callback, pattern=r"^(man__|stop__|rerun__|clrlog__|del__)"))

    print("Bot is up and running!")
    # NOTE: 409 Conflict means another instance is running with same token.
    app_bot.run_polling(drop_pending_updates=True)
