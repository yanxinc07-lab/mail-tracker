import os
import io
import zipfile
import base64
from datetime import datetime, timedelta
from fastapi import FastAPI, Response, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return RedirectResponse(url="/dashboard")

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    if not DATABASE_URL:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS emails (
            id TEXT PRIMARY KEY,
            recipient TEXT DEFAULT '-',
            subject TEXT,
            send_time TEXT,
            status TEXT,
            open_count INTEGER DEFAULT 0,
            first_open_time TEXT,
            user_id TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_logs (
            log_id SERIAL PRIMARY KEY,
            email_id TEXT REFERENCES emails(id) ON DELETE CASCADE,
            open_time TEXT,
            is_scanner BOOLEAN DEFAULT FALSE
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print(f"Database init error: {e}")

PIXEL_GIF_BASE64 = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
PIXEL_DATA = base64.b64decode(PIXEL_GIF_BASE64)

# 动态打包插件下载接口
@app.get("/api/download_extension")
def download_extension():
    manifest_code = """{
  "manifest_version": 3,
  "name": "达人邮件追踪助手",
  "version": "1.0",
  "description": "Gmail 达人发信打开率智能追踪系统",
  "permissions": [
    "storage"
  ],
  "action": {
    "default_popup": "popup.html"
  },
  "content_scripts": [
    {
      "matches": ["https://mail.google.com/*"],
      "js": ["content.js"],
      "run_at": "document_idle"
    }
  ]
}"""

    popup_html_code = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {
      width: 280px;
      padding: 16px;
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f8fafc;
      color: #334155;
    }
    h3 {
      margin: 0 0 12px 0;
      font-size: 16px;
      color: #0f172a;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .input-box {
      width: 100%;
      box-sizing: border-box;
      padding: 8px 12px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      margin-bottom: 10px;
      font-size: 13px;
      outline: none;
    }
    .btn {
      width: 100%;
      background: #2563eb;
      color: white;
      border: none;
      padding: 8px 0;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
    }
    .btn:hover { background: #1d4ed8; }
    .status-panel { display: none; }
    .user-tag {
      background: #e0f2fe;
      color: #0284c7;
      padding: 8px 12px;
      border-radius: 6px;
      font-size: 12px;
      margin-bottom: 12px;
      word-break: break-all;
    }
    .logout-btn {
      background: #f1f5f9;
      color: #ef4444;
      border: 1px solid #cbd5e1;
      margin-top: 8px;
    }
    .msg {
      font-size: 12px;
      margin-top: 8px;
      text-align: center;
    }
  </style>
</head>
<body>
  <h3>📧 达人追踪助手</h3>

  <div id="loginView">
    <input type="email" id="emailInput" class="input-box" placeholder="输入大盘注册邮箱" />
    <input type="password" id="pwdInput" class="input-box" placeholder="输入密码" />
    <button id="loginBtn" class="btn">登 录 激 活</button>
    <div id="msgBox" class="msg"></div>
  </div>

  <div id="userView" class="status-panel">
    <div class="user-tag">
      <div>已连接账号：</div>
      <b id="displayEmail">-</b>
    </div>
    <button id="openDashBtn" class="btn">打开我的追踪大盘</button>
    <button id="logoutBtn" class="btn logout-btn">切换 / 退出账号</button>
  </div>

  <script src="popup.js"></script>
</body>
</html>"""

    popup_js_code = """const SUPABASE_URL = "https://hteqhquorjycjdxburun.supabase.co";
const SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh0ZXFocXVvcmp5Y2pkeGJ1cnVuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk2MTY5MjUsImV4cCI6MjEwNTE5MjkyNX0.JzTkH-xoiExeSxHGu420PgVfUNV_Jp_zZoJQn3yqY2g";

chrome.storage.local.get(["userId", "userEmail"], function (res) {
  if (res.userId && res.userEmail) {
    showUserView(res.userEmail);
  } else {
    showLoginView();
  }
});

function showUserView(email) {
  document.getElementById("loginView").style.display = "none";
  document.getElementById("userView").style.display = "block";
  document.getElementById("displayEmail").innerText = email;
}

function showLoginView() {
  document.getElementById("loginView").style.display = "block";
  document.getElementById("userView").style.display = "none";
  document.getElementById("emailInput").value = "";
  document.getElementById("pwdInput").value = "";
  document.getElementById("msgBox").innerText = "";
}

document.getElementById("loginBtn").addEventListener("click", async function () {
  const email = document.getElementById("emailInput").value.trim();
  const password = document.getElementById("pwdInput").value;
  const msgBox = document.getElementById("msgBox");

  if (!email || !password) {
    msgBox.style.color = "#ef4444";
    msgBox.innerText = "请填写邮箱和密码";
    return;
  }

  msgBox.style.color = "#2563eb";
  msgBox.innerText = "正在验证...";

  try {
    const resp = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=password`, {
      method: "POST",
      headers: {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ email, password })
    });

    const data = await resp.json();

    if (!resp.ok) {
      msgBox.style.color = "#ef4444";
      msgBox.innerText = "登录失败: " + (data.error_description || data.msg || "密码错误");
      return;
    }

    const userId = data.user.id;
    const userEmail = data.user.email;

    chrome.storage.local.set({ userId: userId, userEmail: userEmail }, function () {
      showUserView(userEmail);
    });
  } catch (err) {
    msgBox.style.color = "#ef4444";
    msgBox.innerText = "网络请求失败，请稍后重试";
  }
});

document.getElementById("logoutBtn").addEventListener("click", function () {
  chrome.storage.local.remove(["userId", "userEmail"], function () {
    showLoginView();
  });
});

document.getElementById("openDashBtn").addEventListener("click", function () {
  chrome.tabs.create({ url: "https://mail-tracker-e7da.onrender.com/dashboard" });
});"""

    content_js_code = """document.addEventListener("click", function (e) {
  const sendBtn = e.target.closest('div[role="button"][data-tooltip*="发送"], div[role="button"][data-tooltip*="Send"], div[aria-label*="Send"], div[aria-label*="发送"]');
  if (!sendBtn) return;

  const composeBox = sendBtn.closest('div[role="region"], div[aria-label*="写信"], div[aria-label*="Compose"]') || document.body;

  let recipientEmail = "-";
  const emailChips = composeBox.querySelectorAll('[email]');
  if (emailChips.length > 0) {
    const list = Array.from(emailChips).map(el => el.getAttribute('email')).filter(Boolean);
    if (list.length > 0) recipientEmail = list.join(", ");
  }
  
  if (recipientEmail === "-") {
    const hiddenTo = composeBox.querySelector('input[name="to"]');
    if (hiddenTo && hiddenTo.value) recipientEmail = hiddenTo.value;
  }

  const subjectInput = composeBox.querySelector('input[name="subjectbox"]');
  const subject = subjectInput ? (subjectInput.value.trim() || "无主题邮件") : "无主题邮件";

  const trackId = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
    const r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });

  const editableBody = composeBox.querySelector('div[aria-label*="邮件正文"], div[aria-label*="Message Body"], div[role="textbox"]');
  if (editableBody) {
    const img = document.createElement("img");
    img.src = `https://mail-tracker-e7da.onrender.com/t/${trackId}.png`;
    img.width = 1;
    img.height = 1;
    img.style.display = "none";
    editableBody.appendChild(img);
  }

  chrome.storage.local.get(["userId"], function (res) {
    const currentUserId = res.userId || null;

    fetch('https://mail-tracker-e7da.onrender.com/api/register', {
      method: 'POST',
      mode: 'cors',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: trackId,
        recipient: recipientEmail,
        subject: subject,
        user_id: currentUserId
      })
    }).catch(err => console.error("Register err:", err));
  });

}, true);"""

    # 动态在内存中打包 zip
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("mail-tracker-extension/manifest.json", manifest_code)
        zip_file.writestr("mail-tracker-extension/popup.html", popup_html_code)
        zip_file.writestr("mail-tracker-extension/popup.js", popup_js_code)
        zip_file.writestr("mail-tracker-extension/content.js", content_js_code)
    
    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=mail-tracker-extension.zip"}
    )

class RegisterRequest(BaseModel):
    id: str
    recipient: str = "-"
    subject: str = ""
    user_id: str = None

@app.post("/api/register")
def register_mail(req: RegisterRequest):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO emails (id, recipient, subject, send_time, status, open_count, first_open_time, user_id)
        VALUES (%s, %s, %s, %s, '未读', 0, '-', %s)
        ON CONFLICT (id) DO NOTHING;
    """, (req.id, req.recipient, req.subject, now_str, req.user_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "ok"}

@app.get("/t/{track_id}.png")
def track_pixel(track_id: str, request: Request):
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0"
    }

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM emails WHERE id = %s;", (track_id,))
    row = cur.fetchone()

    if row:
        send_dt = datetime.strptime(row["send_time"], "%Y-%m-%d %H:%M:%S")
        diff_seconds = (now - send_dt).total_seconds()
        
        if diff_seconds < 30:
            cur.close()
            conn.close()
            return Response(content=PIXEL_DATA, media_type="image/gif", headers=headers)

        is_scanner = False

        cur.execute("INSERT INTO email_logs (email_id, open_time, is_scanner) VALUES (%s, %s, %s);", (track_id, now_str, is_scanner))

        new_count = (row["open_count"] or 0) + 1
        first_time = row["first_open_time"] if row["first_open_time"] != "-" else now_str
        
        new_status = row["status"]
        if new_status == "未读":
            new_status = "误扫" if is_scanner else "已读"
        elif new_status == "误扫" and not is_scanner:
            new_status = "已读"

        cur.execute("""
            UPDATE emails 
            SET status = %s, open_count = %s, first_open_time = %s 
            WHERE id = %s;
        """, (new_status, new_count, first_time, track_id))

        conn.commit()

    cur.close()
    conn.close()
    return Response(content=PIXEL_DATA, media_type="image/gif", headers=headers)

