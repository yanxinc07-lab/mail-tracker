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
from email.utils import parsedate_to_datetime, parseaddr
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders as mime_encoders
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional
from fastapi import FastAPI, Request, HTTPException, Response, BackgroundTasks, Header
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
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

# 站长管理（fix23，用户 2026-11-05 定版）：站长本人不限量；其他用户最多绑
# NONOWNER_MAX_ACCOUNTS 个发信账号、每人每天合计最多发 NONOWNER_DAILY_CAP 封；
# 新用户注册名额封顶 REGISTRATION_CAP 位（不含站长）。站长判定：登录邮箱在
# OWNER_EMAILS 内，或 user_id 是全站第一个绑号的账号（站长是最早的用户，
# 兜底防止站长登录邮箱与名单不一致时把站长自己限住）。
OWNER_EMAILS = {"yanxinc07@gmail.com"}
NONOWNER_MAX_ACCOUNTS = 3
NONOWNER_DAILY_CAP = 60
REGISTRATION_CAP = 20

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

class SendBatchRequest(BaseModel):
    user_email: str = ""
    user_id: str = ""
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
        _USER_EMAILS[sub] = payload.get("email") or ""
        _touch_registry(sub)
        return sub
    except HTTPException:
        raise
    except Exception as e:
        print(f"auth verify failed: {e}")
    raise HTTPException(status_code=401, detail="登录已过期，请重新登录")


_USER_EMAILS = {}        # user_id -> 登录邮箱（验签时顺手记下）
_REGISTRY_SEEN = set()   # 本进程已登记过的 user_id，避免每个请求都写注册表


_REGISTRY_ENSURED = False


def _ensure_registry():
    """确保 user_registry 表存在（进程内只真正执行一次）。
    fix23 上线后监控室 500 的教训：建表原本只挂在启动自检里，启动时任一
    语句失败（整段一个 try）就会把建表一起跳过，之后每个查注册表的接口都
    500。故改为懒补建：第一次用到时建，建好后本进程不再重复。"""
    global _REGISTRY_ENSURED
    if _REGISTRY_ENSURED:
        return
    _db_exec("CREATE TABLE IF NOT EXISTS user_registry (user_id text PRIMARY KEY, "
             "email text, first_seen timestamptz DEFAULT now(), "
             "last_seen timestamptz DEFAULT now(), banned boolean DEFAULT false)")
    _REGISTRY_ENSURED = True


def _touch_registry(user_id: str):
    """把登录用户登记进 user_registry：注册名额计数与监控室名单的数据源。
    每个进程对每个用户只写一次；失败只记日志，绝不能拖垮验签本身。"""
    if user_id in _REGISTRY_SEEN:
        return
    _REGISTRY_SEEN.add(user_id)
    try:
        _ensure_registry()
        _db_exec(
            "INSERT INTO user_registry (user_id, email) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET email = EXCLUDED.email, last_seen = now()",
            (user_id, _USER_EMAILS.get(user_id) or None))
    except Exception as e:
        print(f"registry touch failed: {e}")


def _is_owner(user_id: str) -> bool:
    email = (_USER_EMAILS.get(user_id) or "").lower()
    if email in OWNER_EMAILS:
        return True
    try:
        rows = _db_query("SELECT user_id FROM sender_accounts ORDER BY created_at LIMIT 1")
        return bool(rows) and rows[0]["user_id"] == user_id
    except Exception:
        return False


def _is_banned(user_id: str) -> bool:
    try:
        rows = _db_query("SELECT banned FROM user_registry WHERE user_id = %s", (user_id,))
        return bool(rows) and bool(rows[0].get("banned"))
    except Exception:
        return False


def _today_start_utc_iso() -> str:
    """北京时间今日 0 点换算成 UTC 的 ISO 文本。emails.send_time 存的是 UTC
    ISO 文本，同格式字符串可直接比较大小。"""
    cst = timezone(timedelta(hours=8))
    start_local = datetime.now(cst).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).isoformat()


def _sent_today_count(user_id: str) -> int:
    rows = _db_query(
        "SELECT COUNT(*) AS n FROM emails WHERE user_id = %s AND status = 'sent' AND send_time >= %s",
        (user_id, _today_start_utc_iso()))
    return int(rows[0]["n"]) if rows else 0


def _check_user_can_send(user_id: str, n_new: int):
    """他人发送闸口（站长豁免）：被封禁拒绝；今日已发 + 本批封数超过每日合计上限拒绝。"""
    if _is_owner(user_id):
        return
    if _is_banned(user_id):
        raise HTTPException(status_code=403, detail="该账号已被停用，请联系站长")
    sent = _sent_today_count(user_id)
    if sent + n_new > NONOWNER_DAILY_CAP:
        raise HTTPException(status_code=400, detail=(
            f"已达每日发送上限：你今天已发 {sent} 封，每位用户每天最多 {NONOWNER_DAILY_CAP} 封"
            f"（名下所有账号合计），本批 {n_new} 封发不了，请删减名单或明天再发"))


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


# ==========================================
# 回复巡逻（fix26，用户 2026-11-04 定版）：多邮箱统一盯梢
# ----------------------------------------------------------
# 定版口径：有回信就报，不管内容——不做内容分类、不专门过滤自动回复；
# 退信发件人不是收件人本人地址，按“发件人是否在已发送名单里”匹配天然不算。
# 只存标题行（发件人/主题/日期），绝不存正文。巡逻用发信账号已存的应用专用
# 密码走 IMAP 只读检查 INBOX 邮件头，用户零额外配置。
# 范围：只巡逻近 30 天有发送记录的用户（emails 表不记发信账号，落到用户后
# 巡逻其名下全部发信账号）。每约 7 分钟一轮；单账号失败只记日志并退避，
# 巡逻线程是 daemon 且全程异常捕获，绝不拖垮主服务。
# 环境变量 REPLY_WATCH=off 可整体关闭。
# ==========================================
REPLY_WATCH_ENABLED = os.getenv("REPLY_WATCH", "on").strip().lower() not in ("off", "0", "false", "no")
REPLY_WATCH_INTERVAL = 7 * 60          # 每轮间隔（秒）
REPLY_WATCH_SINCE_DAYS = 30            # 只巡逻近 30 天有发送的用户
REPLY_WATCH_FETCH_CAP = 300            # 单账号单轮最多处理的新邮件头数（水位推进，下一轮续）
_REPLY_WATCH_BACKOFF_SECONDS = 20 * 60  # 单账号失败后的退避时长
_REPLY_WATCH_STARTED = False
_REPLY_WATCH_START_LOCK = threading.Lock()
_REPLY_WATCH_ROUND_LOCK = threading.Lock()
_REPLY_WATCH_BACKOFF: Dict[str, float] = {}

_REPLIES_ENSURED = False


def _ensure_replies():
    """确保 replies 表存在（懒补建，沿 fix24 的 _ensure_registry 模式：
    启动自检万一漏建，用到的地方自己补，进程内只真正执行一次）。"""
    global _REPLIES_ENSURED
    if _REPLIES_ENSURED:
        return
    _db_exec("CREATE TABLE IF NOT EXISTS replies (id text PRIMARY KEY, user_id text, "
             "account_email text, from_email text, from_name text, subject text, "
             "received_at timestamptz, gmail_thrid text, gmail_msgid text, "
             "message_id text, task_id text, created_at timestamptz DEFAULT now(), "
             "UNIQUE (account_email, message_id))")
    _db_exec("CREATE INDEX IF NOT EXISTS idx_replies_user ON replies (user_id, received_at DESC)")
    _db_exec("CREATE INDEX IF NOT EXISTS idx_replies_task ON replies (task_id)")
    _REPLIES_ENSURED = True


_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_since_str(dt: datetime) -> str:
    """IMAP SINCE 的日期格式 dd-Mon-yyyy（月份名按 RFC 写死英文，不吃进程 locale）。"""
    return f"{dt.day:02d}-{_IMAP_MONTHS[dt.month - 1]}-{dt.year}"


def _parse_from_header(raw) -> tuple:
    """From 头 → (发件人邮箱小写, 显示名)。解析不出邮箱时邮箱为空串。"""
    name, addr = parseaddr(raw or "")
    return (addr or "").strip().lower(), _decode_mime_header(name).strip()


def _parse_msg_datetime(raw) -> Optional[datetime]:
    """Date 头 → 带时区的 datetime；解析不了返回 None（调用方用当前时间兜底）。"""
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _gmail_reply_link(account_email: str, gmail_thrid, gmail_msgid, from_email: str,
                      message_id: str = None) -> str:
    """回复直达 Gmail 的链接（2026-11-04 补充定版；fix28 修正编号进制与兜底）：
    谷歌内部编号（X-GM-THRID/X-GM-MSGID）入库时是十进制，而 Gmail 网页
    #inbox/<id> 只认十六进制，拼链接前必须先转十六进制，否则落不到原信；
    优先 thrid（打开整个会话），无 thrid 时用 msgid（打开单封）。
    编号缺失或转换异常时，退回按 RFC Message-ID 搜索定位（比按发件人搜更准），
    再兜底才是按发件人搜索。authuser 始终带上、切到正确的发信邮箱。"""
    base = f"https://mail.google.com/mail/u/?authuser={account_email}"
    for ident in (gmail_thrid, gmail_msgid):
        if ident is None or str(ident).strip() == "":
            continue
        try:
            return f"{base}#inbox/{format(int(str(ident).strip()), 'x')}"
        except (TypeError, ValueError):
            continue
    mid = (message_id or "").strip().strip("<>").strip()
    if mid:
        return f"{base}#search/rfc822msgid%3A{mid}"
    return f"{base}#search/from%3A{from_email}"


def _imap_fetch_headers(account_email: str, password: str, since: datetime,
                        expect_uv: Optional[str] = None, after_uid: int = 0,
                        cap: int = REPLY_WATCH_FETCH_CAP):
    """只读拉取 INBOX 邮件头（不取正文）。返回 (uidvalidity, [(uid, headers)])，
    headers 含 from/subject/date/message_id/gmail_thrid/gmail_msgid。
    只返回 uid 大于水位的（uidvalidity 与水位记录一致时才按水位过滤），升序、最多 cap 封。
    登录/连接失败抛异常，由调用方记日志并退避。"""
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    try:
        mail.login(account_email, password)
        typ, _ = mail.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError("select INBOX failed")
        uidvalidity = "0"
        try:
            _t, d = mail.response("UIDVALIDITY")
            if d and d[0]:
                uidvalidity = (d[0].decode() if isinstance(d[0], bytes) else str(d[0])).strip()
        except Exception:
            pass
        eff_after = after_uid if (expect_uv and expect_uv == uidvalidity) else 0
        typ, data = mail.uid("SEARCH", None, "SINCE", _imap_since_str(since))
        if typ != "OK":
            raise RuntimeError("uid search failed")
        uids = []
        if data and data[0]:
            uids = [int(x) for x in data[0].split()]
        uids = sorted(u for u in uids if u > eff_after)[:cap]
        out = []
        for uid in uids:
            typ, fdata = mail.uid(
                "FETCH", str(uid),
                "(X-GM-THRID X-GM-MSGID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            if typ != "OK" or not fdata:
                continue
            meta, raw_headers = b"", None
            for item in fdata:
                if isinstance(item, tuple):
                    meta = item[0] or b""
                    raw_headers = item[1]
                    break
            if raw_headers is None:
                continue
            if isinstance(meta, str):
                meta = meta.encode()
            thrid_m = re.search(rb"X-GM-THRID\s+(\d+)", meta)
            msgid_m = re.search(rb"X-GM-MSGID\s+(\d+)", meta)
            msg = message_from_bytes(raw_headers)
            out.append((uid, {
                "from": msg.get("From") or "",
                "subject": _decode_mime_header(msg.get("Subject")),
                "date": msg.get("Date") or "",
                "message_id": (msg.get("Message-ID") or "").strip(),
                "gmail_thrid": thrid_m.group(1).decode() if thrid_m else None,
                "gmail_msgid": msgid_m.group(1).decode() if msgid_m else None,
            }))
        return uidvalidity, out
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _reply_watermark_get(user_id: str, account_email: str) -> dict:
    """巡逻水位存 user_settings（key=reply_watch:<账号>）：{uv, uid, ts}，没有返回空 dict。"""
    try:
        rows = _db_query("SELECT value FROM user_settings WHERE user_id = %s AND key = %s",
                         (user_id, f"reply_watch:{account_email}"))
        if rows and rows[0].get("value"):
            return json.loads(rows[0]["value"]) or {}
    except Exception as e:
        print(f"reply watermark get failed: {e}")
    return {}


def _reply_watermark_set(user_id: str, account_email: str, wm: dict):
    try:
        _db_exec("INSERT INTO user_settings (user_id, key, value) VALUES (%s, %s, %s) "
                 "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
                 (user_id, f"reply_watch:{account_email}", json.dumps(wm)))
    except Exception as e:
        print(f"reply watermark set failed: {e}")


def _poll_account_replies(user_id: str, account: SmtpAccount,
                          sent_latest: Dict[str, Optional[str]]) -> int:
    """巡逻一个发信账号：把发件人命中已发送名单的新邮件记入 replies，返回新增条数。
    sent_latest: 收件人邮箱（小写）→ 最近一次任务 id（归属用）。"""
    email = account.email
    if _REPLY_WATCH_BACKOFF.get(email, 0) > time.time():
        return 0
    # fix27：自有发信账号发出的信永不算回复。自发自测时收件人就是本人，
    # 本人地址进了已发送名单，自测信落进本人 INBOX 后发件人（=本人账号）
    # 会命中名单被误记成客户回复（用户 2026-11-06 截图实证）。这里是所有
    # 邮件头记入 replies 的必经之路，排除必须落在这里而不是某个调用点。
    own_emails = {(a.email or "").strip().lower()
                  for a in _load_user_accounts(user_id)}
    own_emails.add((email or "").strip().lower())
    wm = _reply_watermark_get(user_id, email)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=REPLY_WATCH_SINCE_DAYS)
    if wm.get("ts"):
        try:
            # SINCE 只有日期粒度：从水位时间再往前让 2 天，靠 UID 水位 + 唯一键保证不漏不重
            since = datetime.fromisoformat(wm["ts"]) - timedelta(days=2)
        except Exception:
            pass
    uidvalidity, headers = _imap_fetch_headers(
        email, account.password, since,
        expect_uv=wm.get("uv"), after_uid=int(wm.get("uid") or 0))
    new_count = 0
    max_uid = int(wm.get("uid") or 0) if wm.get("uv") == uidvalidity else 0
    for uid, h in headers:
        max_uid = max(max_uid, uid)
        from_email, from_name = _parse_from_header(h.get("from"))
        if not from_email or from_email in own_emails:
            continue
        if from_email not in sent_latest:
            continue
        mid = h.get("message_id") or (
            f"x-gm-msgid:{h['gmail_msgid']}" if h.get("gmail_msgid")
            else f"uid:{uidvalidity}:{uid}")
        exists = _db_query(
            "SELECT id FROM replies WHERE account_email = %s AND message_id = %s",
            (email, mid))
        if exists:
            continue
        received = _parse_msg_datetime(h.get("date")) or now
        _db_exec(
            "INSERT INTO replies (id, user_id, account_email, from_email, from_name, "
            "subject, received_at, gmail_thrid, gmail_msgid, message_id, task_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (account_email, message_id) DO NOTHING",
            (uuid.uuid4().hex, user_id, email, from_email, from_name or None,
             h.get("subject") or None, received, h.get("gmail_thrid"),
             h.get("gmail_msgid"), mid, sent_latest.get(from_email)))
        new_count += 1
    _reply_watermark_set(user_id, email,
                         {"uv": uidvalidity, "uid": max_uid, "ts": now.isoformat()})
    return new_count


def _reply_watch_round():
    """一轮巡逻：近 30 天有发送的用户，逐个账号查回复。单用户/单账号失败不影响其他。"""
    if not _REPLY_WATCH_ROUND_LOCK.acquire(blocking=False):
        return
    try:
        _ensure_replies()
        since_iso = (datetime.now(timezone.utc)
                     - timedelta(days=REPLY_WATCH_SINCE_DAYS)).isoformat()
        users = _db_query(
            "SELECT DISTINCT user_id::text AS uid FROM emails "
            "WHERE status = 'sent' AND send_time >= %s", (since_iso,))
        for u in users:
            user_id = u["uid"]
            try:
                sent_rows = _db_query(
                    "SELECT DISTINCT ON (lower(recipient)) lower(recipient) AS rcpt, task_id "
                    "FROM emails WHERE user_id = %s AND status = 'sent' "
                    "ORDER BY lower(recipient), send_time DESC", (user_id,))
                sent_latest = {r["rcpt"]: r.get("task_id")
                               for r in sent_rows if r.get("rcpt")}
                if not sent_latest:
                    continue
                for account in _load_user_accounts(user_id):
                    try:
                        n = _poll_account_replies(user_id, account, sent_latest)
                        if n:
                            print(f"reply watch: {account.email} 新增回复 {n} 条")
                    except Exception as e:
                        _REPLY_WATCH_BACKOFF[account.email] = (
                            time.time() + _REPLY_WATCH_BACKOFF_SECONDS)
                        print(f"reply watch account failed {account.email}: {e}")
            except Exception as e:
                print(f"reply watch user failed {user_id}: {e}")
    finally:
        _REPLY_WATCH_ROUND_LOCK.release()


