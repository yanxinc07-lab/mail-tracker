import os
import re
import html as html_lib
import time
import uuid
import imaplib
import smtplib
import urllib.request
import threading
from email import message_from_bytes
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from typing import Dict, List, Optional
from fastapi import FastAPI, Request, HTTPException, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from supabase import create_client, Client
import io

# ==========================================
# 环境变量与配置（已脱敏）
# ==========================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://YOUR_SUPABASE_PROJECT_ID.supabase.co")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "YOUR_SUPABASE_SERVICE_ROLE_KEY")
BASE_URL = os.getenv("BASE_URL", "https://YOUR_APP_NAME.onrender.com")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI(title="MailTracker Batch Outreach Engine")

# 挂载静态目录与模板
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# 1x1 像素透明 GIF 字节流
TRANSPARENT_1PX_GIF = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
    b"\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00"
    b"\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
)

# 发件人设备标记 Cookie 名：访问工作台/大盘时种下，/track 见到它就不记已读
SENDER_COOKIE_NAME = "is_sender_device"
SENDER_COOKIE_MAX_AGE = 365 * 24 * 3600  # 一年
MAX_EMAILS_PER_TASK = 1000  # 单批收件人硬上限（与前端导入上限一致，防误发）

# 商品链接行默认值（用户已确认过的商品短链接与文案，2026-11-02）
DEFAULT_LINK_URL = "https://shop.tiktok.com/us/pdp/1732256087876277185"
DEFAULT_LINK_TEXT = "👉 See it on TikTok Shop — already 2.9K+ sold ✨"
# 正文里可用的链接行占位标记：发送拼装时按开关替换/删除
LINK_MARKERS = ("[[商品链接]]", "{{product_link_block}}")

# ==========================================
# 数据模型定义
# ==========================================
class EmailItem(BaseModel):
    to_email: str
    subject: str
    content: str

class SendBatchRequest(BaseModel):
    user_email: str
    smtp_account: str
    smtp_password: str
    emails: List[EmailItem]
    # 商品链接行拼装参数（发送前对每封正文拼装一次，默认关闭）
    link_enabled: bool = False
    link_url: str = DEFAULT_LINK_URL
    link_text: str = DEFAULT_LINK_TEXT

class SmtpAccount(BaseModel):
    email: str
    password: str

class BatchTaskRequest(BaseModel):
    user_email: str
    accounts: List[SmtpAccount]
    emails: List[EmailItem]
    daily_cap: int = 50
    interval_seconds: float = 0
    # 商品链接行拼装参数（发送前对每封正文拼装一次，默认关闭）
    link_enabled: bool = False
    link_url: str = DEFAULT_LINK_URL
    link_text: str = DEFAULT_LINK_TEXT

# ==========================================
# SMTP 服务分发工具函数
# ==========================================
def get_smtp_host_and_port(smtp_account: str):
    domain = smtp_account.split("@")[-1].lower()
    if "gmail.com" in domain:
        return "smtp.gmail.com", 465, True
    elif "qq.com" in domain:
        return "smtp.qq.com", 465, True
    elif "163.com" in domain:
        return "smtp.163.com", 465, True
    elif "outlook.com" in domain or "hotmail.com" in domain:
        return "smtp.office365.com", 587, False
    else:
        # 默认企业邮箱走通用 SSL 465
        return f"smtp.{domain}", 465, True

# ==========================================
# 商品链接行拼装（变量替换之后、加追踪像素之前，对每封正文只执行一次）
# ------------------------------------------
# 规则（按顺序）：
# 1. 正文里有标记 [[商品链接]] 或 {{product_link_block}}：
#    开关开 → 标记换成链接行 HTML；开关关 → 标记整行删掉（不留空链接）。
# 2. 无标记且开关开：在正文里找合作邀约段（含 collaborate 的 <p> 段，
#    或纯文本里该句所在行），把链接行插到该段之前。
# 3. 锚点也找不到：追加到正文末尾。追踪像素不在这里处理，仍由
#    build_message 在拼装结果之后最后追加，保证像素永远在最末尾。
# 安全：link_url 只允许 http/https，其它一律按关闭处理；link_text 先做
# HTML 转义再拼入，防止模板文案里的 < > & 把正文结构打坏。
# ==========================================
def _link_block_html(link_url: str, link_text: str) -> str:
    safe_url = html_lib.escape((link_url or "").strip(), quote=True)
    safe_text = html_lib.escape(link_text or "")
    return f'<p><a href="{safe_url}">{safe_text}</a></p>'

