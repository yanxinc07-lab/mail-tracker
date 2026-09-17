import os
import base64
from datetime import datetime
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    return conn

# 初始化数据表
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
    print(f"Database init warning: {e}")

# 1x1 透明 GIF 像素
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
        ON CONFLICT (id) DO NOTHING;
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

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM emails ORDER BY send_time DESC;")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total_sent = len(rows)
    total_opened = sum(1 for r in rows if r["status"] == "已读")
    rate = f"{(total_opened / total_sent * 100):.1f}%" if total_sent > 0 else "0.0%"

    rows_html = ""
    for r in rows:
        status_color = "color: #16a34a; font-weight: bold;" if r["status"] == "已读" else "color: #94a3b8;"
        badge = f"{r['status']} ({r['open_count']}次)" if r['status'] == "已读" else r['status']
        rows_html += f"""
        <tr style="border-bottom: 1px solid #f1f5f9; height: 48px;">
            <td style="padding: 12px 16px; color: #475569;">{r['recipient'] or '-'}</td>
            <td style="padding: 12px 16px; color: #0f172a; font-weight: 500;">{r['subject'] or '-'}</td>
            <td style="padding: 12px 16px; color: #64748b;">{r['send_time']}</td>
            <td style="padding: 12px 16px; {status_color}">{badge}</td>
            <td style="padding: 12px 16px; color: #64748b;">{r['first_open_time']}</td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>达人邮件打开率大盘</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f8fafc; margin: 0; padding: 40px; color: #334155; }}
            .container {{ max-width: 1000px; margin: 0 auto; }}
            .header {{ font-size: 24px; font-weight: bold; margin-bottom: 24px; color: #0f172a; display: flex; align-items: center; gap: 8px; }}
            .cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 32px; }}
            .card {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }}
            .card-title {{ font-size: 13px; color: #64748b; margin-bottom: 8px; font-weight: 500; }}
            .card-value {{ font-size: 32px; font-weight: bold; color: #0f172a; }}
            .table-container {{ background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; overflow: hidden; }}
            table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }}
            th {{ background: #f1f5f9; padding: 14px 16px; color: #0f172a; font-weight: 600; font-size: 13px; }}
            .table-title {{ padding: 20px; font-size: 16px; font-weight: bold; color: #0f172a; border-bottom: 1px solid #f1f5f9; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">📧 达人邮件打开率大盘</div>
            <div class="cards">
                <div class="card">
                    <div class="card-title">总发信量</div>
                    <div class="card-value">{total_sent}</div>
                </div>
                <div class="card">
                    <div class="card-title">被打开数</div>
                    <div class="card-value" style="color: #16a34a;">{total_opened}</div>
                </div>
                <div class="card">
                    <div class="card-title">综合打开率</div>
                    <div class="card-value" style="color: #2563eb;">{rate}</div>
                </div>
            </div>
            <div class="table-container">
                <div class="table-title">发信追踪明细</div>
                <table>
                    <thead>
                        <tr>
                            <th>收件人</th>
                            <th>主题</th>
                            <th>发送时间</th>
                            <th>状态</th>
                            <th>首次打开时间</th>
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