def _reply_watch_loop():
    while True:
        try:
            _reply_watch_round()
        except Exception as e:
            print(f"reply watch round failed: {e}")
        slept = 0
        while slept < REPLY_WATCH_INTERVAL:
            step = min(15, REPLY_WATCH_INTERVAL - slept)
            time.sleep(step)
            slept += step


def start_reply_watch():
    """启动回复巡逻线程（进程内只启一次；REPLY_WATCH=off 时不启）。"""
    global _REPLY_WATCH_STARTED
    if not REPLY_WATCH_ENABLED:
        return
    with _REPLY_WATCH_START_LOCK:
        if _REPLY_WATCH_STARTED:
            return
        _REPLY_WATCH_STARTED = True
    threading.Thread(target=_reply_watch_loop, daemon=True, name="reply-watch").start()
    print("reply watch started")


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
    for stmt in (
        "ALTER TABLE emails ADD COLUMN IF NOT EXISTS task_id text",
        # fix32：emails 的 error 列从未真正落到线上库（原版 init_db 没有它、
        # 历次改造也没人 ALTER 过），但 fix15 起明细/大盘的 SELECT 一直在读它，
        # 线上大盘因此 500（UndefinedColumn）。启动时幂等补列，与 task_id 同例。
        "ALTER TABLE emails ADD COLUMN IF NOT EXISTS error text",
        "CREATE TABLE IF NOT EXISTS user_settings (user_id text NOT NULL, "
        "key text NOT NULL, value text, PRIMARY KEY (user_id, key))",
        "CREATE TABLE IF NOT EXISTS user_registry (user_id text PRIMARY KEY, "
        "email text, first_seen timestamptz DEFAULT now(), "
        "last_seen timestamptz DEFAULT now(), banned boolean DEFAULT false)",
        "ALTER TABLE send_tasks ADD COLUMN IF NOT EXISTS payload text",
        "CREATE TABLE IF NOT EXISTS replies (id text PRIMARY KEY, user_id text, "
        "account_email text, from_email text, from_name text, subject text, "
        "received_at timestamptz, gmail_thrid text, gmail_msgid text, "
        "message_id text, task_id text, created_at timestamptz DEFAULT now(), "
        "UNIQUE (account_email, message_id))",
        "CREATE INDEX IF NOT EXISTS idx_replies_user ON replies (user_id, received_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_replies_task ON replies (task_id)",
        # fix30：打开流水表（恢复接手前原版的逐次打开记录），emails 只存汇总
        "CREATE TABLE IF NOT EXISTS email_logs (log_id BIGSERIAL PRIMARY KEY, "
        "email_id TEXT, open_time TEXT, is_scanner BOOLEAN DEFAULT FALSE)",
        "CREATE INDEX IF NOT EXISTS idx_email_logs_email ON email_logs (email_id)",
        # fix27 存量清理：fix26 巡逻把自发自测信误记成的行（发件人=发信账号本人）
        # 每次启动幂等删一遍，删完即净、无行可删时无害。
        "DELETE FROM public.replies WHERE lower(from_email) = lower(account_email)",
        "UPDATE send_tasks SET status = 'interrupted' WHERE status IN ('running', 'paused')",
    ):
        try:
            _db_exec(stmt)
        except Exception as e:
            print(f"startup housekeeping stmt failed: {e} :: {stmt[:60]}")
    start_reply_watch()

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


def build_message(from_addr: str, item: EmailItem, email_id: Optional[str], content: Optional[str] = None) -> str:
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
    msg = body_part
    msg["From"] = from_addr
    msg["To"] = item.to_email
    msg["Subject"] = item.subject
    return msg.as_string()


def send_one(user_id: str, conn: SmtpConnection, item: EmailItem, *,
             link_enabled: bool = False, link_url: str = DEFAULT_LINK_URL,
             link_text: str = DEFAULT_LINK_TEXT, task_id: str = None):
    """发送一封：建记录 -> 拼装链接行 -> 组装（含像素）-> 发送（失败自动重试 1 次，重试前重建连接）。
    返回 (ok: bool, error: Optional[str])
    注意：变量替换在调用方（前端提交前）完成，这里只做链接行拼装，且只拼装一次；
    重试时复用同一份组装好的邮件串，不会重复拼装。"""
    email_id = create_send_record(user_id, item, task_id)
    if not email_id:
        return False, "record_insert_failed"
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

MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024

def _check_inline_image_sizes(emails):
    """正文内嵌图护栏：单张 data 图不得超过 2MB（用户 2026-11-04 定版）。
    前端插入时已拦/已压，这里是服务端兜底，防止粘贴或旧模板绕过。"""
    for e in emails or []:
        for m in re.finditer(r"data:[^\"')]+", e.content or ""):
            if len(m.group(0)) * 3 // 4 > MAX_INLINE_IMAGE_BYTES:
                raise HTTPException(status_code=400, detail="正文里有一张图片超过 2MB，请删掉或换小一点的图再发")


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
                               link_text=req.link_text, task_id=task_id)
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
    _check_inline_image_sizes(req.emails)
    if not req.emails:
        raise HTTPException(status_code=400, detail="名单为空")
    if len(req.emails) > MAX_EMAILS_PER_TASK:
        raise HTTPException(status_code=400, detail=f"单批最多 {MAX_EMAILS_PER_TASK} 位收件人，请删减名单后重试")
    accounts = _load_user_accounts(user_id)
    if not accounts:
        raise HTTPException(status_code=400, detail="还没有发信账号，请先在账号区添加（云端保存）")
    _check_user_can_send(user_id, len(req.emails))
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


