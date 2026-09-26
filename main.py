import os
import io
import zipfile
import base64
from datetime import datetime, timedelta
from fastapi import FastAPI, Response, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
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

# 挂载静态资源（让 style.css 可以正常通过 /static/style.css 访问）
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

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

# 读取 templates 里的 index.html（原生读取，零第三方依赖）
@app.get("/", response_class=HTMLResponse)
def landing_page():
    path = os.path.join("templates", "index.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h3>首页文件加载中，请稍后刷新</h3>"

# 读取 templates 里的 dashboard.html（原生读取，零第三方依赖）
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    path = os.path.join("templates", "dashboard.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h3>大盘文件加载中，请稍后刷新</h3>"

# 404 全局防呆兜底：任何错误地址直接跳回官网首页
@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return RedirectResponse(url="/")
    return Response(content=str(exc.detail), status_code=exc.status_code)

# 动态打包插件下载接口（单层平级解压 + 快捷键与鼠标双支持）
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
    body { width: 280px; padding: 16px; margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8fafc; color: #334155; }
    h3 { margin: 0 0 12px 0; font-size: 16px; color: #0f172a; display: flex; align-items: center; gap: 6px; }
    .input-box { width: 100%; box-sizing: border-box; padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 6px; margin-bottom: 10px; font-size: 13px; outline: none; }
    .btn { width: 100%; background: #2563eb; color: white; border: none; padding: 8px 0; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; }
    .btn:hover { background: #1d4ed8; }
    .status-panel { display: none; }
    .user-tag { background: #e0f2fe; color: #0284c7; padding: 8px 12px; border-radius: 6px; font-size: 12px; margin-bottom: 12px; word-break: break-all; }
    .logout-btn { background: #f1f5f9; color: #ef4444; border: 1px solid #cbd5e1; margin-top: 8px; }
    .msg { font-size: 12px; margin-top: 8px; text-align: center; }
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
      headers: { "apikey": SUPABASE_KEY, "Content-Type": "application/json" },
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

    content_js_code = """function findActiveComposeBox(activeEl) {
  if (activeEl) {
    const box = activeEl.closest('div[role="region"], div[aria-label*="写信"], div[aria-label*="Compose"], table.aoI, div.M9');
    if (box) return box;
  }
  const allBoxes = document.querySelectorAll('div[role="region"], div[aria-label*="写信"], div[aria-label*="Compose"], table.aoI');
  for (let b of allBoxes) {
    if (b.offsetWidth > 0 && b.offsetHeight > 0) return b;
  }
  return document.body;
}

function triggerTrack(composeBox) {
  if (!composeBox || composeBox.__tracked_sending) return;
  composeBox.__tracked_sending = true;
  setTimeout(() => { composeBox.__tracked_sending = false; }, 3000);

  let recipientEmail = "-";
  const emailChips = composeBox.querySelectorAll('[email]');
  if (emailChips.length > 0) {
    const list = Array.from(emailChips).map(el => el.getAttribute('email')).filter(Boolean);
    if (list.length > 0) recipientEmail = list.join(", ");
  }
  
  if (recipientEmail === "-") {
    const hiddenTo = composeBox.querySelector('input[name="to"], textarea[name="to"], [aria-label*="收件人"], [aria-label*="To"]');
    if (hiddenTo && (hiddenTo.value || hiddenTo.innerText)) {
      recipientEmail = (hiddenTo.value || hiddenTo.innerText).trim();
    }
  }

  const subjectInput = composeBox.querySelector('input[name="subjectbox"], input[name="subject"]');
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
}

document.addEventListener("click", function (e) {
  const sendBtn = e.target.closest('div[role="button"][data-tooltip*="发送"], div[role="button"][data-tooltip*="Send"], div[aria-label*="Send"], div[aria-label*="发送"], .T-I-atl');
  if (!sendBtn) return;
  const composeBox = findActiveComposeBox(sendBtn);
  triggerTrack(composeBox);
}, true);

window.addEventListener("keydown", function (e) {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    const composeBox = findActiveComposeBox(document.activeElement);
    triggerTrack(composeBox);
  }
}, true);"""

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("manifest.json", manifest_code)
        zip_file.writestr("popup.html", popup_html_code)
        zip_file.writestr("popup.js", popup_js_code)
        zip_file.writestr("content.js", content_js_code)
    
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
