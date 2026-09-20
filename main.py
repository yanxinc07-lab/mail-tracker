import os
import base64
from datetime import datetime, timedelta
from fastapi import FastAPI, Response, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

# 允许跨域，彻底放行来自 Gmail 网页端的所有请求
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
            first_open_time TEXT
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

class RegisterRequest(BaseModel):
    id: str
    recipient: str = "-"
    subject: str = ""

@app.post("/api/register")
def register_mail(req: RegisterRequest):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO emails (id, recipient, subject, send_time, status, open_count, first_open_time)
        VALUES (%s, %s, %s, %s, '未读', 0, '-')
        ON CONFLICT (id) DO NOTHING;
    """, (req.id, req.recipient, req.subject, now_str))
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "ok"}

@app.get("/t/{track_id}.png")
def track_pixel(track_id: str, request: Request):
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    user_agent = request.headers.get("user-agent", "")
    
    # 基础响应头（禁止缓存）
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
        
        # 核心改动：30 秒内一律静默丢弃，不写数据库、不改状态、不留误扫杂音！
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
    else:
        # 如果是未注册邮件直接来访（比如意外请求），直接忽略不记
        pass

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

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(time_range: str = "all", custom_start: str = "", custom_end: str = ""):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    now = datetime.now()
    where_clause = ""
    params = []
    
    if custom_start and custom_end:
        where_clause = "WHERE send_time >= %s AND send_time <= %s"
        params.extend([f"{custom_start} 00:00:00", f"{custom_end} 23:59:59"])
    elif time_range == "today":
        where_clause = "WHERE send_time >= %s"
        params.append(now.strftime("%Y-%m-%d 00:00:00"))
    elif time_range == "yesterday":
        y_start = (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        y_end = (now - timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
        where_clause = "WHERE send_time >= %s AND send_time <= %s"
        params.extend([y_start, y_end])
    elif time_range == "week":
        where_clause = "WHERE send_time >= %s"
        params.append((now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00"))
    elif time_range == "month":
        where_clause = "WHERE send_time >= %s"
        params.append((now - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00"))
    elif time_range == "quarter":
        where_clause = "WHERE send_time >= %s"
        params.append((now - timedelta(days=90)).strftime("%Y-%m-%d 00:00:00"))

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

    def get_tab_style(target):
        active = "background:#2563eb; color:#fff; font-weight:600;"
        inactive = "background:#fff; color:#475569; border:1px solid #cbd5e1;"
        return active if time_range == target and not custom_start else inactive

    rows_html = ""
    for r in rows:
        email_id = r["id"]
        item_logs = logs_map.get(email_id, [])
        log_count = len(item_logs) if item_logs else (r["open_count"] or 0)

        st = r['status']
        if st == "已读":
            badge_class = "badge-read"
        elif st == "误扫":
            badge_class = "badge-scan"
        else:
            badge_class = "badge-unread"

        timeline_items = ""
        for idx, l in enumerate(item_logs, 1):
            tag = '<span style="color:#eab308; font-size:11px; margin-left:4px;">(系统扫)</span>' if l['is_scanner'] else ''
            timeline_items += f"""
            <div id="log-row-{l['log_id']}" style="display:flex; justify-content:space-between; align-items:center; padding:6px 0; font-size:12px; border-bottom:1px dashed #f1f5f9;">
                <span>#{idx} {l['open_time']}{tag}</span>
                <a href="javascript:void(0)" onclick="deleteLog({l['log_id']}, '{email_id}')" style="color:#ef4444; text-decoration:none; margin-left:12px; font-size:11px;">删除</a>
            </div>
            """
        
        timeline_html = ""
        if log_count > 0:
            timeline_html = f"""
            <span id="log-count-btn-{email_id}" style="font-size:11px; color:#2563eb; cursor:pointer; margin-left:6px; user-select:none;" onclick="toggleLogs(event, '{email_id}')">
                (共{log_count}次 ▾)
            </span>
            <div id="logs-{email_id}" class="popup-panel" style="display:none; position:absolute; z-index:100; background:#fff; border:1px solid #e2e8f0; border-radius:8px; box-shadow:0 8px 20px rgba(0,0,0,0.12); padding:12px 14px; width:280px; margin-top:4px;">
                <div style="font-weight:600; font-size:12px; margin-bottom:8px; color:#0f172a; border-bottom:1px solid #f1f5f9; padding-bottom:4px;">打开时间轴流水</div>
                <div id="log-list-{email_id}">
                    {timeline_items or '<div style="font-size:12px; color:#94a3b8;">暂无流水明细</div>'}
                </div>
            </div>
            """

        subj = r['subject'] or '无主题'
        recipient = r['recipient'] if (r['recipient'] and r['recipient'] != '-') else ''
        recipient_html = f'<div style="font-size: 12px; color: #64748b; margin-top: 4px; font-weight: normal;">👤 {recipient}</div>' if recipient else ''
        
        send_t = r['send_time']
        first_t = r['first_open_time']

        rows_html += f"""
        <tr style="border-bottom: 1px solid #f1f5f9; height: 56px;">
            <td style="padding: 12px 16px; color: #0f172a; font-weight: 500;">
                <div>{subj}</div>
                {recipient_html}
            </td>
            <td style="padding: 12px 16px; color: #64748b; font-size: 13px;">{send_t}</td>
            <td style="padding: 12px 16px;">
                <div style="position:relative; display:inline-block;">
                    <button id="btn-{email_id}" onclick="toggleMenu(event, '{email_id}')" class="status-btn {badge_class}">
                        <span id="txt-{email_id}">{st} ({log_count}次)</span> ▾
                    </button>
                    <div id="menu-{email_id}" class="popup-panel menu-box" style="display:none; position:absolute; z-index:100; left:0; margin-top:4px; background:#fff; border:1px solid #e2e8f0; border-radius:8px; box-shadow:0 8px 20px rgba(0,0,0,0.12); width:110px; overflow:hidden;">
                        <div class="menu-item" onclick="setStatus(event, '{email_id}', '已读', {log_count})" style="color:#15803d;">标为已读</div>
                        <div class="menu-item" onclick="setStatus(event, '{email_id}', '误扫', {log_count})" style="color:#a16207; border-top:1px solid #f8fafc;">标为误扫</div>
                        <div class="menu-item" onclick="setStatus(event, '{email_id}', '未读', {log_count})" style="color:#64748b; border-top:1px solid #f8fafc;">标为未读</div>
                    </div>
                </div>
            </td>
            <td style="padding: 12px 16px; color: #475569; font-size: 13px; position:relative;">
                <span id="first-time-{email_id}">{first_t}</span>{timeline_html}
            </td>
        </tr>
        """

    if not rows_html:
        rows_html = '<tr><td colspan="4" style="text-align:center; padding: 40px; color: #94a3b8;">该时间范围内暂无发信记录</td></tr>'

    tab_all = get_tab_style('all')
    tab_today = get_tab_style('today')
    tab_yesterday = get_tab_style('yesterday')
    tab_week = get_tab_style('week')
    tab_month = get_tab_style('month')
    tab_quarter = get_tab_style('quarter')

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>达人邮件追踪大盘</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f8fafc; margin: 0; padding: 40px; color: #334155; }}
        .container {{ max-width: 1000px; margin: 0 auto; }}
        .header {{ font-size: 24px; font-weight: bold; margin-bottom: 20px; color: #0f172a; }}
        .nav-bar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }}
        .tabs {{ display: flex; gap: 8px; }}
        .tab-btn {{ text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; transition: 0.1s; }}
        .custom-picker {{ display: flex; align-items: center; gap: 6px; font-size: 13px; }}
        .custom-picker input {{ border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 10px; font-size: 13px; outline: none; }}
        .custom-picker button {{ background: #2563eb; color: white; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; }}
        .cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 32px; }}
        .card {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }}
        .card-title {{ font-size: 13px; color: #64748b; margin-bottom: 8px; font-weight: 500; }}
        .card-value {{ font-size: 32px; font-weight: bold; color: #0f172a; }}
        .table-container {{ background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }}
        th {{ background: #f1f5f9; padding: 14px 16px; color: #0f172a; font-weight: 600; font-size: 13px; }}
        .table-title {{ padding: 20px; font-size: 16px; font-weight: bold; color: #0f172a; border-bottom: 1px solid #f1f5f9; }}
        .status-btn {{ cursor: pointer; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; outline: none; transition: 0.15s; user-select: none; }}
        .badge-read {{ background: #dcfce7; color: #15803d; border: 1px solid #86efac; }}
        .badge-scan {{ background: #fef9c3; color: #a16207; border: 1px solid #fde047; }}
        .badge-unread {{ background: #f1f5f9; color: #64748b; border: 1px solid #cbd5e1; }}
        .menu-item {{ padding: 9px 14px; font-size: 12px; cursor: pointer; transition: background 0.1s; user-select: none; }}
        .menu-item:hover {{ background: #f8fafc; font-weight: 600; }}
    </style>
    <script>
        document.addEventListener('click', function() {{
            document.querySelectorAll('.popup-panel').forEach(function(p) {{ p.style.display = 'none'; }});
        }});

        function toggleMenu(e, id) {{
            e.stopPropagation();
            var menu = document.getElementById('menu-' + id);
            var isShown = menu.style.display === 'block';
            document.querySelectorAll('.popup-panel').forEach(function(p) {{ p.style.display = 'none'; }});
            menu.style.display = isShown ? 'none' : 'block';
        }}

        function toggleLogs(e, id) {{
            e.stopPropagation();
            var panel = document.getElementById('logs-' + id);
            var isShown = panel.style.display === 'block';
            document.querySelectorAll('.popup-panel').forEach(function(p) {{ p.style.display = 'none'; }});
            panel.style.display = isShown ? 'none' : 'block';
        }}

        function setStatus(e, id, newStatus, count) {{
            e.stopPropagation();
            document.getElementById('menu-' + id).style.display = 'none';

            var btn = document.getElementById('btn-' + id);
            var txt = document.getElementById('txt-' + id);
            txt.innerText = newStatus + ' (' + count + '次)';

            var cls = 'status-btn ';
            if (newStatus === '已读') cls += 'badge-read';
            else if (newStatus === '误扫') cls += 'badge-scan';
            else cls += 'badge-unread';
            btn.className = cls;

            fetch('/api/update_status', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ id: id, status: newStatus }})
            }});
        }}

        function deleteLog(logId, emailId) {{
            if (!confirm("确定删除这条打开记录吗？")) return;
            
            fetch('/api/logs/' + logId, {{ method: 'DELETE' }})
                .then(function(res) {{ return res.json(); }})
                .then(function(data) {{
                    if (data.status === 'ok') {{
                        var row = document.getElementById('log-row-' + logId);
                        if (row) row.remove();

                        var txt = document.getElementById('txt-' + emailId);
                        if (txt) {{
                            var curStatus = txt.innerText.split(' ')[0];
                            txt.innerText = curStatus + ' (' + data.new_count + '次)';
                        }}

                        var ft = document.getElementById('first-time-' + emailId);
                        if (ft) ft.innerText = data.new_first_time;
                        
                        var countBtn = document.getElementById('log-count-btn-' + emailId);
                        if (countBtn) {{
                            if (data.new_count > 0) {{
                                countBtn.innerText = '(共' + data.new_count + '次 ▾)';
                            }} else {{
                                countBtn.style.display = 'none';
                            }}
                        }}
                    }}
                }});
        }}

        function applyCustomDate() {{
            var start = document.getElementById('startDate').value;
            var end = document.getElementById('endDate').value;
            if (!start || !end) {{
                alert("请选择起止日期");
                return;
            }}
            window.location.href = '/dashboard?custom_start=' + start + '&custom_end=' + end;
        }}
    </script>
</head>
<body>
    <div class="container">
        <div class="header">📧 达人邮件追踪大盘</div>
        <div class="nav-bar">
            <div class="tabs">
                <a href="/dashboard?time_range=all" class="tab-btn" style="{tab_all}">全部</a>
                <a href="/dashboard?time_range=today" class="tab-btn" style="{tab_today}">今天</a>
                <a href="/dashboard?time_range=yesterday" class="tab-btn" style="{tab_yesterday}">昨天</a>
                <a href="/dashboard?time_range=week" class="tab-btn" style="{tab_week}">7天</a>
                <a href="/dashboard?time_range=month" class="tab-btn" style="{tab_month}">30天</a>
                <a href="/dashboard?time_range=quarter" class="tab-btn" style="{tab_quarter}">90天</a>
            </div>
            <div class="custom-picker">
                <input type="date" id="startDate" value="{custom_start}">
                <span>至</span>
                <input type="date" id="endDate" value="{custom_end}">
                <button onclick="applyCustomDate()">筛选历史</button>
            </div>
        </div>
        <div class="cards">
            <div class="card">
                <div class="card-title">总发信量</div>
                <div class="card-value">{total_sent}</div>
            </div>
            <div class="card">
                <div class="card-title">真人有效打开数</div>
                <div class="card-value" style="color: #16a34a;">{total_opened}</div>
            </div>
            <div class="card">
                <div class="card-title">真实打开率</div>
                <div class="card-value" style="color: #2563eb;">{rate}</div>
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
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""
    return html