def _marker_solo_p_pattern(marker: str):
    """标记独占一整个 <p>...</p> 时的匹配（允许标记前后有空白）。"""
    return re.compile(r"<p[^>]*>\s*" + re.escape(marker) + r"\s*</p>", re.IGNORECASE)

def _remove_link_marker(content: str, marker: str) -> str:
    # 标记独占 <p> 段：整段删
    content = _marker_solo_p_pattern(marker).sub("", content)
    if marker in content:
        # 标记独占一整行（去掉标签后整行就只有标记）：整行删
        def _marker_only_line(line: str) -> bool:
            return re.sub(r"<[^>]+>", "", line).strip() == marker
        content = "\n".join(
            ln for ln in content.split("\n")
            if marker not in ln or not _marker_only_line(ln)
        )
    # 还残留的（嵌在句子中间等）：只抹掉标记本身，不动其它内容
    return content.replace(marker, "")

def assemble_content(html: str, *, link_enabled: bool, link_url: str, link_text: str) -> str:
    content = html or ""
    url_ok = bool(link_url) and link_url.strip().lower().startswith(("http://", "https://"))
    enabled = bool(link_enabled) and url_ok
    block = _link_block_html(link_url, link_text) if enabled else ""

    has_marker = any(m in content for m in LINK_MARKERS)
    if has_marker:
        for marker in LINK_MARKERS:
            if marker not in content:
                continue
            if block:
                # 独占 <p> 的标记整段换成链接行，避免 <p> 套 <p>；其余位置直接替换
                content = _marker_solo_p_pattern(marker).sub(lambda _m: block, content)
                content = content.replace(marker, block)
            else:
                content = _remove_link_marker(content, marker)
        return content

    if not block:
        return content

    # 无标记：找合作邀约段锚点（含 collaborate 的 <p> 段优先）
    for m in re.finditer(r"<p[^>]*>.*?</p>", content, flags=re.IGNORECASE | re.DOTALL):
        if "collaborate" in m.group(0).lower():
            return content[:m.start()] + block + content[m.start():]
    # 纯文本兜底：collaborate 所在行的行首之前
    idx = content.lower().find("collaborate")
    if idx != -1:
        line_start = content.rfind("\n", 0, idx) + 1
        return content[:line_start] + block + "\n" + content[line_start:]
    # 锚点也找不到：追加到正文末尾（像素随后由 build_message 再追加，仍在最末尾）
    return content + block

# ==========================================
# 发送记录（Supabase emails 表，带 status 并做降级）
# ==========================================
def _insert_record(user_email: str, sender_account: str, item: EmailItem, with_status: bool):
    payload = {
        "user_email": user_email,
        "sender_account": sender_account,
        "recipient": item.to_email,
        "subject": item.subject,
        "is_read": False,
    }
    if with_status:
        payload["status"] = "pending"
    return supabase.table("emails").insert(payload).execute()

def create_send_record(user_email: str, sender_account: str, item: EmailItem) -> Optional[str]:
    """插入一条 pending 记录并返回 id；若 emails 表还没有 status 列，
    自动去掉 status 字段重试一次，避免整个发送任务被表结构卡死。"""
    try:
        res = _insert_record(user_email, sender_account, item, with_status=True)
    except Exception as e:
        print(f"Insert with status failed, retrying without status: {e}")
        res = _insert_record(user_email, sender_account, item, with_status=False)
    if res.data:
        return res.data[0]["id"]
    return None

def update_record_status(email_id: Optional[str], status: str):
    if not email_id:
        return
    try:
        supabase.table("emails").update({"status": status}).eq("id", email_id).execute()
    except Exception as e:
        # status 列不存在等情况下只记日志，不影响发送本身
        print(f"Update record status failed for {email_id}: {e}")

# ==========================================
# SMTP 连接（同账号复用一条连接，断了才重建）
# ==========================================
class SmtpConnection:
    def __init__(self, account: SmtpAccount):
        self.account = account
        self.server = None

    def ensure_connected(self):
        if self.server is not None:
            return self.server
        host, port, use_ssl = get_smtp_host_and_port(self.account.email)
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        server.login(self.account.email, self.account.password)
        self.server = server
        return server

    def send(self, from_addr: str, to_addr: str, msg_str: str):
        server = self.ensure_connected()
        server.sendmail(from_addr, [to_addr], msg_str)

    def close(self):
        if self.server is not None:
            try:
                self.server.quit()
            except Exception:
                pass
            self.server = None