@app.get("/api/tasks/reply-counts")
async def task_reply_counts(authorization: Optional[str] = Header(default=None)):
    """每个任务的回复数（进度页任务行显示用）：{task_id: 条数}，只统计当前用户的。
    注：进度页的任务列表本身是前端直查 Supabase 的 send_tasks，没有后端列表接口，
    故回复数走这个独立小接口，前端按 task_id 合并进任务行。"""
    user_id = _verify_user_id(authorization)
    _ensure_replies()
    rows = _db_query(
        "SELECT task_id, COUNT(*) AS n FROM replies "
        "WHERE user_id = %s AND task_id IS NOT NULL GROUP BY task_id", (user_id,))
    return {"counts": {r["task_id"]: int(r["n"]) for r in rows}}


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
        "SELECT id, status, total, sent, failed, skipped, subject, created_at, payload, accounts "
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
    _check_user_can_send(user_id, len(remaining))
    req = BatchTaskRequest(
        emails=[EmailItem(**e) for e in remaining],
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


_HARD_BOUNCE_CODES = ("550", "551", "553")
_HARD_BOUNCE_WORDS = ("不存在", "user not found", "no such user", "address not found",
                      "recipient address rejected", "mailbox not found",
                      "invalid recipient", "does not exist")


def _is_hard_bounce_error(err) -> bool:
    """硬退信口径（再发一轮排除用）：SMTP 550/551/553，或文案明确说地址不存在。
    拿不准的一律 False——宁可保留在名单里，也不错杀一个还能联系的人。"""
    if not err:
        return False
    s = str(err)
    if any(code in s for code in _HARD_BOUNCE_CODES):
        return True
    low = s.lower()
    return any(w in low for w in _HARD_BOUNCE_WORDS)


def _extract_recipient_name(subject_template: str, rendered_subject: str) -> str:
    """从主题模板与某封已渲染主题的差异里反推 {{name}} 当时被替换成的值。
    快照里只存了渲染后的主题/正文，没存名单的名字列；主题模板里有 {{name}} 时
    可以这样把名字找回来，推不出就返回空串（前端名单里名字留空，用户可补）。"""
    if not subject_template or not rendered_subject:
        return ""
    m = re.search(r"\{\{\s*name\s*\}\}", subject_template, re.IGNORECASE)
    if not m:
        return ""
    pre, post = subject_template[:m.start()], subject_template[m.end():]
    if not rendered_subject.startswith(pre):
        return ""
    if post and not rendered_subject.endswith(post):
        return ""
    end = len(rendered_subject) - len(post) if post else len(rendered_subject)
    if end < len(pre):
        return ""
    return rendered_subject[len(pre):end].strip()


def _restore_content_template(subject_template: str, first_email: dict) -> str:
    """正文模板还原：快照里的正文是已渲染的（{{name}} 已被换成第一位收件人的名字），
    把推断出的名字换回 {{name}}，新一轮发送时前端会按每位收件人重新替换。
    推不出名字就原样返回（用户在发信页可再改）。"""
    content = first_email.get("content") or ""
    name = _extract_recipient_name(subject_template or "",
                                   first_email.get("subject") or "")
    if name and name in content:
        content = content.replace(name, "{{name}}")
    return content


@app.post("/api/tasks/{task_id}/clone-prefill")
async def clone_task_prefill(task_id: str, authorization: Optional[str] = Header(default=None)):
    """再发一轮（定版形态：克隆已完成任务做新一轮触达）：从原任务快照还原一份
    发信页预填数据，只还原、不发送，用户在发信页可改可不改、自己点开始。
    名单 = 原任务全部收件人 − 已回复的人（replies 表）− 原任务里硬退信的人。"""
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
    if not payload or not payload.get("emails"):
        raise HTTPException(status_code=400,
                            detail="这个任务是旧版本创建的，当时没有保存名单和正文，无法再发一轮；请重新准备名单发送")
    originals = payload["emails"]
    _ensure_replies()
    replied = {r["from_email"] for r in _db_query(
        "SELECT DISTINCT lower(from_email) AS from_email FROM replies WHERE user_id = %s",
        (user_id,))}
    hard = set()
    for r in _db_query(
            "SELECT lower(recipient) AS rcpt, error FROM emails "
            "WHERE task_id = %s AND user_id = %s AND status = 'failed'", (task_id, user_id)):
        if _is_hard_bounce_error(r.get("error")):
            hard.add(r["rcpt"])
    kept, n_replied, n_hard = [], 0, 0
    for e in originals:
        em = (e.get("to_email") or "").strip().lower()
        if em in replied:
            n_replied += 1
            continue
        if em in hard:
            n_hard += 1
            continue
        kept.append(e)
    subject_template = payload.get("subject_template") or (
        originals[0].get("subject") if originals else "")
    recipients = [{
        "email": e.get("to_email") or "",
        "name": _extract_recipient_name(subject_template, e.get("subject") or ""),
    } for e in kept]
    accounts = [a.strip() for a in (row.get("accounts") or "").split(",") if a.strip()]
    base_name = payload.get("batch_name") or ""
    return {
        "success": True,
        "accounts": accounts,
        "subject_template": subject_template,
        "content": _restore_content_template(subject_template, kept[0]) if kept else "",
        "batch_name": (base_name + "·再发") if base_name else "再发一轮",
        "recipients": recipients,
        "daily_cap": payload.get("daily_cap", 50),
        "interval_seconds": payload.get("interval_seconds", 0),
        "interval_max_seconds": payload.get("interval_max_seconds", 0),
        "link_enabled": payload.get("link_enabled", False),
        "link_url": payload.get("link_url", ""),
        "link_text": payload.get("link_text", ""),
        "excluded": {"replied": n_replied, "hard_bounce": n_hard},
        "original_total": len(originals),
        "kept_total": len(kept),
    }


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
# 回复列表接口（fix26）：数据来自回复巡逻写入的 replies 表，只读展示，
# 每行带好直达 Gmail 的链接，前端点了就跳过去由用户本人精聊。
# ==========================================
@app.get("/api/replies")
async def list_replies(limit: int = 100, authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    _ensure_replies()
    try:
        limit = max(1, min(int(limit or 100), 500))
    except Exception:
        limit = 100
    rows = _db_query(
        "SELECT id, account_email, from_email, from_name, subject, received_at, "
        "gmail_thrid, gmail_msgid, message_id, task_id, created_at FROM replies "
        "WHERE user_id = %s ORDER BY received_at DESC NULLS LAST, created_at DESC LIMIT %s",
        (user_id, limit))
    subjects = {}
    task_ids = list({r["task_id"] for r in rows if r.get("task_id")})
    if task_ids:
        for t in _db_query(
                "SELECT id, subject FROM send_tasks WHERE user_id = %s AND id = ANY(%s)",
                (user_id, task_ids)):
            subjects[t["id"]] = t["subject"]
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "account_email": r["account_email"],
            "from_email": r["from_email"],
            "from_name": r["from_name"] or "",
            "subject": r["subject"] or "",
            "received_at": _iso_or_none(r.get("received_at")),
            "task_id": r.get("task_id"),
            "task_subject": subjects.get(r.get("task_id")),
            "gmail_url": _gmail_reply_link(r["account_email"], r.get("gmail_thrid"),
                                           r.get("gmail_msgid"), r["from_email"],
                                           r.get("message_id")),
        })
    total_rows = _db_query("SELECT COUNT(*) AS n FROM replies WHERE user_id = %s", (user_id,))
    return {"replies": out, "total": int(total_rows[0]["n"]) if total_rows else 0}


