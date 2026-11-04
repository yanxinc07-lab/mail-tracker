import os
import re
import random
import html as html_lib
import time
import uuid
import imaplib
import smtplib
import urllib.request
import json
import hashlib
import base64
import threading
from email import message_from_bytes
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders as mime_encoders
from datetime import datetime, timezone
from typing import Dict, List, Optional
from fastapi import FastAPI, Request, HTTPException, Response, BackgroundTasks, Header
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import psycopg2
import psycopg2.extras
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
import io

# ==========================================
# 环境变量与配置（已脱敏）
# ==========================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
if not SUPABASE_URL or "YOUR_SUPABASE" in SUPABASE_URL:
    # Render 环境变量里若还是 Gemini 留下的占位地址，用项目真实地址兜底
    # （该地址是公开信息，前端页面里同样嵌着它）
    SUPABASE_URL = "https://hteqhquorjycjdxburun.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "YOUR_SUPABASE_SERVICE_ROLE_KEY")
BASE_URL = os.getenv("BASE_URL", "https://YOUR_APP_NAME.onrender.com")

# ==========================================
# 数据库数据层（直连 Postgres，与 9 月可跑版本同一条路）
# ----------------------------------------------------------
# 本服务的 Render 环境里只配置了 DATABASE_URL（Supabase 连接池地址）；
# SUPABASE_URL / SERVICE_ROLE_KEY 从未真正配置过（一直是占位符），
# 因此后端一律走 DATABASE_URL 直连，不用 Supabase REST 客户端。
# SUPABASE_URL 仅用于登录令牌验签时拼 JWKS 地址（公开信息，带兜底）。
# ==========================================
DATABASE_URL = os.getenv("DATABASE_URL", "")


def _db_exec(sql: str, params=()):
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _db_query(sql: str, params=()):
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

# 发件人设备标记 Cookie 名：访问工作台/大盘时种下，/track 见到它就不记已读
SENDER_COOKIE_NAME = "is_sender_device"
SENDER_COOKIE_MAX_AGE = 365 * 24 * 3600  # 一年
MAX_EMAILS_PER_TASK = 1000  # 单批收件人硬上限（与前端导入上限一致，防误发）

# 商品链接行默认值：一律为空。早期自用时曾写死站长的商品链接，多用户后任何账号
# 打开都会被预填站长的广告（2026-11-03 用户发现），故清空；链接由用户在界面里填。
DEFAULT_LINK_URL = ""
DEFAULT_LINK_TEXT = ""
# 正文里可用的链接行占位标记：发送拼装时按开关替换/删除
LINK_MARKERS = ("[[商品链接]]", "{{product_link_block}}")

# ==========================================
# 数据模型定义
# ==========================================
class EmailItem(BaseModel):
    to_email: str
    subject: str
    content: str

class AttachmentItem(BaseModel):
    name: str
    content_type: str = "application/octet-stream"
    data: str = ""  # base64（不带 data: 前缀）

class SendBatchRequest(BaseModel):
    user_email: str = ""
    user_id: str = ""
    smtp_account: str
    smtp_password: str
    emails: List[EmailItem]
    # 商品链接行拼装参数（发送前对每封正文拼装一次，默认关闭）
    attachments: List[AttachmentItem] = []
    link_enabled: bool = False
    link_url: str = DEFAULT_LINK_URL
    link_text: str = DEFAULT_LINK_TEXT

class SmtpAccount(BaseModel):
    email: str
    password: str

class BatchTaskRequest(BaseModel):
    user_email: str = ""
    user_id: str = ""
    subject_template: str = ""  # 未替换变量的原始主题：进度表拿它当批次标题，不再拿第一封渲染后的主题冒充整批
    batch_name: str = ""  # 用户给这一批起的名字（可空）：填了就用它当进度表标题，比主题模板更好认
    accounts: List[SmtpAccount] = []
    emails: List[EmailItem]
    daily_cap: int = 50
    interval_seconds: float = 0
    interval_max_seconds: float = 0
    # 商品链接行拼装参数（发送前对每封正文拼装一次，默认关闭）
    attachments: List[AttachmentItem] = []
    link_enabled: bool = False
    link_url: str = DEFAULT_LINK_URL
    link_text: str = DEFAULT_LINK_TEXT

