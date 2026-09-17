import base64
from datetime import datetime
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
import sqlite3

app = FastAPI()

# 1x1 像素纯透明 GIF 图片的 base64 编码
PIXEL_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

# 初始化 SQLite 数据库
def init_db():
    conn = sqlite3.connect("tracking.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS emails (
                    id TEXT PRIMARY KEY,
                    recipient TEXT,
                    subject TEXT,
                    created_at TEXT,
                    opened_at TEXT,
                    open_count INTEGER DEFAULT 0
                )''')
    conn.commit()
    conn.close()

init_db()

# 1. 注册新发出的邮件（你点发信时调用）
@app.post("/api/register")
def register_email(data: dict):
    email_id = data.get("id")
    recipient = data.get("recipient", "未知达人")
    subject = data.get("subject", "无主题")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    conn = sqlite3.connect("tracking.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO emails (id, recipient, subject, created_at, open_count) VALUES (?, ?, ?, ?, 0)",
              (email_id, recipient, subject, now))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# 2. 追踪图片接口（达人打开邮件时自动请求）
@app.get("/t/{email_id}.png")
def track_pixel(email_id: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect("tracking.db")
    c = conn.cursor()
    # 增加打开次数并记录首次打开时间
    c.execute("UPDATE emails SET open_count = open_count + 1, opened_at = COALESCE(opened_at, ?) WHERE id = ?", (now, email_id))
    conn.commit()
    conn.close()
    
    # 返回透明图片，禁用缓存确保每次打开都计数
    return Response(
        content=PIXEL_GIF, 
        media_type="image/gif",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )

# 3. 极简大盘看板（你要的：发30、开3）
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    conn = sqlite3.connect("tracking.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM emails")
    total_sent = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE open_count > 0")
    total_opened = c.fetchone()[0]
    
    c.execute("SELECT id, recipient, subject, created_at, opened_at, open_count FROM emails ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    
    rate = f"{(total_opened / total_sent * 100):.1f}%" if total_sent > 0 else "0.0%"
    
    table_rows = "".join([
        f"<tr style='border-bottom: 1px solid #eee; text-align: left;'>"
        f"<td style='padding: 10px;'>{r[1]}</td>"
        f"<td style='padding: 10px;'>{r[2]}</td>"
        f"<td style='padding: 10px;'>{r[3]}</td>"
        f"<td style='padding: 10px;'><span style='color: {'#16a34a' if r[5] > 0 else '#9ca3af'}; font-weight: bold;'>{'已读 (' + str(r[5]) + '次)' if r[5] > 0 else '未读'}</span></td>"
        f"<td style='padding: 10px;'>{r[4] or '-'}</td>"
        f"</tr>"
        for r in rows
    ])

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>达人邮件追踪大盘</title></head>
    <body style="font-family: -apple-system, sans-serif; background: #f8fafc; margin: 0; padding: 30px;">
        <div style="max-width: 900px; margin: 0 auto;">
            <h2 style="margin-bottom: 20px;">📧 达人邮件打开率大盘</h2>
            <div style="display: flex; gap: 20px; margin-bottom: 30px;">
                <div style="flex: 1; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                    <div style="color: #64748b; font-size: 14px;">总发信量</div>
                    <div style="font-size: 32px; font-weight: bold; color: #0f172a; margin-top: 5px;">{total_sent}</div>
                </div>
                <div style="flex: 1; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                    <div style="color: #64748b; font-size: 14px;">被打开数</div>
                    <div style="font-size: 32px; font-weight: bold; color: #16a34a; margin-top: 5px;">{total_opened}</div>
                </div>
                <div style="flex: 1; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                    <div style="color: #64748b; font-size: 14px;">综合打开率</div>
                    <div style="font-size: 32px; font-weight: bold; color: #2563eb; margin-top: 5px;">{rate}</div>
                </div>
            </div>
            <div style="background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                <h3 style="margin-top: 0;">发信追踪明细</h3>
                <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                    <thead>
                        <tr style="background: #f1f5f9; text-align: left;">
                            <th style="padding: 10px;">收件人</th>
                            <th style="padding: 10px;">主题</th>
                            <th style="padding: 10px;">发送时间</th>
                            <th style="padding: 10px;">状态</th>
                            <th style="padding: 10px;">首次打开时间</th>
                        </tr>
                    </thead>
                    <tbody>{table_rows}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """