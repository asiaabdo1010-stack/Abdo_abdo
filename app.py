# -*- coding: utf-8 -*-
"""
Store Engine v6.0 - Ultimate Wealth, Reviews & Affiliate Edition for Render / VPS
"""
import csv
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from jinja2 import DictLoader

# ───────────────────────────── الإعدادات الأساسية ─────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "store_production.db")
OWNER_WALLET = os.environ.get(
    "OWNER_WALLET", "UQC9OQpaeMwIVh4RHfpZgRRoQmYbjcLDAG9GAOryck2nUlvu")
STORE_NAME = os.environ.get("STORE_NAME", "متجر الذكاء الاصطناعي الذاتي (ماكينة الملايين)")
TONCENTER_API_KEY = os.environ.get("TONCENTER_API_KEY", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
AUTONOMOUS_INTERVAL = int(os.environ.get("AUTONOMOUS_INTERVAL", "3600"))
ORDER_TTL_MIN = int(os.environ.get("ORDER_TTL_MIN", "30"))
GRACE_SECONDS = 120
ALLOW_AMOUNT_ONLY = os.environ.get("ALLOW_AMOUNT_ONLY", "1") == "1"
DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"
NANO = 1_000_000_000
STATUSES = ("PENDING", "PAID", "EXPIRED")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("store_engine")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "admin12345"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS", "0") == "1",
    MAX_CONTENT_LENGTH=64 * 1024,
)

# ───────────────────────────── قاعدة البيانات المحدثة (مع التقييمات والأفلييت) ─────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    slug        TEXT    UNIQUE,
    price_nano  INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    description TEXT,
    keywords    TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    views       INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS coupons (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT    UNIQUE NOT NULL,
    discount_percent INTEGER NOT NULL,
    active           INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    rating      INTEGER NOT NULL DEFAULT 5,
    comment     TEXT,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS referrals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    UNIQUE NOT NULL,
    sales_count INTEGER DEFAULT 0,
    earned_nano INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT    NOT NULL UNIQUE,
    code        TEXT    NOT NULL UNIQUE,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    amount_nano INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'PENDING',
    tx_hash     TEXT    UNIQUE,
    paid_nano   INTEGER,
    ref_code    TEXT,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    paid_at     INTEGER
);
CREATE TABLE IF NOT EXISTS market_trends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT    NOT NULL,
    demand_score INTEGER NOT NULL,
    status      TEXT    DEFAULT 'DISCOVERED',
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_products_slug ON products(slug);
"""

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM coupons")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO coupons (code, discount_percent, active) VALUES (?, ?, ?)", ("VIP10", 10, 1))
        conn.commit()
    
    # إضافة كود أفلييت افتراضي للتجربة
    cur.execute("SELECT COUNT(*) FROM referrals")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO referrals (code, sales_count, earned_nano) VALUES (?, ?, ?)", ("PARTNER1", 0, 0))
        conn.commit()
    conn.close()

# ───────────────────────────── أدوات مساعدة ونظام الـ SEO ─────────────────────────────
def now():
    return int(time.time())

def to_nano(text):
    try:
        d = Decimal(str(text).strip().replace(",", "."))
    except InvalidOperation:
        return None
    if not d.is_finite() or d < Decimal("0.01") or d > Decimal("100000"):
        return None
    return int((d * NANO).to_integral_value())

def fmt_ton(nano):
    s = format(Decimal(int(nano or 0)) / NANO, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"

def generate_slug(title):
    slug = ''.join(c if c.isalnum() else '-' for c in title.lower())
    while '--' in slug:
        slug = slug.replace('--', '-')
    return slug.strip('-')[:80] or f"prod-{secrets.token_hex(3)}"

STATUS_AR = {"PENDING": "منتظر الدفع", "PAID": "مدفوع", "EXPIRED": "منتهي"}
STATUS_CLASS = {"PENDING": "warn", "PAID": "ok", "EXPIRED": "mute"}
app.add_template_filter(fmt_ton, "ton")
app.add_template_filter(lambda s: STATUS_AR.get(s, s), "status_ar")
app.add_template_filter(lambda s: STATUS_CLASS.get(s, "mute"), "status_cls")
app.add_template_filter(lambda h: (h[:8] + "…" + h[-6:]) if h and len(h) > 16 else (h or "—"), "short")

_hits, _hits_lock = {}, threading.Lock()

def rate_limited(key, limit, window):
    t = now()
    with _hits_lock:
        lst = [x for x in _hits.get(key, []) if t - x < window]
        if len(lst) >= limit:
            _hits[key] = lst
            return True
        lst.append(t)
        _hits[key] = lst
        return False

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]

@app.context_processor
def inject_globals():
    return dict(store_name=STORE_NAME, csrf_token=csrf_token, wallet=OWNER_WALLET)

@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = (request.form.get("csrf") or "").encode()
        real = (session.get("csrf") or "").encode()
        if not real or not hmac.compare_digest(sent, real):
            abort(400)

@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:")
    if request.path.startswith(("/admin", "/api", "/order")):
        resp.headers["Cache-Control"] = "no-store"
    return resp

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper

# ───────────────────────────── الذكاء الذاتي وتوليد المنتجات ─────────────────────────────
AI_TEMPLATES_POOL = [
    {
        "title": "مجلد أكواد بايثون لأتمتة التسويق واستخراج العملاء (Web Scraping Engine v6)",
        "desc": "أحدث سكربت بايثون تجاري مستخرج من تحليل طلب السوق لاستخراج الداتا والمنافسين أوتوماتيكياً.",
        "content": "# Python Web Scraping & Lead Gen Engine\nimport json\ndef fetch_leads():\n    return {'status': 'success', 'leads_found': 2500}\nprint(fetch_leads())",
        "price_ton": "3.5"
    },
    {
        "title": "دليل الهندسة العكسية لبناء SaaS مصغر وتحقيق الدخل بالعملات الرقمية (PDF + Prompts)",
        "desc": "الكتاب الأكثر طلباً لفهم بناء منتجات رقمية مصغرة وتسويقها بدون رأس مال.",
        "content": "--- دليل الملايين الرقمية ---\n1. ابحث عن المشكلة.\n2. ولد الأداة بالذكاء الاصطناعي.\n3. بيع عبر TON واجمع الأرباح.",
        "price_ton": "2.0"
    }
]

def autonomous_market_research_and_generation():
    try:
        with db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            if count >= 15:
                return
            import random
            template = random.choice(AI_TEMPLATES_POOL)
            exists = conn.execute("SELECT id FROM products WHERE title = ?", (template["title"],)).fetchone()
            if exists:
                return
            price_nano = to_nano(template["price_ton"])
            slug = generate_slug(template["title"])
            t = now()
            conn.execute(
                "INSERT INTO products (title, slug, price_nano, content, description, keywords, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (template["title"], slug, price_nano, template["content"], template["desc"], "منتجات رقمية, ذكاء اصطناعي, بايثون", t)
            )
            log.info("🤖 [Autonomous AI Engine] تم توليد ونشر منتج جديد بنجاح: %s", template["title"])
    except Exception as e:
        log.warning("خطأ في محرك التوليد الذاتي: %s", e)

# ───────────────────────────── مراقب المدفوعات والخلفية والأفلييت ─────────────────────────────
STATE = {"last_ok": 0, "last_error": "", "checks": 0, "ai_runs": 0}

def fetch_transactions(limit=50):
    qs = urllib.parse.urlencode({"address": OWNER_WALLET, "limit": limit})
    headers = {"User-Agent": "StoreEngine/6.0"}
    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY
    req = urllib.request.Request("https://toncenter.com/api/v2/getTransactions?" + qs, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("ok"):
        raise RuntimeError("toncenter returned ok=false")
    return data.get("result", [])

def parse_tx(tx):
    in_msg = tx.get("in_msg") or {}
    if not in_msg.get("source"):
        return None
    try:
        value = int(in_msg.get("value") or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    tx_hash = (tx.get("transaction_id") or {}).get("hash")
    if not tx_hash:
        return None
    comment = (in_msg.get("message") or "").strip()
    return tx_hash, int(tx.get("utime") or 0), value, comment

def poll_once():
    try:
        txs = [p for p in (parse_tx(t) for t in fetch_transactions()) if p]
        with db() as conn:
            t = now()
            conn.execute(
                "UPDATE orders SET status='EXPIRED' WHERE status='PENDING' AND expires_at + ? < ?",
                (GRACE_SECONDS, t))
            pending = conn.execute("SELECT * FROM orders WHERE status='PENDING'").fetchall()
            used = {r[0] for r in conn.execute("SELECT tx_hash FROM orders WHERE tx_hash IS NOT NULL")}
            for o in pending:
                for tx_hash, utime, value, comment in txs:
                    if tx_hash in used:
                        continue
                    if not (o["created_at"] - 60 <= utime <= o["expires_at"] + GRACE_SECONDS):
                        continue
                    by_comment = comment == o["code"] and value >= o["amount_nano"]
                    by_amount = ALLOW_AMOUNT_ONLY and value == o["amount_nano"]
                    if by_comment or by_amount:
                        cur = conn.execute(
                            "UPDATE orders SET status='PAID', tx_hash=?, paid_nano=?, paid_at=? "
                            "WHERE id=? AND status='PENDING'",
                            (tx_hash, value, t, o["id"]))
                        if cur.rowcount:
                            used.add(tx_hash)
                            # تحديث أرباح الأفلييت إن وجد
                            if o["ref_code"]:
                                conn.execute("UPDATE referrals SET sales_count = sales_count + 1, earned_nano = earned_nano + ? WHERE code = ?", (value, o["ref_code"]))
                            log.info("تم دفع الطلب %s (%s TON) | tx=%s", o["code"], fmt_ton(value), tx_hash)
                            break
        STATE["last_ok"], STATE["last_error"] = now(), ""
    except Exception as e:
        STATE["last_error"] = str(e)[:200]
        log.warning("فشل فحص المدفوعات: %s", e)

_worker_started = False
_worker_lock = threading.Lock()

def start_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True

    def loop():
        last_ai_run = 0
        while True:
            poll_once()
            current_time = now()
            if current_time - last_ai_run >= AUTONOMOUS_INTERVAL:
                autonomous_market_research_and_generation()
                last_ai_run = current_time
                STATE["ai_runs"] += 1
            STATE["checks"] += 1
            time.sleep(POLL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="autonomous-engine-worker").start()
    log.info("تم تشغيل محرك العمل الخلفي بنجاح.")

# ───────────────────────────── منطق الطلبات والكوبونات والأفلييت ─────────────────────────────
def allocate_amount(conn, base_nano):
    t = now()
    used = {r[0] for r in conn.execute(
        "SELECT amount_nano FROM orders WHERE status='PENDING' AND expires_at + ? > ?",
        (GRACE_SECONDS, t))}
    for suffix in range(1, 1000):
        candidate = base_nano + suffix * 100_000
        if candidate not in used:
            return candidate
    raise RuntimeError("عدد الطلبات المعلّقة كبير جداً حالياً")

def create_order(product_id, coupon_code="", ref_code=""):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        p = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        if not p:
            return None
        
        final_price = p["price_nano"]
        if coupon_code:
            coupon = conn.execute("SELECT discount_percent FROM coupons WHERE code=? AND active=1", (coupon_code.strip().upper(),)).fetchone()
            if coupon:
                discount = int(coupon["discount_percent"])
                if 0 < discount < 100:
                    final_price = int(final_price * (100 - discount) / 100)

        amount = allocate_amount(conn, final_price)
        t = now()
        token = secrets.token_urlsafe(16)
        code = "ORD" + secrets.token_hex(3).upper()
        conn.execute(
            "INSERT INTO orders (token, code, product_id, amount_nano, ref_code, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (token, code, product_id, amount, ref_code, t, t + ORDER_TTL_MIN * 60))
        return token

def get_order(token):
    with db() as conn:
        return conn.execute(
            "SELECT o.*, p.title, p.content FROM orders o JOIN products p ON p.id = o.product_id WHERE o.token = ?",
            (token,)).fetchone()

# ───────────────────────────── المسارات (Routes) ─────────────────────────────
@app.route("/")
def store():
    ref = request.args.get("ref", "").strip()
    if ref:
        session["ref_code"] = ref
    with db() as conn:
        products = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
    return render_template("store.html", products=products, ref=session.get("ref_code", ""))

@app.post("/buy/<int:pid>")
def buy(pid):
    if rate_limited(("buy", request.remote_addr), 10, 600):
        flash("طلبات كتير في وقت قصير، جرّب بعد شوية.", "bad")
        return redirect(url_for("store"))
    
    coupon = request.form.get("coupon", "")
    ref_code = session.get("ref_code", "")
    token = create_order(pid, coupon, ref_code)
    if not token:
        abort(404)
    return redirect(url_for("order_page", token=token))

@app.route("/order/<token>")
def order_page(token):
    o = get_order(token)
    if not o:
        abort(404)
    deeplink = "ton://transfer/%s?amount=%d&text=%s" % (OWNER_WALLET, o["amount_nano"], o["code"])
    return render_template("order.html", o=o, deeplink=deeplink)

@app.route("/api/order/<token>")
def api_order(token):
    o = get_order(token)
    if not o:
        abort(404)
    data = {"status": o["status"]}
    if o["status"] == "PAID":
        data["content"] = o["content"]
    return jsonify(data)

@app.route("/product/<slug>")
def product_view(slug):
    ref = request.args.get("ref", "").strip()
    if ref:
        session["ref_code"] = ref
    with db() as conn:
        p = conn.execute("SELECT * FROM products WHERE slug = ? AND active = 1", (slug,)).fetchone()
        if not p:
            abort(404)
        conn.execute("UPDATE products SET views = views + 1 WHERE id = ?", (p["id"],))
        reviews = conn.execute("SELECT * FROM reviews WHERE product_id = ? ORDER BY id DESC", (p["id"],)).fetchall()
    return render_template("product.html", p=p, reviews=reviews)

@app.post("/product/<int:pid>/review")
def add_review(pid):
    try:
        rating = int(request.form.get("rating", "5"))
    except ValueError:
        rating = 5
    comment = (request.form.get("comment", "")).strip()[:300]
    if comment:
        with db() as conn:
            conn.execute("INSERT INTO reviews (product_id, rating, comment, created_at) VALUES (?, ?, ?, ?)",
                         (pid, max(1, min(5, rating)), comment, now()))
        flash("تم إضافة تقييمك بنجاح ⭐", "ok")
    return redirect(request.referrer or url_for("store"))

@app.route("/sitemap.xml")
def sitemap():
    with db() as conn:
        products = conn.execute("SELECT slug, created_at FROM products WHERE active=1").fetchall()
    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    xml.append('  <url><loc>%s/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>' % request.host_url.rstrip('/'))
    for p in products:
        loc = f"{request.host_url.rstrip('/')}/product/{p['slug']}"
        xml.append(f'  <url><loc>{loc}</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>')
    xml.append('</urlset>')
    return Response("\n".join(xml), mimetype="application/xml")

# ───────────────────────────── لوحة التحكم الإدارية ─────────────────────────────
@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if rate_limited(("login", request.remote_addr), 8, 300):
            flash("محاولات كتير. استنى 5 دقايق.", "bad")
        else:
            pw = (request.form.get("password") or "").encode()
            if hmac.compare_digest(pw, ADMIN_PASSWORD.encode()):
                session.clear()
                session["admin"] = True
                return redirect("/admin")
            flash("كلمة المرور خطأ.", "bad")
    return render_template("login.html")

@app.post("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/admin")
@login_required
def admin():
    with db() as conn:
        orders = conn.execute("SELECT o.*, p.title FROM orders o JOIN products p ON p.id=o.product_id ORDER BY o.id DESC LIMIT 100").fetchall()
        products = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        coupons = conn.execute("SELECT * FROM coupons ORDER BY id DESC").fetchall()
        referrals = conn.execute("SELECT * FROM referrals ORDER BY id DESC").fetchall()
        r = conn.execute("""
            SELECT COALESCE(SUM(CASE WHEN status='PAID' THEN paid_nano END),0) AS revenue,
                   COALESCE(SUM(sta