# ==========================================
# 登录校验与发信账号（云端加密存储）
# ==========================================
_JWKS_CACHE = {"keys": None, "fetched_at": 0.0}


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _verify_user_id(authorization: Optional[str]) -> str:
    """校验前端带来的 Supabase 登录令牌（ES256），返回用户 uuid；无效抛 401。
    用项目公开的 JWKS 公钥在本地验签，不依赖服务器端密钥配置；
    user_id 只取令牌里的 sub，不信任请求体自报的身份。"""
    if not isinstance(authorization, str) or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.split(" ", 1)[1].strip()
    try:
        head_seg, payload_seg, sig_seg = token.split(".")
        header = json.loads(_b64url_decode(head_seg))
        payload = json.loads(_b64url_decode(payload_seg))
        if payload.get("exp", 0) < time.time():
            raise ValueError("token expired")
        now = time.time()
        if not _JWKS_CACHE["keys"] or now - _JWKS_CACHE["fetched_at"] > 3600:
            last_err = None
            for _attempt in range(3):  # 公钥拉取偶发连接重置时重试，成功后缓存 1 小时
                try:
                    # JWKS 是公开端点，不带任何密钥头，避免服务器密钥配置干扰验签
                    req = urllib.request.Request(
                        f"{SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json")
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        _JWKS_CACHE["keys"] = json.loads(resp.read().decode("utf-8"))["keys"]
                    _JWKS_CACHE["fetched_at"] = now
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6)
            if last_err is not None:
                raise last_err
        jwk = next((k for k in _JWKS_CACHE["keys"] if k.get("kid") == header.get("kid")), None)
        if jwk is None:
            raise ValueError("kid not found in JWKS")
        x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
        y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
        pub = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        sig = _b64url_decode(sig_seg)
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        pub.verify(der, f"{head_seg}.{payload_seg}".encode(), ec.ECDSA(hashes.SHA256()))
        sub = payload.get("sub")
        if not sub:
            raise ValueError("no sub")
        return sub
    except HTTPException:
        raise
    except Exception as e:
        print(f"auth verify failed: {e}")
    raise HTTPException(status_code=401, detail="登录已过期，请重新登录")


def _fernet() -> Fernet:
    # 加密密钥由服务器环境里的 DATABASE_URL 派生：数据库单独泄露时，
    # 没有服务器环境里的连接串就解不出授权码。轮换连接串需同步迁移账号。
    seed = DATABASE_URL or SUPABASE_SERVICE_ROLE_KEY
    key = base64.urlsafe_b64encode(
        hashlib.sha256(seed.encode()).digest())
    return Fernet(key)


def _encrypt_pass(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def _decrypt_pass(enc: str) -> str:
    return _fernet().decrypt(enc.encode()).decode()


def _load_user_accounts(user_id: str) -> List[SmtpAccount]:
    """从云端读该用户的发信账号并解密（仅后端内部使用）。"""
    try:
        rows = _db_query(
            "SELECT email, pass_enc FROM sender_accounts WHERE user_id = %s ORDER BY created_at",
            (user_id,))
    except Exception as e:
        print(f"load accounts failed: {e}")
        return []
    accounts = []
    for r in rows:
        try:
            accounts.append(SmtpAccount(**{"email": r["email"], "password": _decrypt_pass(r["pass_enc"])}))
        except Exception as e:
            print(f"decrypt account failed for {r.get('email')}: {e}")
    return accounts


def _sync_task_db(task: dict):
    """把内存任务的进度同步到 send_tasks 表（尽力而为，失败只记日志）。"""
    try:
        _db_exec(
            "UPDATE send_tasks SET status = %s, sent = %s, failed = %s, skipped = %s, "
            "finished_at = %s, notes = %s WHERE id = %s",
            (task["status"], task["sent"], task["failed"], task["skipped"],
             task["finished_at"], "\n".join(task["notes"]), task["id"]))
    except Exception as e:
        print(f"sync task db failed: {e}")

app = FastAPI(title="MailTracker Batch Outreach Engine")


@app.on_event("startup")
def _startup_housekeeping():
    """启动自检：补齐新列；把数据库里残留的“进行中/已暂停”标成“已中断”——
    进程刚启动时内存里没有任何活任务，库里还挂着的一定是上次被重启/崩溃打断的，
    标出来后用户才能在进度页对它们点“继续发送”或“终止”。"""
    try:
        _db_exec("ALTER TABLE emails ADD COLUMN IF NOT EXISTS task_id text")
        _db_exec("CREATE TABLE IF NOT EXISTS user_settings (user_id text NOT NULL, "
                 "key text NOT NULL, value text, PRIMARY KEY (user_id, key))")
        _db_exec("ALTER TABLE send_tasks ADD COLUMN IF NOT EXISTS payload text")
        _db_exec("UPDATE send_tasks SET status = 'interrupted' WHERE status IN ('running', 'paused')")
    except Exception as e:
        print(f"startup housekeeping failed: {e}")

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



# ==========================================
# SMTP 服务分发工具函数
# ==========================================
def get_smtp_host_and_port(smtp_account: str):
    domain = smtp_account.split("@")[-1].lower()
    if "gmail.com" in domain:
        # 用 587+STARTTLS：与 9 月可跑版本一致；465 在本部署环境实测不可达
        return "smtp.gmail.com", 587, False
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
def _insert_record(user_id: str, item: EmailItem, task_id: str = None) -> str:
    """按线上 emails 表结构写记录：id 由后端生成（text 主键、无默认值），
    user_id 为登录用户 uuid，send_time 存 ISO 文本，open_count 初始 0。"""
    rid = str(uuid.uuid4())
    _db_exec(
        "INSERT INTO emails (id, recipient, subject, send_time, status, open_count, user_id, task_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (rid, item.to_email, item.subject,
         datetime.now(timezone.utc).isoformat(), "pending", 0, user_id or None, task_id))
    return rid


def create_send_record(user_id: str, item: EmailItem, task_id: str = None) -> Optional[str]:
    """插入一条 pending 记录并返回 id；失败返回 None（调用方据此判定发送失败）。"""
    try:
        return _insert_record(user_id, item, task_id)
    except Exception as e:
        print(f"Insert send record failed: {e}")
        return None


def update_record_status(email_id: Optional[str], status: str):
    if not email_id:
        return
    try:
        _db_exec("UPDATE emails SET status = %s WHERE id = %s", (status, email_id))
    except Exception as e:
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


# Gmail 内部图片引用：正文若是从一封 Gmail 邮件里复制出来的，图片会带这种地址——
# 它绑定复制者当时的登录会话，收件人打不开、服务器也取不到，必须在发送前清掉/换掉。
_GMAIL_HOSTED_IMG_RE = re.compile(
    r'https?://mail\.google\.com/mail/u/\d+\?[^\s"\']*?(?:attid|realattid)[^\s"\']*',
    re.IGNORECASE,
)


def _product_gif_img_tag() -> str:
    q = chr(34)
    return ("<img src=" + q + "/static/kwoov-demo.gif" + q +
            " alt=" + q + "Kwoov Product Demo" + q +
            " style=" + q + "max-width:100%; border-radius:8px;" + q + ">")


def sanitize_outbound_html(html_content: str) -> str:
    """发送前清洗正文里来自 Gmail 的图片引用：
    - alt 表明是产品 GIF 的（Kwoov Product Demo 等）→ 换成本站产品 GIF（随后会被转成内嵌附件）；
    - 其余 Gmail 图（多为表情小图）→ 直接去掉 img 标签，避免收件人看到坏图。
    必须在 inline_images 之前调用。"""
    if not html_content or "mail.google.com" not in html_content:
        return html_content or ""

    def repl(m):
        tag = m.group(0)
        sm = _IMG_SRC_RE.search(tag)
        src = sm.group(3) if sm else ""
        if not _GMAIL_HOSTED_IMG_RE.search(src):
            return tag
        alt_m = re.search(r"alt=([\"'])(.*?)\1", tag, re.IGNORECASE | re.DOTALL)
        alt = alt_m.group(2) if alt_m else ""
        if "kwoov" in alt.lower() and ("demo" in alt.lower() or "gif" in alt.lower()):
            # 只换图片地址、保留原图自带的尺寸设置（用户在 Gmail 里选的大小）；
            # 原图没带任何尺寸时，默认按 200 宽显示，避免按原图满格撑开。
            new_tag = _IMG_SRC_RE.sub(
                lambda mm: mm.group(1) + mm.group(2) + "/static/kwoov-demo.gif" + mm.group(2),
                tag, count=1)
            if not re.search(r"\bwidth\s*=", new_tag, re.IGNORECASE) and \
               not re.search(r"(max-)?width\s*:", new_tag, re.IGNORECASE):
                new_tag = re.sub(r"<img\b", '<img width="200"', new_tag, count=1, flags=re.IGNORECASE)
            return new_tag
        return ""

    return re.sub(r"<img\b[^>]*>", repl, html_content, flags=re.IGNORECASE)


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


def _decode_data_uri(src: str):
    """把 data:image/...;base64,... 解成 (bytes, subtype)；解不了返回 (None, None)。
    粘贴进编辑器的图片是这种形式，Gmail 不显示 data 图，必须转成 cid 内嵌。"""
    m = re.match(r"data:image/([a-zA-Z0-9.+-]+);base64,(.+)", src, re.S)
    if not m:
        return None, None
    try:
        data = base64.b64decode(m.group(2), validate=False)
    except Exception:
        return None, None
    if not data or len(data) > 15 * 1024 * 1024:
        return None, None
    return data, m.group(1).lower()


def inline_images(html_content: str):
    """把正文里的图片改成本封邮件的内嵌附件（cid:），返回 (新正文, [(cid, bytes, subtype)])。
    追踪像素不在此处理（它必须保持外链才能计数）——调用顺序上像素在内嵌之后才拼接。"""
    images = []
    cid_map: Dict[str, str] = {}

    def _name_for(src, subtype, tag=""):
        if "kwoov-demo.gif" in src:
            return "KWOOV Demo.gif"
        m = re.search(r'data-fname="([^"]+)"', tag) or re.search(r"data-fname='([^']+)'", tag)
        if m:
            nm = re.sub(r'[\\/<>:"\r\n]', "_", m.group(1)).strip()[:80]
            if nm:
                if "." not in nm:
                    nm += f".{subtype or 'png'}"
                return nm
        if not src.startswith("data:"):
            base = src.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            if "." in base and 0 < len(base) <= 80:
                return base
        return f"image{len(images) + 1}.{subtype or 'png'}"

    def repl(m):
        prefix, quote, src = m.group(1), m.group(2), m.group(3)
        if src.startswith("cid:"):
            return m.group(0)
        if src in cid_map:
            return f"{prefix}{quote}cid:{cid_map[src]}{quote}"
        if src.startswith("data:"):
            data, subtype = _decode_data_uri(src)
        else:
            data, subtype = _fetch_image_bytes(src)
        if not data:
            return m.group(0)
        cid = f"img{len(images)}-{uuid.uuid4().hex[:10]}@mailtracker"
        cid_map[src] = cid
        # 正则只匹配到 src 为止，data-fname 在 src 后面，要把整个 img 标签补全再取名字
        tail_end = m.string.find(">", m.end())
        full_tag = m.group(0) + (m.string[m.end():tail_end + 1] if tail_end != -1 else "")
        images.append((cid, data, subtype or "png", _name_for(src, subtype or "png", full_tag)))
        return f"{prefix}{quote}cid:{cid}{quote}"

    return _IMG_SRC_RE.sub(repl, html_content), images


def build_message(from_addr: str, item: EmailItem, email_id: Optional[str], content: Optional[str] = None, attachments=None) -> str:
    if content is None:
        content = item.content
    # 先清掉 Gmail 内部图片引用，再内嵌正文图片，最后拼追踪像素（像素必须保持远程地址）
    content = sanitize_outbound_html(content)
    content, images = inline_images(content)
    if email_id:
        tracking_url = f"{BASE_URL}/track/{email_id}"
        pixel_html = f'<br><img src="{tracking_url}" width="1" height="1" alt="" style="display:none !important;" />'
        content = content + pixel_html
    body_part = MIMEMultipart("related") if images else MIMEMultipart("alternative")
    body_part.attach(MIMEText(content, "html", "utf-8"))
    for cid, data, subtype, fname in images:
        if subtype in ("gif", "jpeg", "jpg", "png", "webp"):
            part = MIMEImage(data, _subtype="jpeg" if subtype == "jpg" else subtype)
        else:
            part = MIMEImage(data)
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header("Content-Disposition", "inline", filename=fname)
        body_part.attach(part)
    atts = [a for a in (attachments or []) if (a.data or "").strip()]
    if atts:
        msg = MIMEMultipart("mixed")
        msg.attach(body_part)
        for a in atts:
            raw = base64.b64decode(a.data)
            ctype = (a.content_type or "application/octet-stream").split(";")[0].strip()
            maintype, _, subtype = ctype.partition("/")
            part = MIMEBase(maintype or "application", subtype or "octet-stream")
            part.set_payload(raw)
            mime_encoders.encode_base64(part)
            fname = re.sub(r'[\r\n"]', "_", (a.name or "attachment")).strip() or "attachment"
            part.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(part)
    else:
        msg = body_part
    msg["From"] = from_addr
    msg["To"] = item.to_email
    msg["Subject"] = item.subject
    return msg.as_string()


def send_one(user_id: str, conn: SmtpConnection, item: EmailItem, *,
             link_enabled: bool = False, link_url: str = DEFAULT_LINK_URL,
             link_text: str = DEFAULT_LINK_TEXT, task_id: str = None, attachments=None):
    """发送一封：建记录 -> 拼装链接行 -> 组装（含像素）-> 发送（失败自动重试 1 次，重试前重建连接）。
    返回 (ok: bool, error: Optional[str])
    注意：变量替换在调用方（前端提交前）完成，这里只做链接行拼装，且只拼装一次；
    重试时复用同一份组装好的邮件串，不会重复拼装。"""
    email_id = create_send_record(user_id, item, task_id)
    if not email_id:
        return False, "record_insert_failed"
    assembled = assemble_content(item.content, link_enabled=link_enabled,
                                 link_url=link_url, link_text=link_text)
    msg_str = build_message(conn.account.email, item, email_id, content=assembled, attachments=attachments)
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