# ==========================================
# 追踪大盘数据接口（fix30）：恢复接手前原版的三件套——打开流水
# （email_logs 逐条）、时间范围筛选、统计卡随范围走。范围日界一律按
# 北京时间（Asia/Shanghai）算，再换算成 UTC ISO 文本与 send_time 比较
# （send_time 存的是 UTC ISO 文本）。
# ==========================================
_EMAIL_LOGS_ENSURED = False


def _ensure_email_logs():
    """确保 email_logs 流水表存在（懒补建，沿 _ensure_replies 模式：
    启动自检万一漏建，用到的地方自己补，进程内只真正执行一次）。"""
    global _EMAIL_LOGS_ENSURED
    if _EMAIL_LOGS_ENSURED:
        return
    _db_exec("CREATE TABLE IF NOT EXISTS email_logs (log_id BIGSERIAL PRIMARY KEY, "
             "email_id TEXT, open_time TEXT, is_scanner BOOLEAN DEFAULT FALSE)")
    _db_exec("CREATE INDEX IF NOT EXISTS idx_email_logs_email ON email_logs (email_id)")
    _EMAIL_LOGS_ENSURED = True


_CST = ZoneInfo("Asia/Shanghai")


def _dashboard_bounds(time_range: str, custom_start: str, custom_end: str):
    """把范围参数换算成 (start_iso, end_iso) UTC ISO 文本，None 表示不限。
    end 为开区间上界，等价于止日 23:59:59 且不丢毫秒级时间。
    week/month/quarter 按自然日历周期算（fix31，用户拍板）：
    week=本周一 00:00 至下周一 00:00；month=本月 1 日至下月 1 日；
    quarter=本季度首月 1 日至下季度首月 1 日（均为北京时间）。
    自定义起止都有效时优先于 time_range；time_range 非法值按 all。"""
    def day_start_utc(d):
        return datetime(d.year, d.month, d.day, tzinfo=_CST) \
            .astimezone(timezone.utc).isoformat()

    def add_months(d, n):
        # d 恒为某月 1 日，只做年月平移
        total = d.year * 12 + (d.month - 1) + n
        return d.replace(year=total // 12, month=total % 12 + 1, day=1)

    cs = ce = None
    if custom_start and custom_end:
        try:
            cs = datetime.strptime(custom_start.strip(), "%Y-%m-%d").date()
            ce = datetime.strptime(custom_end.strip(), "%Y-%m-%d").date()
        except Exception:
            cs = ce = None
    if cs is not None and ce is not None:
        if ce < cs:
            cs, ce = ce, cs
        return day_start_utc(cs), day_start_utc(ce + timedelta(days=1))

    today = datetime.now(_CST).date()
    if time_range == "today":
        return day_start_utc(today), None
    if time_range == "yesterday":
        y = today - timedelta(days=1)
        return day_start_utc(y), day_start_utc(today)
    if time_range == "week":
        monday = today - timedelta(days=today.weekday())
        return day_start_utc(monday), day_start_utc(monday + timedelta(days=7))
    if time_range == "month":
        first = today.replace(day=1)
        return day_start_utc(first), day_start_utc(add_months(first, 1))
    if time_range == "quarter":
        qfirst = today.replace(month=3 * ((today.month - 1) // 3) + 1, day=1)
        return day_start_utc(qfirst), day_start_utc(add_months(qfirst, 3))
    return None, None


def _safe_err_text(e: Exception) -> str:
    """把异常压成可回前端的一行文本：带类型名，抹掉数据库连接串等敏感
    片段并限长。沿 fix24 admin 接口「错误透出」的思路——与其让前端只拿到
    一句纯文本 Internal Server Error，不如把真实原因亮出来便于定位。"""
    msg = f"{type(e).__name__}: {e}"
    if DATABASE_URL:
        msg = msg.replace(DATABASE_URL, "[db]")
    msg = re.sub(r"postgres(?:ql)?://\S+", "[db]", msg)
    msg = re.sub(r"(?i)(password|passwd|pwd)\s*=\s*\S+", r"\1=[hidden]", msg)
    return " ".join(msg.split())[:300]


@app.get("/api/dashboard_data")
async def dashboard_data(time_range: str = "all", custom_start: str = "",
                         custom_end: str = "",
                         authorization: Optional[str] = Header(default=None)):
    try:
        return _dashboard_data_impl(time_range, custom_start, custom_end,
                                    authorization)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"大盘数据查询出错：{_safe_err_text(e)}"})


def _dashboard_data_impl(time_range: str, custom_start: str, custom_end: str,
                         authorization: Optional[str]):
    user_id = _verify_user_id(authorization)
    start_iso, end_iso = _dashboard_bounds(time_range or "all",
                                           custom_start or "", custom_end or "")
    where = "user_id = %s"
    params = [user_id]
    if start_iso:
        where += " AND send_time >= %s"
        params.append(start_iso)
    if end_iso:
        where += " AND send_time < %s"
        params.append(end_iso)
    rows = _db_query(
        "SELECT id, recipient, subject, status, send_time, open_count, "
        "first_open_time, error FROM emails WHERE " + where +
        " ORDER BY send_time DESC", tuple(params))
    emails_out = [{
        "id": r["id"],
        "recipient": r.get("recipient"),
        "subject": r.get("subject"),
        "status": r.get("status"),
        "send_time": r.get("send_time"),
        "open_count": r.get("open_count") or 0,
        "first_open_time": r.get("first_open_time"),
        "error": r.get("error"),
    } for r in rows]
    logs_map = {}
    ids = [r["id"] for r in rows]
    if ids:
        _ensure_email_logs()
        for lg in _db_query(
                "SELECT log_id, email_id, open_time, is_scanner FROM email_logs "
                "WHERE email_id = ANY(%s) ORDER BY open_time ASC", (ids,)):
            logs_map.setdefault(lg["email_id"], []).append({
                "log_id": lg["log_id"],
                "open_time": lg["open_time"],
                "is_scanner": bool(lg.get("is_scanner")),
            })
    # 统计口径沿现大盘：sent = 范围内邮件行数（不分状态，与「累计发送数」
    # 一直以来的算法一致），opened = 其中 open_count>0 的封数。
    sent = len(rows)
    opened = sum(1 for r in rows if (r.get("open_count") or 0) > 0)
    rate = round(opened / sent * 100, 1) if sent else 0.0
    return {"emails": emails_out, "logs_map": logs_map,
            "stats": {"sent": sent, "opened": opened, "rate": rate}}


# ==========================================
# 发信账号接口（云端保存，授权码加密；前端永远看不到明文）
# ==========================================
class AccountIn(BaseModel):
    email: str
    password: str


@app.get("/api/version")
async def api_version():
    return {"v": "v5-fix34-homehero"}


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
    if not _is_owner(user_id):
        if _is_banned(user_id):
            raise HTTPException(status_code=403, detail="该账号已被停用，请联系站长")
        rows = _db_query("SELECT email FROM sender_accounts WHERE user_id = %s", (user_id,))
        existing = {r["email"] for r in rows}
        if email not in existing and len(existing) >= NONOWNER_MAX_ACCOUNTS:
            raise HTTPException(status_code=400, detail=f"发信账号已达上限：每位用户最多绑 {NONOWNER_MAX_ACCOUNTS} 个发信账号")
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
# 站长管理：限额自查 / 注册名额 / 监控室（fix23）
# ==========================================
def _iso_or_none(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


@app.get("/api/me")
async def api_me(authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    info = {"email": _USER_EMAILS.get(user_id) or "", "is_owner": _is_owner(user_id)}
    if not info["is_owner"]:
        try:
            rows = _db_query("SELECT COUNT(*) AS n FROM sender_accounts WHERE user_id = %s", (user_id,))
            info["limits"] = {
                "accounts_used": int(rows[0]["n"]) if rows else 0,
                "accounts_cap": NONOWNER_MAX_ACCOUNTS,
                "sent_today": _sent_today_count(user_id),
                "daily_cap": NONOWNER_DAILY_CAP,
            }
        except Exception as e:
            print(f"api_me limits failed: {e}")
    return info


@app.get("/api/registration-status")
async def registration_status():
    """注册名额：注册表里非站长用户数 < REGISTRATION_CAP 才开放。查不到库时默认开放，
    别因为计数故障把正常注册堵死（发送侧另有限额硬闸）。"""
    try:
        _ensure_registry()
        rows = _db_query("SELECT email FROM user_registry")
        used = sum(1 for r in rows if (r.get("email") or "").lower() not in OWNER_EMAILS)
    except Exception:
        used = 0
    return {"open": used < REGISTRATION_CAP, "used": used, "cap": REGISTRATION_CAP}


def _require_owner(user_id: str):
    if not _is_owner(user_id):
        raise HTTPException(status_code=403, detail="只有站长能进监控室")


@app.get("/api/admin/overview")
async def admin_overview(authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    _require_owner(user_id)
    try:
        return await _admin_overview_impl()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"监控室概览查询出错：{type(e).__name__}: {e}")


async def _admin_overview_impl():
    _ensure_registry()
    start = _today_start_utc_iso()

    def _count(sql, params=()):
        rows = _db_query(sql, params)
        return int(rows[0]["n"]) if rows else 0

    reg_rows = _db_query("SELECT email, banned FROM user_registry")
    return {
        "today_sent": _count("SELECT COUNT(*) AS n FROM emails WHERE status = 'sent' AND send_time >= %s", (start,)),
        "today_failed": _count("SELECT COUNT(*) AS n FROM emails WHERE status = 'failed' AND send_time >= %s", (start,)),
        "today_opened": _count("SELECT COUNT(*) AS n FROM emails WHERE open_count > 0 AND send_time >= %s", (start,)),
        "total_sent": _count("SELECT COUNT(*) AS n FROM emails WHERE status = 'sent'"),
        "running_tasks": sum(1 for t in TASKS.values() if t.get("status") in ("running", "paused")),
        "users_total": len(reg_rows),
        "users_banned": sum(1 for r in reg_rows if r.get("banned")),
        "accounts_total": _count("SELECT COUNT(*) AS n FROM sender_accounts"),
    }


@app.get("/api/admin/users")
async def admin_users(authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    _require_owner(user_id)
    try:
        return await _admin_users_impl()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"监控室用户列表查询出错：{type(e).__name__}: {e}")


async def _admin_users_impl():
    _ensure_registry()
    start = _today_start_utc_iso()
    first_rows = _db_query("SELECT user_id FROM sender_accounts ORDER BY created_at LIMIT 1")
    first_uid = first_rows[0]["user_id"] if first_rows else None
    # 注意：sender_accounts/emails 的 user_id 是 uuid 型，本表的 user_id 是 text，
    # 两列直接相比 Postgres 报 uuid = text 无此运算符（fix24 实测）——一律把 uuid 列转 text 再比
    rows = _db_query(
        "SELECT r.user_id, r.email, r.first_seen, r.banned, "
        "(SELECT COUNT(*) FROM sender_accounts a WHERE a.user_id::text = r.user_id) AS accounts, "
        "(SELECT COUNT(*) FROM emails e WHERE e.user_id::text = r.user_id AND e.status = 'sent') AS sent_total, "
        "(SELECT COUNT(*) FROM emails e WHERE e.user_id::text = r.user_id AND e.status = 'sent' AND e.send_time >= %s) AS sent_today, "
        "(SELECT MAX(e.send_time) FROM emails e WHERE e.user_id::text = r.user_id) AS last_sent "
        "FROM user_registry r ORDER BY r.first_seen", (start,))
    out = []
    for r in rows:
        out.append({
            "user_id": r["user_id"],
            "email": r.get("email") or "",
            "is_owner": ((r.get("email") or "").lower() in OWNER_EMAILS) or r["user_id"] == first_uid,
            "banned": bool(r.get("banned")),
            "accounts": int(r.get("accounts") or 0),
            "sent_total": int(r.get("sent_total") or 0),
            "sent_today": int(r.get("sent_today") or 0),
            "first_seen": _iso_or_none(r.get("first_seen")),
            "last_sent": r.get("last_sent"),
        })
    return {"users": out}


@app.post("/api/admin/users/{target_id}/ban")
async def admin_ban_user(target_id: str, authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    _require_owner(user_id)
    if target_id == user_id or _is_owner(target_id):
        raise HTTPException(status_code=400, detail="站长账号不能封禁")
    _db_exec("UPDATE user_registry SET banned = true WHERE user_id = %s", (target_id,))
    stopped = 0
    for t in TASKS.values():
        if t.get("user_id") == target_id and t.get("status") in ("running", "paused"):
            t["status"] = "stopped"
            stopped += 1
    return {"success": True, "stopped_tasks": stopped}


@app.post("/api/admin/users/{target_id}/unban")
async def admin_unban_user(target_id: str, authorization: Optional[str] = Header(default=None)):
    user_id = _verify_user_id(authorization)
    _require_owner(user_id)
    _db_exec("UPDATE user_registry SET banned = false WHERE user_id = %s", (target_id,))
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


@app.get("/replies", response_class=HTMLResponse)
async def replies_page(request: Request):
    """回复页：巡逻发现的回信汇总，点一条直达 Gmail 原信。"""
    return _page_with_sender_cookie("replies.html", request)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """站长监控室页面壳：数据全走 /api/admin/* 接口，接口侧验站长身份，
    非站长打开只会看到无权限提示，拿不到任何全站数据。"""
    return _serve_page_file("admin.html")


@app.get("/preview-home", response_class=HTMLResponse)
async def preview_home_page(request: Request):
    """新版首页设计稿预览（站长确认后再替换正式首页）。"""
    return _serve_page_file("index_new.html")

def _parse_iso_dt(v):
    """把存的 ISO 时间文本解析成带时区的 datetime；解析不了返回 None。
    无时区的按 UTC 看待（send_time 一律是 UTC ISO 文本）。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ==========================================
# 追踪像素端点（fix30 恢复原版防误判 + 打开流水）
# ------------------------------------------
# 判定顺序：发件人设备 Cookie 免记 → 发送后 30 秒内硬丢弃（邮件系统
# 投递即扫的那一下，不写流水不计数）→ UA 含 GoogleImageProxy 判系统
# 扫描（只写 is_scanner 流水，不计数不算已读）→ 真人打开（写流水 +
# open_count+1，首开时间只记第一次）。流水写失败不拖累计数，反之亦然；
# 任何异常都照常返回像素，绝不让收件端看到报错。
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
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()
            # 发送时间单独查一列：主查询保持老形状（冒烟测试的假库按它匹配）
            send_dt = None
            try:
                srows = _db_query(
                    "SELECT send_time FROM emails WHERE id = %s", (email_id,))
                if srows:
                    send_dt = _parse_iso_dt(srows[0].get("send_time"))
            except Exception:
                send_dt = None
            # 30 秒规则：解析不出发送时间就不启用，照常往下记
            if send_dt is not None and (now - send_dt).total_seconds() < 30:
                return _pixel_response()
            ua = request.headers.get("user-agent") or ""
            if "GoogleImageProxy" in ua:
                try:
                    _ensure_email_logs()
                    _db_exec(
                        "INSERT INTO email_logs (email_id, open_time, is_scanner) "
                        "VALUES (%s, %s, %s)", (email_id, now_iso, True))
                except Exception as e:
                    print(f"Tracking scanner-log error for {email_id}: {e}")
                return _pixel_response()
            try:
                _ensure_email_logs()
                _db_exec(
                    "INSERT INTO email_logs (email_id, open_time, is_scanner) "
                    "VALUES (%s, %s, %s)", (email_id, now_iso, False))
            except Exception as e:
                print(f"Tracking log error for {email_id}: {e}")
            new_count = (rows[0].get("open_count") or 0) + 1
            first_open = rows[0].get("first_open_time") or now_iso
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
    _check_user_can_send(req.user_id, len(req.emails))
    _check_inline_image_sizes(req.emails)
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
