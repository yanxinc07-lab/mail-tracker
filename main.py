import os
import base64
from datetime import datetime, timedelta
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

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
            recipient TEXT,
            subject TEXT,
            send_time TEXT,
            status TEXT,
            open_count INTEGER DEFAULT 0,
            first_open_time TEXT
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
    recipient: str = ""
    subject: str = ""

@app.post("/api/register")
def register_mail(req: RegisterRequest):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO emails (id, recipient, subject, send_time, status, open_count, first_open_time)
        VALUES (%s, %s, %s, %s, '未读', 0, '-')
        ON CONFLICT (id) DO UPDATE SET 
            recipient = EXCLUDED.recipient,
            subject = EXCLUDED.subject;
    """, (req.id, req.recipient, req.subject, now_str))
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "ok"}

@app.get("/t/{track_id}.png")
def track_pixel(track_id: str):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM emails WHERE id = %s;", (track_id,))
    row = cur.fetchone()
    if row:
        new_count = (row["open_count"] or 0) + 1
        first_time = row["first_open_time"] if row["first_open_time"] != "-" else now_str
        cur.execute("""
            UPDATE emails 
            SET status = '已读', open_count = %s, first_open_time = %s 
            WHERE id = %s;
        """, (new_count, first_time, track_id))
        conn.commit()
    cur.close()
    conn.close()
    return Response(
        content=PIXEL_DATA, 
        media_type="image/gif",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

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

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(time_range: str = "all"):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 时间筛选逻辑
    now = datetime.now()
    where_clause = ""
    params = []
    
    if time_range == "today":
        start_date = now.strftime("%Y-%m-%d 00:00:00")
        where_clause = "WHERE send_time >= %s"
        params.append(start_date)
    elif time_range == "yesterday":
        yesterday_start = (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        yesterday_end = (now - timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
        where_clause = "WHERE send_time >= %s AND send_time <= %s"
        params.extend([yesterday_start, yesterday_end])
    elif time_range == "week":
        start_date = (now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        where_clause = "WHERE send_time >= %s"
        params.append(start_date)
    elif time_range == "month":
        start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
        where_clause = "WHERE send_time >= %s"
        params.append(start_date)

    query = f"SELECT * FROM emails {where_clause} ORDER BY send_time DESC;"
    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total_sent = len(rows)
    total_opened = sum(1 for r in rows if r["status"] == "已读")
    rate = f"{(total_opened / total_sent * 100):.1f}%" if total_sent > 0 else "0.0%"

    def get_tab_style(target):
        active = "background:#2563eb; color:#fff; font-weight:600;"
        inactive = "background:#fff; color:#475569; border:1px solid #cbd5e1;"
        return active if time_range == target else inactive

    rows_html = ""
    for r in rows:
        st = r['status']
        if st == "已读":
            status_color = "color: #16a34a; font-weight: bold;"
            badge = f"已读 ({r['open_count']}次)"
        elif st == "误扫":
            status_color = "color: #eab308; font-weight: bold;"
            badge = f"误扫 ({r['open_count']}次)"
        else:
            status_color = "color: #94a3b8;"
            badge = f"未读 ({r['open_count']}次)" if (r['open_count'] or 0) > 0 else "未读"

        recipient_display = r['recipient'] if (r['recipient'] and r['recipient'].strip()) else "-"

        rows_html += f"""
        <tr style="border-bottom: 1px solid #f1f5f9; height: 50px;">
            <td style="padding: 12px 16px; color: #1e293b; font-weight: 500;">{recipient_display}</td>
            <td style="padding: 12px 16px; color: #334155;">{r['subject'] or '-'}</td>
            <td style="padding: 12px 16px; color: #64748b; font-size: 13px;">{r['send_time']}</td>
            <td style="padding: 12px 16px; {status_color}">{badge}</td>
            <td style="padding: 12px 16px; color: #64748b; font-size: 13px;">{r['first_open_time']}</td>
            <td style="padding: 12px 16px;">
                <button onclick="setStatus('{r['id']}', '已读')" style="cursor:pointer; margin-right:4px; padding:4px 8px; border:1px solid #e2e8f0; border-radius:4px; background:#fff; font-size:12px;">标已读</button>
                <button onclick="setStatus('{r['id']}', '误扫')" style="cursor:pointer; margin-right:4px; padding:4px 8px; border:1px solid #e2e8f0; border-radius:4px; background:#fff; font-size:12px;">标误扫</button>
                <button onclick="setStatus('{r['id']}', '未读')" style="cursor:pointer; padding:4px 8px; border:1px solid #e2e8f0; border-radius:4px; background:#fff; font-size:12px;">重置</button>
            </td>
        </tr>
        """

    if not rows_html:
        rows_html = '<tr><td colspan="6" style="text-align:center; padding: 40px; color: #94a3b8;">该时间范围内暂无发信记录</td></tr>'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>达人邮件打开率大盘</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f8fafc; margin: 0; padding: 40px; color: #334155; }}
            .container {{ max-width: 1100px; margin: 0 auto; }}
            .header {{ font-size: 24px; font-weight: bold; margin-bottom: 20px; color: #0f172a; display: flex; align-items: center; justify-content: space-between; }}
            .tabs {{ display: flex; gap: 8px; margin-bottom: 24px; }}
            .tab-btn {{ text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; transition: 0.1s; }}
            .cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 32px; }}
            .card {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }}
            .card-title {{ font-size: 13px; color: #64748b; margin-bottom: 8px; font-weight: 500; }}
            .card-value {{ font-size: 32px; font-weight: bold; color: #0f172a; }}
            .table-container {{ background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; overflow: hidden; }}
            table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }}
            th {{ background: #f1f5f9; padding: 14px 16px; color: #0f172a; font-weight: 600; font-size: 13px; }}
            .table-title {{ padding: 20px; font-size: 16px; font-weight: bold; color: #0f172a; border-bottom: 1px solid #f1f5f9; }}
        </style>
        <script>
            async function setStatus(id, st) {{
                await fetch('/api/update_status', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ id: id, status: st }})
                }});
                location.reload();
            }}
        </script>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div>📧 达人邮件追踪大盘</div>
            </div>

            <div class="tabs">
                <a href="/dashboard?time_range=all" class="tab-btn" style="{get_tab_style('all')}">全部时间</a>
                <a href="/dashboard?time_range=today" class="tab-btn" style="{get_tab_style('today')}">今天</a>
                <a href="/dashboard?time_range=yesterday" class="tab-btn" style="{get_tab_style('yesterday')}">昨天</a>
                <a href="/dashboard?time_range=week" class="tab-btn" style="{get_tab_style('week')}">最近7天</a>
                <a href="/dashboard?time_range=month" class="tab-btn" style="{get_tab_style('month')}">最近30天</a>
            </div>

            <div class="cards">
                <div class="card">
                    <div class="card-title">总发信量</div>
                    <div class="card-value">{total_sent}</div>
                </div>
                <div class="card">
                    <div class="card-title">有效打开数</div>
                    <div class="card-value" style="color: #16a34a;">{total_opened}</div>
                </div>
                <div class="card">
                    <div class="card-title">综合打开率</div>
                    <div class="card-value" style="color: #2563eb;">{rate}</div>
                </div>
            </div>

            <div class="table-container">
                <div class="table-title">发信明细列表</div>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 22%;">收件人</th>
                            <th style="width: 30%;">主题</th>
                            <th style="width: 16%;">发送时间</th>
                            <th style="width: 12%;">状态</th>
                            <th style="width: 16%;">首次打开时间</th>
                            <th style="width: 14%;">操作</th>
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