MAX_ATTACHMENT_SINGLE = 10 * 1024 * 1024
MAX_ATTACHMENT_TOTAL = 20 * 1024 * 1024

def _validate_attachments(atts):
    """附件护栏：单个 ≤10MB、合计 ≤20MB（Gmail 单封上限 25MB，要留出正文余量）。"""
    total = 0
    for a in atts or []:
        try:
            raw = base64.b64decode(a.data or "", validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail=f"附件「{a.name}」数据损坏，请重新添加")
        if len(raw) > MAX_ATTACHMENT_SINGLE:
            raise HTTPException(status_code=400, detail=f"附件「{a.name}」超过 10MB，请换小一点的文件")
        total += len(raw)
    if total > MAX_ATTACHMENT_TOTAL:
        raise HTTPException(status_code=400, detail="附件合计超过 20MB，请删减后再发")


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
            ok, err = send_one(req.user_id, conns[chosen.email], item,
                               link_enabled=req.link_enabled, link_url=req.link_url,
                               link_text=req.link_text, task_id=task_id,
                               attachments=req.attachments)
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

            # 每封都同步一次计数：进度页读的是数据库，这样它和发信页看到的实时进度一致（当前量级下每封一次小更新无压力）
            _sync_task_db(task)
            gap = 0.0
            if req.interval_seconds and req.interval_seconds > 0:
                lo = float(req.interval_seconds)
                hi = float(req.interval_max_seconds) if req.interval_max_seconds and float(req.interval_max_seconds) > lo else lo
                gap = random.uniform(lo, hi)
            if gap > 0:
                slept = 0.0
                while slept < gap and task["status"] not in ("stopped",):
                    time.sleep(min(0.2, gap - slept))
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
        _sync_task_db(task)