_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=)(["\'])([^"\']+)\2', re.IGNORECASE)
_LOCAL_GIF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "kwoov-demo.gif")


def _fetch_image_bytes(src: str):
    """取图片字节：站内产品 GIF 用服务器本地原图；其他 http(s) 图片尝试下载（限时限量）；失败返回 (None, None)。"""
    if "kwoov-demo.gif" in src and os.path.exists(_LOCAL_GIF_PATH):
        try:
            with open(_LOCAL_GIF_PATH, "rb") as f:
                return f.read(), "gif"
        except Exception:
            pass
    if src.startswith("http://") or src.startswith("https://"):
        try:
            req = urllib.request.Request(src, headers={"User-Agent": "MailTracker/1.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                ctype = r.headers.get_content_type() or ""
                if not ctype.startswith("image/"):
                    return None, None
                data = r.read(5 * 1024 * 1024 + 1)
                if len(data) > 5 * 1024 * 1024:
                    return None, None
                return data, ctype.split("/")[-1].lower()
        except Exception:
            return None, None
    return None, None


def inline_images(html_content: str):
    """把正文里的图片改成本封邮件的内嵌附件（cid:），返回 (新正文, [(cid, bytes, subtype)])。
    追踪像素不在此处理（它必须保持外链才能计数）——调用顺序上像素在内嵌之后才拼接。"""
    images = []
    cid_map: Dict[str, str] = {}

    def repl(m):
        prefix, quote, src = m.group(1), m.group(2), m.group(3)
        if src.startswith("cid:") or src.startswith("data:"):
            return m.group(0)
        if src in cid_map:
            return f"{prefix}{quote}cid:{cid_map[src]}{quote}"
        data, subtype = _fetch_image_bytes(src)
        if not data:
            return m.group(0)
        cid = f"img{len(images)}-{uuid.uuid4().hex[:10]}@mailtracker"
        cid_map[src] = cid
        images.append((cid, data, subtype or "png"))
        return f"{prefix}{quote}cid:{cid}{quote}"

    return _IMG_SRC_RE.sub(repl, html_content), images


def build_message(from_addr: str, item: EmailItem, email_id: Optional[str], content: Optional[str] = None) -> str:
    if content is None:
        content = item.content
    # 先内嵌正文图片，再拼追踪像素（像素必须保持远程地址）
    content, images = inline_images(content)
    if email_id:
        tracking_url = f"{BASE_URL}/track/{email_id}"
        pixel_html = f'<br><img src="{tracking_url}" width="1" height="1" alt="" style="display:none !important;" />'
        content = content + pixel_html
    if images:
        msg = MIMEMultipart("related")
    else:
        msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = item.to_email
    msg["Subject"] = item.subject
    msg.attach(MIMEText(content, "html", "utf-8"))
    for cid, data, subtype in images:
        if subtype in ("gif", "jpeg", "jpg", "png", "webp"):
            part = MIMEImage(data, _subtype="jpeg" if subtype == "jpg" else subtype)
        else:
            part = MIMEImage(data)
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header("Content-Disposition", "inline")
        msg.attach(part)
    return msg.as_string()


def send_one(user_email: str, conn: SmtpConnection, item: EmailItem, *,
             link_enabled: bool = False, link_url: str = DEFAULT_LINK_URL,
             link_text: str = DEFAULT_LINK_TEXT):
    """发送一封：建记录 -> 拼装链接行 -> 组装（含像素）-> 发送（失败自动重试 1 次，重试前重建连接）。
    返回 (ok: bool, error: Optional[str])
    注意：变量替换在调用方（前端提交前）完成，这里只做链接行拼装，且只拼装一次；
    重试时复用同一份组装好的邮件串，不会重复拼装。"""
    try:
        email_id = create_send_record(user_email, conn.account.email, item)
    except Exception as e:
        return False, f"record_insert_failed: {e}"
    assembled = assemble_content(item.content, link_enabled=link_enabled,
                                 link_url=link_url, link_text=link_text)
    msg_str = build_message(conn.account.email, item, email_id, content=assembled)
    last_err = None
    for attempt in range(2):  # 首次 + 重试 1 次
        try:
            conn.send(conn.account.email, item.to_email, msg_str)
            update_record_status(email_id, "sent")
            return True, None
        except Exception as err:
            last_err = err
            conn.close()  # 下一次尝试会重建连接
    update_record_status(email_id, "failed")
    return False, str(last_err)

# ==========================================
# 每账号每日发送计数（进程内，按 UTC 日期）
# ==========================================
_daily_counts: Dict[str, int] = {}
_daily_lock = threading.Lock()

def _daily_key(account_email: str) -> str:
    return f"{datetime.now(timezone.utc).date().isoformat()}::{account_email}"

def daily_sent_count(account_email: str) -> int:
    with _daily_lock:
        return _daily_counts.get(_daily_key(account_email), 0)

def daily_increment(account_email: str):
    with _daily_lock:
        key = _daily_key(account_email)
        _daily_counts[key] = _daily_counts.get(key, 0) + 1

# ==========================================
# 批量任务（后台运行，状态存内存字典）
# ==========================================
TASKS: Dict[str, dict] = {}

def _public_task(task: dict) -> dict:
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "total": task["total"],
        "sent": task["sent"],
        "failed": task["failed"],
        "skipped": task["skipped"],
        "errors": task["errors"][-20:],
        "notes": task["notes"],
        "per_account": task["per_account"],
        "created_at": task["created_at"],
        "finished_at": task["finished_at"],
    }

def run_batch_task(task_id: str, req: BatchTaskRequest):
    task = TASKS[task_id]
    task["status"] = "running"
    conns: Dict[str, SmtpConnection] = {}
    pointer = 0
    try:
        for idx, item in enumerate(req.emails):
            if task["status"] == "stopped":
                task["skipped"] += len(req.emails) - idx
                break
            while task["status"] == "paused":
                time.sleep(0.2)
            if task["status"] == "stopped":
                task["skipped"] += len(req.emails) - idx
                break

            # 轮换选账号：从 pointer 开始找第一个未达每日上限的账号
            chosen = None
            for offset in range(len(req.accounts)):
                cand_idx = (pointer + offset) % len(req.accounts)
                cand = req.accounts[cand_idx]
                if daily_sent_count(cand.email) < req.daily_cap:
                    chosen = cand
                    pointer = cand_idx + 1
                    break
            if chosen is None:
                note = f"所有发信账号均已达到每日上限 {req.daily_cap} 封，剩余 {len(req.emails) - idx} 封跳过"
                if note not in task["notes"]:
                    task["notes"].append(note)
                task["skipped"] += len(req.emails) - idx
                break

            if chosen.email not in conns:
                conns[chosen.email] = SmtpConnection(chosen)
            ok, err = send_one(req.user_email, conns[chosen.email], item,
                               link_enabled=req.link_enabled, link_url=req.link_url,
                               link_text=req.link_text)
            acct_stat = task["per_account"].setdefault(
                chosen.email, {"sent": 0, "failed": 0})
            if ok:
                task["sent"] += 1
                acct_stat["sent"] += 1
                daily_increment(chosen.email)
            else:
                task["failed"] += 1
                acct_stat["failed"] += 1
                task["errors"].append({"to": item.to_email, "account": chosen.email, "error": err})

            if req.interval_seconds and req.interval_seconds > 0:
                slept = 0.0
                while slept < req.interval_seconds and task["status"] not in ("stopped",):
                    time.sleep(min(0.2, req.interval_seconds - slept))
                    slept += 0.2
                    while task["status"] == "paused":
                        time.sleep(0.2)
    except Exception as e:
        task["status"] = "failed"
        task["errors"].append({"to": None, "account": None, "error": f"task_crashed: {e}"})
    finally:
        for conn in conns.values():
            conn.close()
        if task["status"] == "running":
            task["status"] = "done"
        task["finished_at"] = datetime.now(timezone.utc).isoformat()

@app.post("/api/tasks")
async def create_batch_task(req: BatchTaskRequest, background_tasks: BackgroundTasks):
    if not req.accounts:
        raise HTTPException(status_code=400, detail="至少需要一个发信账号")
    if not req.emails:
        raise HTTPException(status_code=400, detail="名单为空")
    if len(req.emails) > MAX_EMAILS_PER_TASK:
        raise HTTPException(status_code=400, detail=f"单批最多 {MAX_EMAILS_PER_TASK} 位收件人，请删减名单后重试")
    task_id = uuid.uuid4().hex[:12]
    TASKS[task_id] = {
        "task_id": task_id,
        "status": "running",
        "total": len(req.emails),
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
        "notes": [],
        "per_account": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    background_tasks.add_task(run_batch_task, task_id, req)
    return {"success": True, "task_id": task_id, "status": "running", "total": len(req.emails)}

@app.get("/api/tasks/{task_id}")
async def get_batch_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _public_task(task)

@app.post("/api/tasks/{task_id}/pause")
async def pause_batch_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] == "running":
        task["status"] = "paused"
    return _public_task(task)

@app.post("/api/tasks/{task_id}/resume")
async def resume_batch_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] == "paused":
        task["status"] = "running"
    return _public_task(task)