class UpdateStatusRequest(BaseModel):
    id: str
    status: str

@app.post("/api/update_status")
def update_status(req: UpdateStatusRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE emails SET status = %s WHERE id = %s;", (req.status, req.id))
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/logs/{log_id}")
def delete_log(log_id: int):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT email_id FROM email_logs WHERE log_id = %s;", (log_id,))
    log = cur.fetchone()
    if not log:
        cur.close()
        conn.close()
        return JSONResponse(status_code=404, content={"error": "Not found"})

    email_id = log["email_id"]
    cur.execute("DELETE FROM email_logs WHERE log_id = %s;", (log_id,))

    cur.execute("SELECT open_time FROM email_logs WHERE email_id = %s ORDER BY open_time ASC;", (email_id,))
    remaining_logs = cur.fetchall()
    
    new_count = len(remaining_logs)
    new_first_time = remaining_logs[0]["open_time"] if new_count > 0 else "-"
    new_status = "已读" if new_count > 0 else "未读"

    cur.execute("""
        UPDATE emails 
        SET open_count = %s, first_open_time = %s, status = %s 
        WHERE id = %s;
    """, (new_count, new_first_time, new_status, email_id))

    conn.commit()
    cur.close()
    conn.close()
    return {"status": "ok", "new_count": new_count, "new_first_time": new_first_time}

@app.get("/api/dashboard_data")
def get_dashboard_data(time_range: str = "all", custom_start: str = "", custom_end: str = "", user_id: str = ""):
    if not user_id:
        return {"rows": [], "total_sent": 0, "total_opened": 0, "rate": "0.0%", "logs_map": {}}

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    now = datetime.now()
    
    where_parts = ["user_id = %s"]
    params = [user_id]
    
    if custom_start and custom_end:
        where_parts.append("send_time >= %s AND send_time <= %s")
        params.extend([f"{custom_start} 00:00:00", f"{custom_end} 23:59:59"])
    elif time_range == "today":
        where_parts.append("send_time >= %s")
        params.append(now.strftime("%Y-%m-%d 00:00:00"))
    elif time_range == "yesterday":
        y_start = (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        y_end = (now - timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
        where_parts.append("send_time >= %s AND send_time <= %s")
        params.extend([y_start, y_end])
    elif time_range == "week":
        where_parts.append("send_time >= %s")
        params.append((now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00"))
    elif time_range == "month":
        where_parts.append("send_time >= %s")
        params.append((now - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00"))
    elif time_range == "quarter":
        where_parts.append("send_time >= %s")
        params.append((now - timedelta(days=90)).strftime("%Y-%m-%d 00:00:00"))

    where_clause = "WHERE " + " AND ".join(where_parts)
    cur.execute(f"SELECT * FROM emails {where_clause} ORDER BY send_time DESC;", tuple(params))
    rows = cur.fetchall()

    email_ids = [r["id"] for r in rows]
    logs_map = {}
    if email_ids:
        cur.execute("SELECT * FROM email_logs WHERE email_id = ANY(%s) ORDER BY open_time ASC;", (email_ids,))
        all_logs = cur.fetchall()
        for log in all_logs:
            logs_map.setdefault(log["email_id"], []).append(log)

    cur.close()
    conn.close()

    total_sent = len(rows)
    total_opened = sum(1 for r in rows if r["status"] == "已读")
    rate = f"{(total_opened / total_sent * 100):.1f}%" if total_sent > 0 else "0.0%"

    return {
        "rows": rows,
        "logs_map": logs_map,
        "total_sent": total_sent,
        "total_opened": total_opened,
        "rate": rate
    }

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>达人邮件追踪大盘</title>
    <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f8fafc; margin: 0; padding: 40px; color: #334155; }
        .container { max-width: 1000px; margin: 0 auto; display: none; }
        .header-box { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .header { font-size: 24px; font-weight: bold; color: #0f172a; }
        .user-info { font-size: 13px; color: #64748b; display: flex; align-items: center; gap: 12px; }
        .logout-btn { background: #f1f5f9; border: 1px solid #cbd5e1; padding: 4px 10px; border-radius: 6px; cursor: pointer; color: #ef4444; font-size: 12px; }
        .download-btn { background: #10b981; color: white; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600; text-decoration: none; display: inline-flex; align-items: center; gap: 4px; }
        .download-btn:hover { background: #059669; }
        .nav-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }
        .tabs { display: flex; gap: 8px; }
        .tab-btn { text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; transition: 0.1s; cursor: pointer; background: #fff; color: #475569; border: 1px solid #cbd5e1; }
        .tab-btn.active { background: #2563eb; color: #fff; font-weight: 600; border-color: #2563eb; }
        .custom-picker { display: flex; align-items: center; gap: 6px; font-size: 13px; }
        .custom-picker input { border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 10px; font-size: 13px; outline: none; }
        .custom-picker button { background: #2563eb; color: white; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
        .cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 32px; }
        .card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }
        .card-title { font-size: 13px; color: #64748b; margin-bottom: 8px; font-weight: 500; }
        .card-value { font-size: 32px; font-weight: bold; color: #0f172a; }
        .table-container { background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }
        th { background: #f1f5f9; padding: 14px 16px; color: #0f172a; font-weight: 600; font-size: 13px; }
        .table-title { padding: 20px; font-size: 16px; font-weight: bold; color: #0f172a; border-bottom: 1px solid #f1f5f9; }
        .status-btn { cursor: pointer; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; outline: none; transition: 0.15s; user-select: none; }
        .badge-read { background: #dcfce7; color: #15803d; border: 1px solid #86efac; }
        .badge-scan { background: #fef9c3; color: #a16207; border: 1px solid #fde047; }
        .badge-unread { background: #f1f5f9; color: #64748b; border: 1px solid #cbd5e1; }
        .menu-item { padding: 9px 14px; font-size: 12px; cursor: pointer; transition: background 0.1s; user-select: none; }
        .menu-item:hover { background: #f8fafc; font-weight: 600; }

        #authOverlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: #f8fafc; display: flex; justify-content: center; align-items: center; z-index: 9999; }
        .auth-card { background: white; border: 1px solid #e2e8f0; border-radius: 16px; padding: 32px; width: 340px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); }
        .auth-card h2 { margin: 0 0 8px 0; font-size: 20px; color: #0f172a; text-align: center; }
        .auth-card p { margin: 0 0 20px 0; font-size: 13px; color: #64748b; text-align: center; }
        .auth-input { width: 100%; box-sizing: border-box; padding: 10px 14px; border: 1px solid #cbd5e1; border-radius: 8px; margin-bottom: 14px; font-size: 14px; outline: none; }
        .auth-btn { width: 100%; background: #2563eb; color: white; border: none; padding: 10px 0; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; }
        .auth-toggle { margin-top: 16px; text-align: center; font-size: 12px; color: #64748b; }
        .auth-toggle a { color: #2563eb; text-decoration: none; cursor: pointer; font-weight: 600; }
        #authMsg { font-size: 12px; margin-top: 10px; text-align: center; }
        .ext-download-box { margin-top: 24px; padding-top: 20px; border-top: 1px dashed #e2e8f0; text-align: center; }
    </style>
    <script>
        const SUPABASE_URL = "https://hteqhquorjycjdxburun.supabase.co";
        const SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh0ZXFocXVvcmp5Y2pkeGJ1cnVuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk2MTY5MjUsImV4cCI6MjEwNTE5MjkyNX0.JzTkH-xoiExeSxHGu420PgVfUNV_Jp_zZoJQn3yqY2g";
        const client = supabase.createClient(SUPABASE_URL, SUPABASE_KEY);

        let currentUser = null;
        let currentTimeRange = "all";
        let isRegisterMode = false;

        async function checkAuth() {
            const { data: { session } } = await client.auth.getSession();
            if (session && session.user) {
                currentUser = session.user;
                document.getElementById('authOverlay').style.display = 'none';
                document.getElementById('mainContainer').style.display = 'block';
                document.getElementById('userEmailText').innerText = currentUser.email;
                loadData();
            } else {
                currentUser = null;
                document.getElementById('authOverlay').style.display = 'flex';
                document.getElementById('mainContainer').style.display = 'none';
            }
        }

        function toggleMode() {
            isRegisterMode = !isRegisterMode;
            document.getElementById('authTitle').innerText = isRegisterMode ? '注册新账号' : '登录系统';
            document.getElementById('authBtn').innerText = isRegisterMode ? '立即注册' : '登 录';
            document.getElementById('modeText').innerText = isRegisterMode ? '已有账号？' : '还没有账号？';
            document.getElementById('modeBtn').innerText = isRegisterMode ? '去登录' : '立即注册';
            document.getElementById('authMsg').innerText = '';
        }

        async function handleAuth() {
            const email = document.getElementById('authEmail').value.trim();
            const password = document.getElementById('authPassword').value;
            const msgEl = document.getElementById('authMsg');
            if (!email || !password) {
                msgEl.style.color = '#ef4444';
                msgEl.innerText = '请填写邮箱和密码';
                return;
            }
            msgEl.style.color = '#2563eb';
            msgEl.innerText = isRegisterMode ? '正在注册...' : '正在登录...';

            if (isRegisterMode) {
                const { data, error } = await client.auth.signUp({ email, password });
                if (error) {
                    msgEl.style.color = '#ef4444';
                    msgEl.innerText = error.message;
                } else {
                    msgEl.style.color = '#16a34a';
                    msgEl.innerText = '注册成功！正在进入...';
                    setTimeout(checkAuth, 1000);
                }
            } else {
                const { data, error } = await client.auth.signInWithPassword({ email, password });
                if (error) {
                    msgEl.style.color = '#ef4444';
                    msgEl.innerText = '登录失败: ' + error.message;
                } else {
                    checkAuth();
                }
            }
        }

        async function handleLogout() {
            await client.auth.signOut();
            window.location.reload();
        }

        async function loadData(customStart = "", customEnd = "") {
            if (!currentUser) return;
            let url = `/api/dashboard_data?user_id=${currentUser.id}&time_range=${currentTimeRange}`;
            if (customStart && customEnd) {
                url += `&custom_start=${customStart}&custom_end=${customEnd}`;
            }

            const res = await fetch(url);
            const data = await res.json();

            document.getElementById('statTotalSent').innerText = data.total_sent;
            document.getElementById('statTotalOpened').innerText = data.total_opened;
            document.getElementById('statRate').innerText = data.rate;

            const tbody = document.getElementById('tableBody');
            if (!data.rows || data.rows.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; padding: 40px; color: #94a3b8;">暂无发信记录</td></tr>';
                return;
            }

            let html = '';
            for (let r of data.rows) {
                const emailId = r.id;
                const itemLogs = data.logs_map[emailId] || [];
                const logCount = itemLogs.length > 0 ? itemLogs.length : (r.open_count || 0);

                let badgeClass = 'badge-unread';
                if (r.status === '已读') badgeClass = 'badge-read';
                else if (r.status === '误扫') badgeClass = 'badge-scan';

                let timelineItems = '';
                itemLogs.forEach((l, idx) => {
                    const tag = l.is_scanner ? '<span style="color:#eab308; font-size:11px; margin-left:4px;">(系统扫)</span>' : '';
                    timelineItems += `
                    <div id="log-row-${l.log_id}" style="display:flex; justify-content:space-between; align-items:center; padding:6px 0; font-size:12px; border-bottom:1px dashed #f1f5f9;">
                        <span>#${idx+1} ${l.open_time}${tag}</span>
                        <a href="javascript:void(0)" onclick="deleteLog(${l.log_id}, '${emailId}')" style="color:#ef4444; text-decoration:none; margin-left:12px; font-size:11px;">删除</a>
                    </div>`;
                });

                let timelineHtml = '';
                if (logCount > 0) {
                    timelineHtml = `
                    <span id="log-count-btn-${emailId}" style="font-size:11px; color:#2563eb; cursor:pointer; margin-left:6px; user-select:none;" onclick="toggleLogs(event, '${emailId}')">
                        (共${logCount}次 ▾)
                    </span>
                    <div id="logs-${emailId}" class="popup-panel" style="display:none; position:absolute; z-index:100; background:#fff; border:1px solid #e2e8f0; border-radius:8px; box-shadow:0 8px 20px rgba(0,0,0,0.12); padding:12px 14px; width:280px; margin-top:4px;">
                        <div style="font-weight:600; font-size:12px; margin-bottom:8px; color:#0f172a; border-bottom:1px solid #f1f5f9; padding-bottom:4px;">打开时间轴流水</div>
                        <div id="log-list-${emailId}">
                            ${timelineItems || '<div style="font-size:12px; color:#94a3b8;">暂无流水明细</div>'}
                        </div>
                    </div>`;
                }

                const subj = r.subject || '无主题';
                const recipient = (r.recipient && r.recipient !== '-') ? `<div style="font-size: 12px; color: #64748b; margin-top: 4px; font-weight: normal;">👤 ${r.recipient}</div>` : '';

                html += `
                <tr style="border-bottom: 1px solid #f1f5f9; height: 56px;">
                    <td style="padding: 12px 16px; color: #0f172a; font-weight: 500;">
                        <div>${subj}</div>
                        ${recipient}
                    </td>
                    <td style="padding: 12px 16px; color: #64748b; font-size: 13px;">${r.send_time}</td>
                    <td style="padding: 12px 16px;">
                        <div style="position:relative; display:inline-block;">
                            <button id="btn-${emailId}" onclick="toggleMenu(event, '${emailId}')" class="status-btn ${badgeClass}">
                                <span id="txt-${emailId}">${r.status} (${logCount}次)</span> ▾
                            </button>
                            <div id="menu-${emailId}" class="popup-panel menu-box" style="display:none; position:absolute; z-index:100; left:0; margin-top:4px; background:#fff; border:1px solid #e2e8f0; border-radius:8px; box-shadow:0 8px 20px rgba(0,0,0,0.12); width:110px; overflow:hidden;">
                                <div class="menu-item" onclick="setStatus(event, '${emailId}', '已读', ${logCount})" style="color:#15803d;">标为已读</div>
                                <div class="menu-item" onclick="setStatus(event, '${emailId}', '误扫', ${logCount})" style="color:#a16207; border-top:1px solid #f8fafc;">标为误扫</div>
                                <div class="menu-item" onclick="setStatus(event, '${emailId}', '未读', ${logCount})" style="color:#64748b; border-top:1px solid #f8fafc;">标为未读</div>
                            </div>
                        </div>
                    </td>
                    <td style="padding: 12px 16px; color: #475569; font-size: 13px; position:relative;">
                        <span id="first-time-${emailId}">${r.first_open_time}</span>${timelineHtml}
                    </td>
                </tr>`;
            }
            tbody.innerHTML = html;
        }

        function switchTab(range) {
            currentTimeRange = range;
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.range === range);
            });
            document.getElementById('startDate').value = '';
            document.getElementById('endDate').value = '';
            loadData();
        }

        function applyCustomDate() {
            const start = document.getElementById('startDate').value;
            const end = document.getElementById('endDate').value;
            if (!start || !end) {
                alert("请选择起止日期");
                return;
            }
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            loadData(start, end);
        }

        window.addEventListener('DOMContentLoaded', checkAuth);

        document.addEventListener('click', function() {
            document.querySelectorAll('.popup-panel').forEach(p => p.style.display = 'none');
        });

        function toggleMenu(e, id) {
            e.stopPropagation();
            const menu = document.getElementById('menu-' + id);
            const isShown = menu.style.display === 'block';
            document.querySelectorAll('.popup-panel').forEach(p => p.style.display = 'none');
            menu.style.display = isShown ? 'none' : 'block';
        }

        function toggleLogs(e, id) {
            e.stopPropagation();
            const panel = document.getElementById('logs-' + id);
            const isShown = panel.style.display === 'block';
            document.querySelectorAll('.popup-panel').forEach(p => p.style.display = 'none');
            panel.style.display = isShown ? 'none' : 'block';
        }

        function setStatus(e, id, newStatus, count) {
            e.stopPropagation();
            document.getElementById('menu-' + id).style.display = 'none';

            const btn = document.getElementById('btn-' + id);
            const txt = document.getElementById('txt-' + id);
            txt.innerText = `${newStatus} (${count}次)`;

            let cls = 'status-btn ';
            if (newStatus === '已读') cls += 'badge-read';
            else if (newStatus === '误扫') cls += 'badge-scan';
            else cls += 'badge-unread';
            btn.className = cls;

            fetch('/api/update_status', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: id, status: newStatus })
            });
        }

        function deleteLog(logId, emailId) {
            if (!confirm("确定删除这条打开记录吗？")) return;
            
            fetch('/api/logs/' + logId, { method: 'DELETE' })
                .then(res => res.json())
                .then(data => {
                    if (data.status === 'ok') {
                        const row = document.getElementById('log-row-' + logId);
                        if (row) row.remove();

                        const txt = document.getElementById('txt-' + emailId);
                        if (txt) {
                            const curStatus = txt.innerText.split(' ')[0];
                            txt.innerText = `${curStatus} (${data.new_count}次)`;
                        }

                        const ft = document.getElementById('first-time-' + emailId);
                        if (ft) ft.innerText = data.new_first_time;
                        
                        const countBtn = document.getElementById('log-count-btn-' + emailId);
                        if (countBtn) {
                            if (data.new_count > 0) {
                                countBtn.innerText = `(共${data.new_count}次 ▾)`;
                            } else {
                                countBtn.style.display = 'none';
                            }
                        }
                    }
                });
        }
    </script>
</head>
<body>
    <div id="authOverlay">
        <div class="auth-card">
            <h2 id="authTitle">登录系统</h2>
            <p>达人邮件追踪商业看板</p>
            <input type="email" id="authEmail" class="auth-input" placeholder="请输入你的邮箱地址" />
            <input type="password" id="authPassword" class="auth-input" placeholder="请输入密码 (至少6位)" />
            <button id="authBtn" class="auth-btn" onclick="handleAuth()">登 录</button>
            <div id="authMsg"></div>
            <div class="auth-toggle">
                <span id="modeText">还没有账号？</span>
                <a id="modeBtn" onclick="toggleMode()">立即注册</a>
            </div>
            <div class="ext-download-box">
                <a href="/api/download_extension" class="download-btn" style="width:100%; justify-content:center; box-sizing:border-box; padding:10px 0;">
                    📥 下载 Chrome / Edge 插件
                </a>
            </div>
        </div>
    </div>

    <div class="container" id="mainContainer">
        <div class="header-box">
            <div class="header">📧 达人邮件追踪大盘</div>
            <div class="user-info">
                <a href="/api/download_extension" class="download-btn">📥 下载插件包</a>
                <span>当前账号: <b id="userEmailText" style="color: #0f172a;">-</b></span>
                <button class="logout-btn" onclick="handleLogout()">退出</button>
            </div>
        </div>
        <div class="nav-bar">
            <div class="tabs">
                <button class="tab-btn active" data-range="all" onclick="switchTab('all')">全部</button>
                <button class="tab-btn" data-range="today" onclick="switchTab('today')">今天</button>
                <button class="tab-btn" data-range="yesterday" onclick="switchTab('yesterday')">昨天</button>
                <button class="tab-btn" data-range="week" onclick="switchTab('week')">7天</button>
                <button class="tab-btn" data-range="month" onclick="switchTab('month')">30天</button>
                <button class="tab-btn" data-range="quarter" onclick="switchTab('quarter')">90天</button>
            </div>
            <div class="custom-picker">
                <input type="date" id="startDate">
                <span>至</span>
                <input type="date" id="endDate">
                <button onclick="applyCustomDate()">筛选历史</button>
            </div>
        </div>
        <div class="cards">
            <div class="card">
                <div class="card-title">总发信量</div>
                <div class="card-value" id="statTotalSent">0</div>
            </div>
            <div class="card">
                <div class="card-title">真人有效打开数</div>
                <div class="card-value" id="statTotalOpened" style="color: #16a34a;">0</div>
            </div>
            <div class="card">
                <div class="card-title">真实打开率</div>
                <div class="card-value" id="statRate" style="color: #2563eb;">0.0%</div>
            </div>
        </div>
        <div class="table-container">
            <div class="table-title">发信明细列表</div>
            <table>
                <thead>
                    <tr>
                        <th style="width: 44%;">邮件主题与收件人</th>
                        <th style="width: 20%;">发送时间</th>
                        <th style="width: 16%;">状态 (点击修改)</th>
                        <th style="width: 20%;">首次打开 (流水)</th>
                    </tr>
                </thead>
                <tbody id="tableBody">
                    <tr><td colspan="4" style="text-align:center; padding: 40px; color: #94a3b8;">加载中...</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""
    return html