@app.post("/api/tasks")
async def create_batch_task(req: BatchTaskRequest, background_tasks: BackgroundTasks,
                            authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    # 硬闸：同一用户已有进行中（或暂停）的任务时拒绝再建，防止重复点发送导致双任务同号并发、名单重叠重复投递
    for t in TASKS.values():
        if t.get("user_id") == user_id and t.get("status") in ("running", "paused"):
            done_n = t["sent"] + t["failed"] + t["skipped"]
            raise HTTPException(status_code=409, detail=f"已有一个任务正在进行（已处理 {done_n}/{t['total']}），请等它完成，或先停止它再发新任务")
    if req.interval_seconds <= 0:
        raise HTTPException(status_code=400, detail="发送间隔不能为 0：每封之间必须留间隔，请把最少间隔填成大于 0 的分钟数")
    _validate_attachments(req.attachments)
    if not req.emails:
        raise HTTPException(status_code=400, detail="名单为空")
    if len(req.emails) > MAX_EMAILS_PER_TASK:
        raise HTTPException(status_code=400, detail=f"单批最多 {MAX_EMAILS_PER_TASK} 位收件人，请删减名单后重试")
    accounts = _load_user_accounts(user_id)
    if not accounts:
        raise HTTPException(status_code=400, detail="还没有发信账号，请先在账号区添加（云端保存）")
    req.user_id = user_id
    req.accounts = accounts
    task_id = _launch_task(user_id, req, background_tasks)
    return {"success": True, "task_id": task_id, "status": "running", "total": len(req.emails)}

def _launch_task(user_id: str, req: BatchTaskRequest, background_tasks: BackgroundTasks,
                 continued_from: str = "") -> str:
    """真正把一个任务跑起来：建内存状态 + 落库（含 payload 快照）+ 挂后台执行。
    payload 存下名单与发送参数，任务被打断后“继续发送”和“明细回看”全靠它。"""
    task_id = uuid.uuid4().hex[:12]
    TASKS[task_id] = {
        "id": task_id,
        "task_id": task_id,
        "user_id": user_id,
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
    payload = {
        "emails": [e.model_dump() for e in req.emails],
        "daily_cap": req.daily_cap,
        "interval_seconds": req.interval_seconds,
        "interval_max_seconds": req.interval_max_seconds,
        "link_enabled": req.link_enabled,
        "link_url": req.link_url,
        "link_text": req.link_text,
        "subject_template": req.subject_template,
        "batch_name": req.batch_name,
        "continued_from": continued_from,
    }
    try:
        _db_exec(
            "INSERT INTO send_tasks (id, user_id, status, total, sent, failed, skipped, "
            "accounts, subject, created_at, notes, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (task_id, user_id, "running", len(req.emails), 0, 0, 0,
             ",".join(a.email for a in req.accounts),
             req.batch_name or req.subject_template or (req.emails[0].subject if req.emails else ""),
             TASKS[task_id]["created_at"], "", json.dumps(payload, ensure_ascii=False)))
    except Exception as e:
        print(f"insert send_tasks failed: {e}")
    background_tasks.add_task(run_batch_task, task_id, req)
    return task_id

def _owned_task(task_id: str, authorization: Optional[str]) -> dict:
    user_id = _verify_user_id(authorization)
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("user_id") and task["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.get("/api/tasks/current")
async def get_current_task(authorization: Optional[str] = Header(default=None)):
    """返回当前用户正在进行（或暂停）的最新任务，供发信页重进时把实时进度接回来；没有则 task_id 为 null。"""
    user_id = _verify_user_id(authorization)
    mine = [t for t in TASKS.values()
            if t.get("user_id") == user_id and t.get("status") in ("running", "paused")]
    if not mine:
        return {"task_id": None}
    t = max(mine, key=lambda x: x.get("created_at") or "")
    return _public_task(t)


@app.get("/api/tasks/search-recipient")
async def search_recipient(q: str = "", authorization: Optional[str] = Header(default=None)):
    """按收件人邮箱搜发送记录，并定位到所属任务（进度表顶部的全局搜索用）。
    新记录带 task_id 可精确归属；旧记录没有 task_id 的，按发送时间落进哪个任务的时间窗来匹配，标 legacy。"""
    user_id = _verify_user_id(authorization)
    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="至少输入 2 个字符再搜")
    rows = _db_query(
        "SELECT e.recipient, e.status, e.send_time, e.open_count, e.task_id, "
        "t.subject AS task_subject, t.created_at AS task_created_at "
        "FROM emails e LEFT JOIN send_tasks t ON e.task_id = t.id "
        "WHERE e.user_id = %s AND e.recipient ILIKE %s "
        "ORDER BY e.send_time DESC LIMIT 100",
        (user_id, f"%{q}%"))
    orphans = [r for r in rows if not r.get("task_id")]
    if orphans:
        tasks = _db_query(
            "SELECT id, subject, created_at FROM send_tasks WHERE user_id = %s "
            "ORDER BY created_at", (user_id,))

        def _as_dt(v):
            if isinstance(v, datetime):
                return v
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except Exception:
                return None

        task_dts = [(_as_dt(t["created_at"]), t) for t in tasks]
        for r in orphans:
            st = _as_dt(r["send_time"])
            if st is None:
                continue
            best = None
            for tdt, t in task_dts:
                if tdt is not None and tdt <= st:
                    best = t
                else:
                    break
            if best:
                r["task_id"] = best["id"]
                r["task_subject"] = best["subject"]
                r["task_created_at"] = best["created_at"]
                r["legacy"] = True
    for r in rows:
        r.setdefault("legacy", False)
    return {"matches": rows}


@app.get("/api/tasks/{task_id}")
async def get_batch_task(task_id: str, authorization: Optional[str] = Header(default=None)):
    return _public_task(_owned_task(task_id, authorization))

@app.post("/api/tasks/{task_id}/pause")
async def pause_batch_task(task_id: str, authorization: Optional[str] = Header(default=None)):
    task = _owned_task(task_id, authorization)
    if task["status"] == "running":
        task["status"] = "paused"
        _sync_task_db(task)
    return _public_task(task)

@app.post("/api/tasks/{task_id}/resume")
async def resume_batch_task(task_id: str, authorization: Optional[str] = Header(default=None)):
    task = _owned_task(task_id, authorization)
    if task["status"] == "paused":
        task["status"] = "running"
        _sync_task_db(task)
    return _public_task(task)

@app.post("/api/tasks/{task_id}/stop")
async def stop_batch_task(task_id: str, authorization: Optional[str] = Header(default=None)):
    task = _owned_task(task_id, authorization)
    if task["status"] in ("running", "paused"):
        task["status"] = "stopped"
    return _public_task(task)

# ==========================================
# 任务回看 / 继续发送 / 终止（面向数据库里的任务行，含已被打断的）
# ==========================================
def _task_row(task_id: str, user_id: str):
    rows = _db_query(
        "SELECT id, status, total, sent, failed, skipped, subject, created_at, payload "
        "FROM send_tasks WHERE id = %s AND user_id = %s", (task_id, user_id))
    return rows[0] if rows else None


@app.get("/api/tasks/{task_id}/detail")
async def task_detail(task_id: str, authorization: Optional[str] = Header(default=None)):
    """任务明细：这一批到底给哪些人发了、每人什么状态。
    新任务（有 payload 快照 + 记录带 task_id）：名单与逐封状态精确对应，未发出的标“未发送”。
    旧任务（无快照）：按任务时间窗到下一个任务为止去 emails 表里匹配，标 legacy 供前端提示仅供参考。"""
    user_id = _verify_user_id(authorization)
    row = _task_row(task_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    payload = None
    if row.get("payload"):
        try:
            payload = json.loads(row["payload"])
        except Exception:
            payload = None
    recs = _db_query(
        "SELECT recipient, status, send_time, open_count, error FROM emails "
        "WHERE task_id = %s AND user_id = %s ORDER BY send_time", (task_id, user_id))
    recipients = []
    legacy = False
    if payload and payload.get("emails"):
        by_email = {}
        for r in recs:
            by_email[r["recipient"]] = r  # 按时间升序，后写的覆盖先写的，取最后一次状态
        for e in payload["emails"]:
            r = by_email.get(e["to_email"])
            if r is None:
                recipients.append({"email": e["to_email"], "status": "unsent",
                                   "send_time": None, "open_count": 0, "error": None})
            else:
                recipients.append({"email": e["to_email"], "status": r["status"],
                                   "send_time": r["send_time"], "open_count": r["open_count"] or 0,
                                   "error": r.get("error")})
    else:
        legacy = True
        created = row["created_at"]
        nxt = _db_query(
            "SELECT MIN(created_at) AS nxt FROM send_tasks WHERE user_id = %s AND created_at > %s",
            (user_id, created))
        window_end = (nxt[0]["nxt"] if nxt and nxt[0]["nxt"]
                      else "2999-01-01T00:00:00+00:00")
        rows = _db_query(
            "SELECT recipient, status, send_time, open_count, error FROM emails "
            "WHERE user_id = %s AND send_time::timestamptz >= %s AND send_time::timestamptz < %s "
            "ORDER BY send_time", (user_id, created, window_end))
        for r in rows:
            recipients.append({"email": r["recipient"], "status": r["status"],
                               "send_time": r["send_time"], "open_count": r["open_count"] or 0,
                               "error": r.get("error")})
    return {
        "task_id": row["id"], "status": row["status"], "subject": row["subject"],
        "total": row["total"], "sent": row["sent"], "failed": row["failed"],
        "skipped": row["skipped"], "created_at": row["created_at"],
        "legacy": legacy, "recipients": recipients,
    }


@app.post("/api/tasks/{task_id}/continue")
async def continue_task(task_id: str, background_tasks: BackgroundTasks,
                        authorization: Optional[str] = Header(default=None)):
    """继续发送：拿原任务保存的名单，跳过一切已经尝试过的人（已发、失败、以及状态不明的
    pending——宁可漏发也绝不重复投递），按原参数对其余的人新建一个任务接着发。"""
    user_id = _verify_user_id(authorization)
    row = _task_row(task_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] in ("running", "paused"):
        raise HTTPException(status_code=400, detail="这个任务还在进行中，不用继续")
    payload = None
    if row.get("payload"):
        try:
            payload = json.loads(row["payload"])
        except Exception:
            payload = None
    if not payload or not payload.get("emails"):
        raise HTTPException(status_code=400,
                            detail="这个任务是旧版本创建的，当时没有保存名单和正文，无法自动继续；请重新准备名单发送，并注意去掉已经发过的人")
    for t in TASKS.values():
        if t.get("user_id") == user_id and t.get("status") in ("running", "paused"):
            done_n = t["sent"] + t["failed"] + t["skipped"]
            raise HTTPException(status_code=409,
                                detail=f"已有一个任务正在进行（已处理 {done_n}/{t['total']}），请等它完成，或先停止它再继续")
    attempted = {r["recipient"] for r in _db_query(
        "SELECT DISTINCT recipient FROM emails WHERE task_id = %s AND user_id = %s",
        (task_id, user_id))}
    remaining = [e for e in payload["emails"] if e["to_email"] not in attempted]
    skipped_done = len(payload["emails"]) - len(remaining)
    if not remaining:
        _db_exec("UPDATE send_tasks SET status = 'done' WHERE id = %s", (task_id,))
        return {"success": True, "task_id": None, "remaining": 0,
                "skipped_done": skipped_done, "message": "名单里的人都已经发过了，没有需要继续的"}
    accounts = _load_user_accounts(user_id)
    if not accounts:
        raise HTTPException(status_code=400, detail="还没有发信账号，请先在账号区添加（云端保存）")
    req = BatchTaskRequest(
        emails=[EmailItem(**e) for e in remaining],
        attachments=[AttachmentItem(**a) for a in payload.get("attachments", [])],
        daily_cap=payload.get("daily_cap", 50),
        interval_seconds=payload.get("interval_seconds", 0),
        interval_max_seconds=payload.get("interval_max_seconds", 0),
        link_enabled=payload.get("link_enabled", False),
        link_url=payload.get("link_url", ""),
        link_text=payload.get("link_text", ""),
    )
    req.user_id = user_id
    req.accounts = accounts
    new_id = _launch_task(user_id, req, background_tasks, continued_from=task_id)
    _db_exec("UPDATE send_tasks SET status = 'continued' WHERE id = %s", (task_id,))
    return {"success": True, "task_id": new_id, "remaining": len(remaining),
            "skipped_done": skipped_done}


@app.post("/api/tasks/{task_id}/terminate")
async def terminate_task(task_id: str, authorization: Optional[str] = Header(default=None)):
    """终止任务：任务还活着（在本进程内存里）就真停；只剩数据库里一行记录的
    （已被重启打断的孤儿任务）就把状态标成已停止，别再永远挂着“进行中”。"""
    user_id = _verify_user_id(authorization)
    live = TASKS.get(task_id)
    if live is not None and live.get("user_id") == user_id:
        if live["status"] in ("running", "paused"):
            live["status"] = "stopped"
            _sync_task_db(live)
        return {"success": True, "mode": "live", "status": live["status"]}
    row = _task_row(task_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] in ("running", "paused", "interrupted", "failed"):
        _db_exec("UPDATE send_tasks SET status = 'stopped' WHERE id = %s", (task_id,))
        return {"success": True, "mode": "record", "status": "stopped"}
    return {"success": True, "mode": "record", "status": row["status"]}

# ==========================================
# 用户个人设置（按登录账号隔离保存，如插入链接时填的地址与文字）
# 规则：凡是要长期记住的填写内容，一律按账号存这里，各账号互不可见，
# 绝不写死在页面里给所有人看（2026-11-03 用户定的规矩）。
# ==========================================
class LinkSettingIn(BaseModel):
    link_url: str = ""
    link_text: str = ""


@app.get("/api/settings/link")
async def get_link_setting(authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    rows = _db_query("SELECT key, value FROM user_settings WHERE user_id = %s "
                     "AND key IN ('link_url', 'link_text')", (user_id,))
    d = {r["key"]: r["value"] for r in rows}
    return {"link_url": d.get("link_url") or "", "link_text": d.get("link_text") or ""}


@app.post("/api/settings/link")
async def save_link_setting(payload: LinkSettingIn,
                            authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    for k, v in (("link_url", payload.link_url), ("link_text", payload.link_text)):
        _db_exec("INSERT INTO user_settings (user_id, key, value) VALUES (%s, %s, %s) "
                 "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
                 (user_id, k, v or ""))
    return {"success": True}


# ==========================================
# 发信账号接口（云端保存，授权码加密；前端永远看不到明文）
# ==========================================
class AccountIn(BaseModel):
    email: str
    password: str


@app.get("/api/version")
async def api_version():
    return {"v": "v5-fix20-cursorimg"}


@app.get("/api/accounts")
async def list_accounts(authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    rows = _db_query(
        "SELECT id, email, created_at FROM sender_accounts WHERE user_id = %s ORDER BY created_at",
        (user_id,))
    return {"accounts": [
        {"id": str(r["id"]), "email": r["email"],
         "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"]}
        for r in rows]}


@app.post("/api/accounts")
async def add_account(body: AccountIn, authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    email = body.email.strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="邮箱格式不对")
    if not body.password.strip():
        raise HTTPException(status_code=400, detail="授权码不能为空")
    _db_exec(
        "INSERT INTO sender_accounts (user_id, email, pass_enc) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, email) DO UPDATE SET pass_enc = EXCLUDED.pass_enc",
        (user_id, email, _encrypt_pass(body.password.strip())))
    return {"success": True}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str, authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    _db_exec("DELETE FROM sender_accounts WHERE id = %s AND user_id = %s",
             (account_id, user_id))
    return {"success": True}


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


@app.get("/progress", response_class=HTMLResponse)
async def progress_page(request: Request):
    return _page_with_sender_cookie("progress.html", request)


@app.get("/preview-home", response_class=HTMLResponse)
async def preview_home_page(request: Request):
    """新版首页设计稿预览（站长确认后再替换正式首页）。"""
    return _serve_page_file("index_new.html")

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
        rows = _db_query(
            "SELECT open_count, first_open_time FROM emails WHERE id = %s", (email_id,))
        if rows:
            new_count = (rows[0].get("open_count") or 0) + 1
            first_open = rows[0].get("first_open_time") or datetime.now(timezone.utc).isoformat()
            _db_exec("UPDATE emails SET open_count = %s, first_open_time = %s WHERE id = %s",
                     (new_count, first_open, email_id))
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
async def send_batch_emails(req: SendBatchRequest,
                            authorization: Optional[str] = Header(default=None)):
    req.user_id = _verify_user_id(authorization)
    _validate_attachments(req.attachments)
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
        ok, _err = send_one(req.user_id, conn, item,
                            link_enabled=req.link_enabled, link_url=req.link_url,
                            link_text=req.link_text, attachments=req.attachments)
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