@app.post("/api/tasks/{task_id}/stop")
async def stop_batch_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] in ("running", "paused"):
        task["status"] = "stopped"
    return _public_task(task)

# ==========================================
# 页面路由
# ==========================================
def _serve_page_file(template_name: str) -> HTMLResponse:
    """直接按文件内容返回页面，不走 Jinja 渲染。
    原因：三个模板里没有任何服务端 Jinja 变量，反而正文里有前端自己的
    {{name}} 占位符和 JS 模板字符串（{{${key}}}），Jinja 编译会直接报错
    或把 {{name}} 吞掉。原版用 TemplateResponse 在当前依赖版本下打开
    /outreach 就会 TemplateSyntaxError，这也是顺带修掉的一个存量问题。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", template_name)
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

def _page_with_sender_cookie(template_name: str, request: Request) -> HTMLResponse:
    resp = _serve_page_file(template_name)
    resp.set_cookie(
        SENDER_COOKIE_NAME, "1",
        max_age=SENDER_COOKIE_MAX_AGE, path="/", samesite="lax",
    )
    return resp

@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    return _serve_page_file("index.html")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return _page_with_sender_cookie("dashboard.html", request)

@app.get("/outreach", response_class=HTMLResponse)
async def outreach_page(request: Request):
    return _page_with_sender_cookie("outreach.html", request)

# ==========================================
# 追踪像素端点
# ==========================================
@app.get("/track/{email_id}")
async def track_email_open(email_id: str, request: Request):
    # 发件人自己的设备（访问过工作台/大盘、种过标记 Cookie）打开邮件不计已读，
    # 避免发件人查看往来邮件时把真实未读状态冲成已读。
    if request.cookies.get(SENDER_COOKIE_NAME) == "1":
        return _pixel_response()
    try:
        supabase.table("emails").update({
            "is_read": True,
            "read_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", email_id).execute()
    except Exception as e:
        print(f"Tracking error for {email_id}: {e}")

    return _pixel_response()

def _pixel_response() -> Response:
    return Response(
        content=TRANSPARENT_1PX_GIF,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

# ==========================================
# 批量发信 API（旧版单账号同步接口，保留兼容，内部已改为连接复用+记录状态）
# ==========================================
@app.post("/api/send_batch")
async def send_batch_emails(req: SendBatchRequest):
    if len(req.emails) > MAX_EMAILS_PER_TASK:
        raise HTTPException(status_code=400, detail=f"单批最多 {MAX_EMAILS_PER_TASK} 位收件人，请删减名单后重试")
    account = SmtpAccount(email=req.smtp_account, password=req.smtp_password)
    conn = SmtpConnection(account)
    sent_count = 0
    failed_count = 0
    try:
        conn.ensure_connected()
    except Exception as e:
        return {"success": False, "message": f"SMTP 认证登录失败: {str(e)}", "sent_count": 0, "failed_count": len(req.emails)}
    for item in req.emails:
        ok, _err = send_one(req.user_email, conn, item,
                            link_enabled=req.link_enabled, link_url=req.link_url,
                            link_text=req.link_text)
        if ok:
            sent_count += 1
            daily_increment(req.smtp_account)
        else:
            failed_count += 1
    conn.close()
    return {
        "success": True,
        "sent_count": sent_count,
        "failed_count": failed_count,
        "message": f"发送完成：成功 {sent_count} 封，失败 {failed_count} 封"
    }

# ==========================================
# 邮箱自带模板直读（骨架接口）
# ------------------------------------------
# 调研结论（详见改造报告）：
# - Gmail 没有独立的“模板”数据接口。网页端的操作就叫 “Save draft as template”
#   （把草稿存为模板，Google 官方帮助原文），Google Apps Script 官方示例也是
#   从 GmailApp.getDrafts() 里按主题名找模板——即模板在数据层就是草稿箱里的
#   特殊草稿。因此可行的读取路径只有两条：Gmail API users.drafts.list/get
#   （需 OAuth），或 IMAP 读 Drafts 文件夹（应用密码）。本骨架实现 IMAP 路径。
#   注意：IMAP 无法区分“模板草稿”和普通草稿，返回的是 Drafts 全部条目，
#   调用方需按主题自行认领哪条是模板（与 Apps Script 示例的做法一致）。
# - Outlook 的 “My Templates” 是加载项（add-in）侧的功能，模板存在创建者
#   主邮箱里（微软官方文档），没有公开的 Microsoft Graph 模板资源可列取；
#   且 Outlook.com 个人账号自 2024-09-16 起停用基本认证（密码/授权码），
#   IMAP/SMTP 必须 OAuth2。因此 Outlook 路径本轮不做密码读取，直接返回
#   结构化的 oauth_required 错误，等接了 OAuth 再实现。
# ==========================================
def _decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)

def _extract_html(msg) -> str:
    html = ""
    plain = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if ctype == "text/html" and not html:
                html = text
            elif ctype == "text/plain" and not plain:
                plain = text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html = text
            else:
                plain = text
    return html or plain

def _gmail_templates_via_imap(account: str, password: str, limit: int = 30):
    """用 IMAP（应用密码）读 Gmail Drafts 文件夹，列出主题 + HTML 正文。
    只读打开（readonly），不修改邮箱里的任何东西。"""
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    try:
        mail.login(account, password)
        # 找带 \Drafts 标记的文件夹，找不到就退回常见名字
        drafts_folder = None
        _typ, listing = mail.list()
        if listing:
            for line in listing:
                if not line:
                    continue
                text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
                if "\\Drafts" in text:
                    # LIST 行格式: (\HasNoChildren \Drafts) "/" "[Gmail]/Drafts"
                    drafts_folder = text.rsplit(' ', 1)[-1].strip().strip('"')
                    break
        for candidate in (drafts_folder, "[Gmail]/Drafts", "Drafts", "INBOX.Drafts"):
            if not candidate:
                continue
            typ, _ = mail.select(f'"{candidate}"', readonly=True)
            if typ == "OK":
                drafts_folder = candidate
                break
        else:
            return {"success": False, "provider": "gmail",
                    "error_type": "drafts_folder_not_found",
                    "message": "未找到 Drafts 文件夹"}
        typ, data = mail.search(None, "ALL")
        if typ != "OK":
            return {"success": False, "provider": "gmail",
                    "error_type": "search_failed", "message": "搜索 Drafts 失败"}
        ids = data[0].split() if data and data[0] else []
        ids = list(reversed(ids))[:limit]
        templates = []
        for mid in ids:
            typ, fetched = mail.fetch(mid, "(RFC822)")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            msg = message_from_bytes(fetched[0][1])
            templates.append({
                "id": mid.decode("ascii", errors="replace"),
                "subject": _decode_mime_header(msg.get("Subject")),
                "html": _extract_html(msg),
            })
        return {"success": True, "provider": "gmail", "source": "imap_drafts",
                "folder": drafts_folder, "templates": templates,
                "note": "IMAP 无法区分模板草稿与普通草稿，返回 Drafts 全部条目，请按主题认领模板"}
    except imaplib.IMAP4.error as e:
        return {"success": False, "provider": "gmail",
                "error_type": "imap_auth_failed",
                "message": f"IMAP 登录失败（需开启 IMAP 并使用应用专用密码）: {e}"}
    finally:
        try:
            mail.logout()
        except Exception:
            pass

@app.get("/api/mailbox/templates")
async def mailbox_templates(provider: str, account: str, password: str):
    """只读骨架：读邮箱里存着的模板（Gmail 走 IMAP 读 Drafts；Outlook 需 OAuth，本轮明确报错）。
    凭据只用于本次 IMAP 登录，不落库、不写日志。"""
    provider = provider.strip().lower()
    if provider == "gmail":
        return _gmail_templates_via_imap(account, password)
    if provider in ("outlook", "hotmail", "microsoft"):
        return {
            "success": False,
            "provider": "outlook",
            "error_type": "oauth_required",
            "message": ("Outlook.com 个人账号自 2024-09-16 起已停用基本认证，"
                        "密码/授权码无法登录 IMAP/SMTP；My Templates 也没有公开的 Graph 读取接口。"
                        "需要先接 Microsoft OAuth2 授权，本接口暂不支持用密码读取 Outlook 模板。"),
        }
    return {"success": False, "provider": provider,
            "error_type": "unsupported_provider",
            "message": f"暂不支持的服务商: {provider}"}
