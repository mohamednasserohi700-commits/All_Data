from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
import re
import webbrowser
import time
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'erp_super_secret_key_2024_!@#$')

DATABASE = os.path.join(os.path.dirname(__file__), 'instance', 'erp.db')

# ─── PostgreSQL (Railway) ────────────────────────────────────────────────────
# لو متغير DATABASE_URL موجود → النظام بيشتغل على PostgreSQL (دايم/مضمون الحفظ).
# لو مش موجود → بيشتغل على SQLite المحلي زي ما كان (instance/erp.db).
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_PG = DATABASE_URL.startswith(('postgres://', 'postgresql://'))
APP_TZ = os.environ.get('APP_TIMEZONE', 'Africa/Cairo')

def _now():
    """الوقت الحالي — على Railway (UTC) بنحوّله لتوقيت APP_TIMEZONE عشان التواريخ تفضل زي ما كانت."""
    if USE_PG:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(APP_TZ))
        except Exception:
            pass
    return datetime.now()
# ─────────────────────────────────────────────────────────────────────

url = "http://11.82.134.189:5050"

def get_lan_ip():
    """يرجع عنوان IP الخاص بالجهاز على الشبكة المحلية (عشان نعرضه بالكونسول)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

# ─── Database ────────────────────────────────────────────────────────────────

_PG_NOW_SQL = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
_PG_POOL = None

def _pg_sql(sql, has_params):
    """يحوّل صياغة SQLite المستخدمة في باقي الكود (؟ / LIKE / datetime('now')) لصياغة PostgreSQL."""
    if has_params:
        sql = sql.replace('%', '%%')
    sql = sql.replace("datetime('now')", _PG_NOW_SQL)
    sql = re.sub(r'\bLIKE\b', 'ILIKE', sql, flags=re.IGNORECASE)  # SQLite LIKE مش حساس لحالة الحروف
    return sql.replace('?', '%s')

class _PgCursor:
    """غلاف لـ cursor الخاص بـ psycopg2 بنفس واجهة sqlite3 (execute / executemany / fetch*)."""
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=None):
        self._cur.execute(_pg_sql(sql, params is not None), params)
        return self

    def executemany(self, sql, seq):
        from psycopg2.extras import execute_batch
        execute_batch(self._cur, _pg_sql(sql, True), list(seq), page_size=500)
        return self

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)

class _PgConn:
    """غلاف لاتصال psycopg2 بنفس واجهة sqlite3 (execute / cursor / commit / rollback / close)."""
    def __init__(self, raw, pool):
        self._raw = raw
        self._pool = pool

    def cursor(self):
        from psycopg2.extras import RealDictCursor
        return _PgCursor(self._raw.cursor(cursor_factory=RealDictCursor))

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, seq):
        return self.cursor().executemany(sql, seq)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        raw, self._raw = self._raw, None
        if raw is not None:
            try:
                self._pool.putconn(raw, close=bool(raw.closed))
            except Exception:
                pass

    def __del__(self):  # أمان: لو route نسي يقفل الاتصال يرجع للـ pool تلقائياً
        self.close()

def _pg_pool():
    global _PG_POOL
    if _PG_POOL is None:
        from psycopg2.pool import ThreadedConnectionPool
        _PG_POOL = ThreadedConnectionPool(
            1, int(os.environ.get('DB_POOL_MAX', 10)), DATABASE_URL,
            connect_timeout=10, keepalives=1, keepalives_idle=30,
            keepalives_interval=10, keepalives_count=5)
    return _PG_POOL

def _safe_int(v):
    """أول رقم صحيح في النص (بيرجع None لو مفيش) — نفس سلوك CAST(... AS INTEGER) في SQLite، من غير أخطاء."""
    m = re.match(r'\s*([-+]?\d+)', str(v)) if v is not None else None
    return int(m.group(1)) if m and len(m.group(1).lstrip('+-')) <= 18 else None

def get_db():
    if USE_PG:
        pool = _pg_pool()
        for attempt in range(2):
            raw = pool.getconn()
            try:
                with raw.cursor() as cur:   # فحص إن الاتصال لسه شغال (ممكن يكون اتقفل من السيرفر)
                    cur.execute('SELECT 1')
                raw.rollback()
                return _PgConn(raw, pool)
            except Exception:
                pool.putconn(raw, close=True)
                if attempt == 1:
                    raise
    conn = sqlite3.connect(DATABASE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function('safe_int', 1, _safe_int)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def init_db():
    if USE_PG:
        _init_db_postgres()
    else:
        _init_db_sqlite()

def _init_db_postgres():
    """إنشاء/تحديث جداول PostgreSQL (آمن للتكرار — IF NOT EXISTS)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT pg_advisory_lock(727001)")  # لو أكتر من worker بدأوا مع بعض
    try:
        N = "DEFAULT " + _PG_NOW_SQL
        c.execute(f'''CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            secondary_password TEXT NOT NULL
        )''')
        c.execute(f'''CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            gpit TEXT,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            location TEXT,
            national_id TEXT,
            security_q1 TEXT,
            security_q2 TEXT,
            phone TEXT,
            hire_date TEXT,
            user_type TEXT DEFAULT 'october',
            created_at TEXT {N}
        )''')
        c.execute("UPDATE users SET user_type = 'october' WHERE user_type IS NULL")
        c.execute(f'''CREATE TABLE IF NOT EXISTS sap_transactions (
            id SERIAL PRIMARY KEY,
            transaction_name TEXT NOT NULL,
            transaction_code TEXT NOT NULL,
            movement TEXT,
            created_at TEXT {N}
        )''')
        c.execute(f'''CREATE TABLE IF NOT EXISTS other_users (
            id SERIAL PRIMARY KEY,
            gpit TEXT,
            username TEXT NOT NULL,
            location TEXT,
            created_at TEXT {N}
        )''')
        c.execute(f'''CREATE TABLE IF NOT EXISTS cold_books (
            id SERIAL PRIMARY KEY,
            book_name TEXT NOT NULL,
            seq_from TEXT,
            seq_to TEXT,
            pages_per_book INTEGER DEFAULT 50,
            last_purchase_date TEXT,
            quantity INTEGER,
            min_quantity INTEGER DEFAULT 5,
            created_at TEXT {N}
        )''')
        # ملحوظة: بدون FOREIGN KEY عن قصد — نفس سلوك SQLite (حذف دفتر مش بيمسح سجلات الصرف بتاعته)
        c.execute('''CREATE TABLE IF NOT EXISTS book_dispense (
            id SERIAL PRIMARY KEY,
            book_id INTEGER NOT NULL,
            book_index INTEGER DEFAULT 1,
            serial_from BIGINT DEFAULT 0,
            serial_to BIGINT DEFAULT 0,
            serial_number TEXT,
            location TEXT,
            dispensed_at TEXT
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_bd_book_id ON book_dispense(book_id)')
        c.execute(f'''CREATE TABLE IF NOT EXISTS weight_diff_receipts (
            id SERIAL PRIMARY KEY,
            seq_no TEXT,
            receipt_date TEXT,
            car_number TEXT,
            permit_number TEXT,
            category TEXT,
            entity TEXT,
            dispense_room TEXT,
            receipt_number TEXT NOT NULL,
            source_sheet TEXT,
            product_type TEXT NOT NULL DEFAULT 'potato',
            imported_at TEXT {N}
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_wdr_receipt_number ON weight_diff_receipts(receipt_number)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_wdr_product_type ON weight_diff_receipts(product_type)')
        c.execute(f'''CREATE TABLE IF NOT EXISTS duplicate_receipts_dismissed (
            id SERIAL PRIMARY KEY,
            year TEXT NOT NULL,
            receipt_number TEXT NOT NULL,
            reason TEXT,
            dismissed_by TEXT,
            dismissed_at TEXT {N},
            UNIQUE(year, receipt_number)
        )''')
        c.execute(f'''CREATE TABLE IF NOT EXISTS page_visits (
            id SERIAL PRIMARY KEY,
            page TEXT,
            username TEXT,
            ip_address TEXT,
            search_query TEXT,
            visited_at TEXT {N}
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_pv_page ON page_visits(page)')

        # بديل CAST(... AS INTEGER) المتساهل في SQLite: بيقرا أول رقم في النص (مثلاً '30639/30641' ← 30639)
        # وبيرجع NULL بدل ما يرمي خطأ لو مفيش رقم
        c.execute('''CREATE OR REPLACE FUNCTION safe_int(t text) RETURNS bigint AS $$
            SELECT CASE WHEN btrim(t) ~ '^[-+]{0,1}[0-9]{1,18}([^0-9]|$)'
                        THEN substring(btrim(t) from '^[-+]{0,1}[0-9]+')::bigint END
        $$ LANGUAGE sql IMMUTABLE''')
        # index على الرقم المستخرج — بيخلّي صفحة الدفاتر الفارغة/المستخدمة (اللي بتعد الايصالات لكل دفتر) سريعة
        c.execute('CREATE INDEX IF NOT EXISTS idx_wdr_receipt_int ON weight_diff_receipts (safe_int(receipt_number))')

        existing = c.execute("SELECT id FROM admins WHERE username='admin'").fetchone()
        if not existing:
            c.execute("INSERT INTO admins (username, password, secondary_password) VALUES (?, ?, ?)",
                      ('admin',
                       generate_password_hash('1907*'),
                       generate_password_hash('1907*')))
        conn.commit()
    finally:
        try:
            conn.rollback()  # لو حصل خطأ في النص، لازم نخرج من الـ transaction الفاشلة قبل فك القفل
            c.execute("SELECT pg_advisory_unlock(727001)")
            conn.commit()
        except Exception:
            pass
        conn.close()

def _init_db_sqlite():
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    conn = get_db()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        secondary_password TEXT NOT NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gpit TEXT,
        username TEXT NOT NULL,
        password TEXT NOT NULL,
        location TEXT,
        national_id TEXT,
        security_q1 TEXT,
        phone TEXT,
        hire_date TEXT,
        user_type TEXT DEFAULT "october",
        created_at TEXT DEFAULT (datetime('now'))
    )''')
    # Migrate existing DBs
    try:
        c.execute('ALTER TABLE users ADD COLUMN phone TEXT')
    except Exception:
        pass
    try:
        c.execute('ALTER TABLE users ADD COLUMN user_type TEXT DEFAULT "october"')
    except Exception:
        pass
    c.execute('UPDATE users SET user_type = "october" WHERE user_type IS NULL')

    c.execute('''CREATE TABLE IF NOT EXISTS sap_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_name TEXT NOT NULL,
        transaction_code TEXT NOT NULL,
        movement TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS other_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gpit TEXT,
        username TEXT NOT NULL,
        location TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS cold_books (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_name TEXT NOT NULL,
        seq_from INTEGER,
        pages_per_book INTEGER DEFAULT 50,
        last_purchase_date TEXT,
        quantity INTEGER,
        min_quantity INTEGER DEFAULT 5,
        created_at TEXT DEFAULT (datetime('now'))
    )''')
    # Migrate existing DBs
    try:
        c.execute('ALTER TABLE cold_books ADD COLUMN min_quantity INTEGER DEFAULT 5')
    except Exception:
        pass
    try:
        c.execute('ALTER TABLE cold_books ADD COLUMN pages_per_book INTEGER DEFAULT 50')
    except Exception:
        pass
    # seq_to column no longer needed — keep for backward compat but ignore
    try:
        c.execute('ALTER TABLE cold_books ADD COLUMN seq_to TEXT')
    except Exception:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS book_dispense (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL,
        book_index INTEGER DEFAULT 1,
        serial_from INTEGER DEFAULT 0,
        serial_to INTEGER DEFAULT 0,
        serial_number TEXT,
        location TEXT,
        dispensed_at TEXT,
        FOREIGN KEY(book_id) REFERENCES cold_books(id)
    )''')
    # Migrate: fix serial_number NOT NULL and missing columns
    try:
        col_info = c.execute("PRAGMA table_info(book_dispense)").fetchall()
        sn_col = next((col for col in col_info if col[1] == 'serial_number'), None)
        if sn_col and sn_col[3] == 1:
            conn.execute('PRAGMA foreign_keys=OFF')
            c.execute('''CREATE TABLE book_dispense_fixed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                book_index INTEGER DEFAULT 1,
                serial_from INTEGER DEFAULT 0,
                serial_to INTEGER DEFAULT 0,
                serial_number TEXT,
                location TEXT,
                dispensed_at TEXT,
                FOREIGN KEY(book_id) REFERENCES cold_books(id)
            )''')
            c.execute('''INSERT INTO book_dispense_fixed
                         (id, book_id, book_index, serial_from, serial_to, serial_number, location, dispensed_at)
                         SELECT id, book_id, book_index, serial_from, serial_to, serial_number, location, dispensed_at
                         FROM book_dispense''')
            c.execute('DROP TABLE book_dispense')
            c.execute('ALTER TABLE book_dispense_fixed RENAME TO book_dispense')
            conn.execute('PRAGMA foreign_keys=ON')
    except Exception:
        pass
    try:
        c.execute('ALTER TABLE book_dispense ADD COLUMN serial_number TEXT')
    except Exception:
        pass
    try:
        c.execute('ALTER TABLE book_dispense ADD COLUMN location TEXT')
    except Exception:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS weight_diff_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seq_no TEXT,
        receipt_date TEXT,
        car_number TEXT,
        permit_number TEXT,
        category TEXT,
        entity TEXT,
        dispense_room TEXT,
        receipt_number TEXT NOT NULL,
        source_sheet TEXT,
        imported_at TEXT DEFAULT (datetime('now'))
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_wdr_receipt_number ON weight_diff_receipts(receipt_number)')
    try:
        c.execute('ALTER TABLE weight_diff_receipts ADD COLUMN source_sheet TEXT')
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE weight_diff_receipts ADD COLUMN product_type TEXT NOT NULL DEFAULT 'potato'")
    except Exception:
        pass
    c.execute('CREATE INDEX IF NOT EXISTS idx_wdr_product_type ON weight_diff_receipts(product_type)')

    # ─── تخطي الايصالات المكررة (لما المستخدم يراجع ويأكد إنه مش تكرار حقيقي) ───
    c.execute('''CREATE TABLE IF NOT EXISTS duplicate_receipts_dismissed (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year TEXT NOT NULL,
        receipt_number TEXT NOT NULL,
        reason TEXT,
        dismissed_by TEXT,
        dismissed_at TEXT DEFAULT (datetime('now')),
        UNIQUE(year, receipt_number)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS page_visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        page TEXT,
        username TEXT,
        ip_address TEXT,
        search_query TEXT,
        visited_at TEXT DEFAULT (datetime('now'))
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_pv_page ON page_visits(page)')

    # Default admin
    existing = c.execute("SELECT id FROM admins WHERE username='admin'").fetchone()
    if not existing:
        c.execute("INSERT INTO admins (username, password, secondary_password) VALUES (?, ?, ?)",
                  ('admin',
                   generate_password_hash('1907*'),
                   generate_password_hash('1907*')))

    conn.commit()
    conn.close()

# ─── Auth Decorators ──────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def get_client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or '—'


def log_page_visit(page, search_query=None):
    try:
        username = session.get('admin_username') or 'زائر (غير مسجل دخول)'
        conn = get_db()
        conn.execute(
            "INSERT INTO page_visits (page, username, ip_address, search_query) VALUES (?, ?, ?, ?)",
            (page, username, get_client_ip(), search_query)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

# ─── Auth Routes ─────────────────────────────────────────────────────────────

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'admin_id' in session:
        return redirect(url_for('dashboard'))
    
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        conn = get_db()
        admin = conn.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
        conn.close()
        if admin and check_password_hash(admin['password'], password):
            session['admin_id'] = admin['id']
            session['admin_username'] = admin['username']
            return redirect(url_for('dashboard'))
        else:
            error = 'اسم المستخدم أو كلمة المرور غير صحيحة'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    stats = {
        'users': conn.execute("SELECT COUNT(*) as c FROM users").fetchone()['c'],
        'sap': conn.execute("SELECT COUNT(*) as c FROM sap_transactions").fetchone()['c'],
        'books': conn.execute("SELECT COUNT(*) as c FROM cold_books").fetchone()['c'],
        'other_users': conn.execute("SELECT COUNT(*) as c FROM other_users").fetchone()['c'],
    }
    conn.close()
    return render_template('dashboard.html', stats=stats)

# ─── Module 1: Users ─────────────────────────────────────────────────────────

@app.route('/users')
@login_required
def users():
    conn = get_db()
    search = request.args.get('search', '')
    location_filter = request.args.get('location', '')
    user_type = request.args.get('user_type', 'october')

    query = "SELECT * FROM users WHERE user_type = ?"
    params = [user_type]
    if search:
        query += " AND (username LIKE ? OR gpit LIKE ? OR national_id LIKE ?)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    if location_filter:
        query += " AND location = ?"
        params.append(location_filter)
    query += " ORDER BY created_at DESC, id ASC"
    all_users = conn.execute(query, params).fetchall()
    locations_oct = [r['location'] for r in conn.execute("SELECT DISTINCT location FROM users WHERE user_type='october' AND location IS NOT NULL AND location != '' ORDER BY location").fetchall()]
    locations_other = [r['location'] for r in conn.execute("SELECT DISTINCT location FROM users WHERE user_type='other' AND location IS NOT NULL AND location != '' ORDER BY location").fetchall()]
    count_oct = conn.execute("SELECT COUNT(*) as c FROM users WHERE user_type='october'").fetchone()['c']
    count_other = conn.execute("SELECT COUNT(*) as c FROM users WHERE user_type='other'").fetchone()['c']
    conn.close()
    locations = locations_oct if user_type == 'october' else locations_other
    return render_template('users.html', users=all_users, search=search, location_filter=location_filter,
                           locations=locations, user_type=user_type,
                           count_oct=count_oct, count_other=count_other)

@app.route('/users/add', methods=['POST'])
@login_required
def add_user():
    d = request.form
    conn = get_db()
    user_type = d.get('user_type', 'october')
    conn.execute("""INSERT INTO users (gpit, username, password, location, national_id, security_q1, phone, hire_date, user_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (d.get('gpit'), d.get('username'), generate_password_hash(d.get('password', '')),
                  d.get('location'), d.get('national_id'), d.get('security_q1'),
                  d.get('phone'), d.get('hire_date'), user_type))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم إضافة المستخدم بنجاح'})

@app.route('/users/edit/<int:uid>', methods=['GET', 'POST'])
@login_required
def edit_user(uid):
    conn = get_db()
    if request.method == 'POST':
        d = request.form
        pw = d.get('password', '')
        if pw:
            conn.execute("""UPDATE users SET gpit=?, username=?, password=?, location=?, national_id=?,
                            security_q1=?, phone=?, hire_date=? WHERE id=?""",
                         (d.get('gpit'), d.get('username'), generate_password_hash(pw),
                          d.get('location'), d.get('national_id'), d.get('security_q1'),
                          d.get('phone'), d.get('hire_date'), uid))
        else:
            conn.execute("""UPDATE users SET gpit=?, username=?, location=?, national_id=?,
                            security_q1=?, phone=?, hire_date=? WHERE id=?""",
                         (d.get('gpit'), d.get('username'), d.get('location'), d.get('national_id'),
                          d.get('security_q1'), d.get('phone'), d.get('hire_date'), uid))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'تم تحديث المستخدم بنجاح'})
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not user:
        return jsonify({'error': 'المستخدم غير موجود'}), 404
    return jsonify(dict(user))

@app.route('/users/edit-page/<int:uid>', methods=['GET', 'POST'])
@login_required
def edit_user_page(uid):
    conn = get_db()
    if request.method == 'POST':
        d = request.form
        pw = d.get('password', '').strip()
        pw_confirm = d.get('password_confirm', '').strip()
        if pw and pw != pw_confirm:
            flash('كلمتا المرور غير متطابقتين!', 'error')
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            conn.close()
            return render_template('edit_user.html', user=user)
        if pw:
            conn.execute("""UPDATE users SET gpit=?, username=?, password=?, location=?, national_id=?,
                            security_q1=?, phone=?, hire_date=? WHERE id=?""",
                         (d.get('gpit'), d.get('username'), generate_password_hash(pw),
                          d.get('location'), d.get('national_id'), d.get('security_q1'),
                          d.get('phone'), d.get('hire_date'), uid))
        else:
            conn.execute("""UPDATE users SET gpit=?, username=?, location=?, national_id=?,
                            security_q1=?, phone=?, hire_date=? WHERE id=?""",
                         (d.get('gpit'), d.get('username'), d.get('location'), d.get('national_id'),
                          d.get('security_q1'), d.get('phone'), d.get('hire_date'), uid))
        conn.commit()
        conn.close()
        flash('تم تحديث بيانات المستخدم بنجاح ✅', 'success')
        return redirect(url_for('edit_user_page', uid=uid))
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not user:
        flash('المستخدم غير موجود', 'error')
        return redirect(url_for('users'))
    return render_template('edit_user.html', user=user)

@app.route('/users/delete/<int:uid>', methods=['POST'])
@login_required
def delete_user(uid):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم حذف المستخدم'})

@app.route('/users/export-excel')
@login_required
def export_users_excel():
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from flask import send_file
    user_type = request.args.get('user_type', 'october')
    conn = get_db()
    users_list = conn.execute("SELECT * FROM users WHERE user_type=? ORDER BY created_at DESC, id ASC", (user_type,)).fetchall()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "أكتوبر" if user_type == "october" else "المصانع الأخرى"
    ws.sheet_view.rightToLeft = True

    headers = ["#", "GPIT", "اسم المستخدم", "الموقع", "الرقم القومي",
               "سؤال الأمان", "رقم الهاتف", "تاريخ التعيين", "تاريخ الإضافة"]
    col_widths = [5, 14, 22, 26, 18, 30, 16, 16, 20]

    header_fill = PatternFill("solid", fgColor="1a2744")
    header_font = Font(bold=True, color="4F9CF9", size=11)
    even_fill = PatternFill("solid", fgColor="0f1629")
    odd_fill = PatternFill("solid", fgColor="161e35")
    thin = Side(style="thin", color="2a3a5c")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col, (h, w) in enumerate(zip(headers, col_widths), 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border
        ws.column_dimensions[cell.column_letter].width = w
    ws.row_dimensions[1].height = 28

    for i, u in enumerate(users_list, 1):
        row_data = [i, u["gpit"] or "", u["username"] or "", u["location"] or "",
                    u["national_id"] or "", u["security_q1"] or "",
                    u["phone"] or "", u["hire_date"] or "", (u["created_at"] or "")[:10]]
        fill = even_fill if i % 2 == 0 else odd_fill
        for col, val in enumerate(row_data, 1):
            cell = ws.cell(row=i+1, column=col, value=val)
            cell.fill = fill
            cell.alignment = center
            cell.border = border
            cell.font = Font(size=10, color="C8D8F0")
        ws.row_dimensions[i+1].height = 22

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = "أكتوبر" if user_type == "october" else "المصانع_الأخرى"
    return send_file(buf, as_attachment=True,
                     download_name=f"GPIT_Team_{fname}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ─── Module 2: SAP Transactions ──────────────────────────────────────────────

@app.route('/sap')
@login_required
def sap():
    conn = get_db()
    search = request.args.get('search', '')
    query = "SELECT * FROM sap_transactions WHERE 1=1"
    params = []
    if search:
        query += " AND (transaction_name LIKE ? OR transaction_code LIKE ? OR movement LIKE ?)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    query += " ORDER BY created_at DESC, id ASC"
    transactions = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('sap.html', transactions=transactions, search=search)

@app.route('/sap/add', methods=['POST'])
@login_required
def add_sap():
    d = request.form
    conn = get_db()
    conn.execute("INSERT INTO sap_transactions (transaction_name, transaction_code, movement) VALUES (?, ?, ?)",
                 (d.get('transaction_name'), d.get('transaction_code'), d.get('movement')))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم إضافة المعاملة بنجاح'})

@app.route('/sap/edit/<int:tid>', methods=['GET', 'POST'])
@login_required
def edit_sap(tid):
    conn = get_db()
    if request.method == 'POST':
        d = request.form
        conn.execute("UPDATE sap_transactions SET transaction_name=?, transaction_code=?, movement=? WHERE id=?",
                     (d.get('transaction_name'), d.get('transaction_code'), d.get('movement'), tid))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'تم تحديث المعاملة بنجاح'})
    t = conn.execute("SELECT * FROM sap_transactions WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not t:
        return jsonify({'error': 'المعاملة غير موجودة'}), 404
    return jsonify(dict(t))

@app.route('/sap/delete/<int:tid>', methods=['POST'])
@login_required
def delete_sap(tid):
    conn = get_db()
    conn.execute("DELETE FROM sap_transactions WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم حذف المعاملة'})

# ─── Module 3: Other Users (Protected) ───────────────────────────────────────

@app.route('/other-users')
@login_required
def other_users():
    if not session.get('secondary_auth'):
        return render_template('secondary_auth.html')
    conn = get_db()
    search = request.args.get('search', '')
    query = "SELECT * FROM other_users WHERE 1=1"
    params = []
    if search:
        query += " AND (username LIKE ? OR gpit LIKE ? OR location LIKE ?)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    query += " ORDER BY created_at DESC, id ASC"
    users_list = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('other_users.html', users=users_list, search=search)

@app.route('/secondary-auth', methods=['POST'])
@login_required
def secondary_auth():
    password = request.form.get('secondary_password', '')
    conn = get_db()
    admin = conn.execute("SELECT * FROM admins WHERE id=?", (session['admin_id'],)).fetchone()
    conn.close()
    if admin and check_password_hash(admin['secondary_password'], password):
        session['secondary_auth'] = True
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'كلمة المرور الثانوية غير صحيحة'})

@app.route('/secondary-logout')
@login_required
def secondary_logout():
    session.pop('secondary_auth', None)
    return redirect(url_for('dashboard'))

@app.route('/other-users/add', methods=['POST'])
@login_required
def add_other_user():
    if not session.get('secondary_auth'):
        return jsonify({'error': 'غير مصرح'}), 403
    d = request.form
    conn = get_db()
    conn.execute("INSERT INTO other_users (gpit, username, location) VALUES (?, ?, ?)",
                 (d.get('gpit'), d.get('username'), d.get('location')))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم الإضافة بنجاح'})

@app.route('/other-users/edit/<int:uid>', methods=['GET', 'POST'])
@login_required
def edit_other_user(uid):
    if not session.get('secondary_auth'):
        return jsonify({'error': 'غير مصرح'}), 403
    conn = get_db()
    if request.method == 'POST':
        d = request.form
        conn.execute("UPDATE other_users SET gpit=?, username=?, location=? WHERE id=?",
                     (d.get('gpit'), d.get('username'), d.get('location'), uid))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'تم التحديث بنجاح'})
    u = conn.execute("SELECT * FROM other_users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not u:
        return jsonify({'error': 'غير موجود'}), 404
    return jsonify(dict(u))

@app.route('/other-users/delete/<int:uid>', methods=['POST'])
@login_required
def delete_other_user(uid):
    if not session.get('secondary_auth'):
        return jsonify({'error': 'غير مصرح'}), 403
    conn = get_db()
    conn.execute("DELETE FROM other_users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم الحذف'})

# ─── Module 4: Cold Books ─────────────────────────────────────────────────────

@app.route('/cold-books')
@login_required
def cold_books():
    conn = get_db()
    sort = request.args.get('sort', 'created_at')
    order = request.args.get('order', 'DESC')
    if sort not in ['book_name', 'last_purchase_date', 'quantity', 'created_at']:
        sort = 'created_at'
    if order not in ['ASC', 'DESC']:
        order = 'DESC'
    books = conn.execute(f"SELECT * FROM cold_books ORDER BY {sort} {order}, id ASC").fetchall()
    conn.close()
    return render_template('cold_books.html', books=books, sort=sort, order=order)

# ── سجل الصرف الكامل مع فلترة ───────────────────────────────────────────────
@app.route('/dispense-log')
@login_required
def dispense_log():
    conn   = get_db()
    # فلاتر من الـ query string
    f_book     = request.args.get('book_id',  '')
    f_location = request.args.get('location', '').strip()
    f_date_from= request.args.get('date_from','').strip()
    f_date_to  = request.args.get('date_to',  '').strip()
    f_search   = request.args.get('search',   '').strip()
    page       = max(1, int(request.args.get('page', 1)))
    per_page   = 50

    query  = """
        SELECT d.id, d.book_id, d.book_index, d.serial_from, d.serial_to,
               d.serial_number, d.location, d.dispensed_at,
               b.book_name, b.pages_per_book
        FROM book_dispense d
        LEFT JOIN cold_books b ON b.id = d.book_id
        WHERE 1=1
    """
    params = []

    if f_book:
        query += " AND d.book_id = ?"
        params.append(f_book)
    if f_location:
        query += " AND d.location LIKE ?"
        params.append(f'%{f_location}%')
    if f_date_from:
        query += " AND d.dispensed_at >= ?"
        params.append(f_date_from)
    if f_date_to:
        query += " AND d.dispensed_at <= ?"
        params.append(f_date_to + ' 23:59:59')
    if f_search:
        search_conditions = [
            "b.book_name LIKE ?",
            "d.location LIKE ?",
            "CAST(d.serial_from AS TEXT) LIKE ?",
            "CAST(d.serial_to AS TEXT) LIKE ?",
            "CAST(d.serial_number AS TEXT) LIKE ?"
        ]
        search_params = [f'%{f_search}%'] * 5
        # لو المستخدم كاتب رقم سيريال، دور كمان على أي دفتر يقع هذا الرقم ضمن نطاقه (من - إلى)
        if f_search.isdigit():
            search_conditions.append(
                "(d.serial_from IS NOT NULL AND d.serial_to IS NOT NULL "
                "AND d.serial_to >= d.serial_from AND safe_int(?) BETWEEN d.serial_from AND d.serial_to)"
            )
            search_params.append(f_search)
        query += " AND (" + " OR ".join(search_conditions) + ")"
        params += search_params

    # إجمالي السجلات للـ pagination
    count_q = f"SELECT COUNT(*) as c FROM ({query}) AS q"
    total_records = conn.execute(count_q, params).fetchone()['c']
    total_pages   = max(1, (total_records + per_page - 1) // per_page)
    page          = min(page, total_pages)
    offset        = (page - 1) * per_page

    query += " ORDER BY d.dispensed_at DESC, d.id ASC LIMIT ? OFFSET ?"
    params += [per_page, offset]
    records = conn.execute(query, params).fetchall()

    # ── تصدير (CSV أو Excel) ─────────────────────────────────────────────────
    export_type = request.args.get('export', '')
    if export_type in ('csv', 'excel'):
        base_query  = query.replace(" LIMIT ? OFFSET ?", "")
        base_params = params[:-2]
        all_records = conn.execute(base_query, base_params).fetchall()
        conn.close()

        headers_ar = ['#', 'اسم الصنف', 'رقم الدفتر', 'السيريال من', 'السيريال إلى', 'الموقع', 'تاريخ الصرف', 'الوقت']

        def row_data(i, r):
            disp_date = (r['dispensed_at'] or '')[:10]
            disp_time = (r['dispensed_at'] or '')[11:16]
            return [
                i,
                r['book_name'] or '',
                r['book_index'] or '',
                r['serial_from'] or r['serial_number'] or '',
                r['serial_to'] or '',
                r['location'] or '',
                disp_date,
                disp_time
            ]

        if export_type == 'csv':
            import csv, io
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers_ar)
            for i, r in enumerate(all_records, 1):
                writer.writerow(row_data(i, r))
            from flask import Response
            return Response(
                '\ufeff' + output.getvalue(),
                mimetype='text/csv; charset=utf-8',
                headers={'Content-Disposition': 'attachment; filename=dispense_log.csv'}
            )

        if export_type == 'excel':
            import io, zipfile, xml.etree.ElementTree as ET
            from datetime import datetime as _dt

            # ── بناء ملف xlsx يدوياً (zip من ملفات XML) بدون أي مكتبة خارجية ──
            # xlsx = مجرد zip يحتوي على ملفات XML محددة

            rows_data = []
            for i, r in enumerate(all_records, 1):
                disp_date = (r['dispensed_at'] or '')[:10]
                disp_time = (r['dispensed_at'] or '')[11:16]
                rows_data.append([
                    str(i),
                    r['book_name'] or '',
                    str(r['book_index'] or ''),
                    str(r['serial_from'] or r['serial_number'] or ''),
                    str(r['serial_to'] or ''),
                    r['location'] or '',
                    disp_date,
                    disp_time
                ])

            def esc(s):
                return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;').replace("'",'&apos;')

            def make_sheet(headers, rows):
                lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
                lines.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
                             ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">')
                lines.append('<sheetView workbookViewId="0" rightToLeft="1"/>')
                lines.append('<sheetData>')
                # header row
                lines.append('<row r="1">')
                for ci, h in enumerate(headers, 1):
                    col = chr(64+ci)
                    lines.append(f'<c r="{col}1" t="inlineStr" s="1"><is><t>{esc(h)}</t></is></c>')
                lines.append('</row>')
                # data rows
                for ri, row in enumerate(rows, 2):
                    lines.append(f'<row r="{ri}">')
                    for ci, val in enumerate(row, 1):
                        col = chr(64+ci) if ci <= 26 else 'A'+chr(64+ci-26)
                        lines.append(f'<c r="{col}{ri}" t="inlineStr"><is><t>{esc(val)}</t></is></c>')
                    lines.append('</row>')
                lines.append('</sheetData>')
                lines.append('</worksheet>')
                return '\n'.join(lines)

            def make_styles():
                return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts><font><sz val="11"/><name val="Cairo"/></font>
         <font><sz val="11"/><b/><name val="Cairo"/><color rgb="FFFFFFFF"/></font></fonts>
  <fills><fill><patternFill patternType="none"/></fill>
         <fill><patternFill patternType="gray125"/></fill>
         <fill><patternFill patternType="solid"><fgColor rgb="FF1E3A5F"/></patternFill></fill></fills>
  <borders><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
</styleSheet>'''

            sheet_xml   = make_sheet(headers_ar, rows_data)
            styles_xml  = make_styles()

            rels_main = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

            rels_wb = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

            workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="سجل الصرف" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''

            content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml"  ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml"            ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml"   ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml"              ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('[Content_Types].xml',          content_types)
                zf.writestr('_rels/.rels',                  rels_main)
                zf.writestr('xl/workbook.xml',              workbook_xml)
                zf.writestr('xl/_rels/workbook.xml.rels',   rels_wb)
                zf.writestr('xl/worksheets/sheet1.xml',     sheet_xml)
                zf.writestr('xl/styles.xml',                styles_xml)

            buf.seek(0)
            from flask import Response
            return Response(
                buf.getvalue(),
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={'Content-Disposition': 'attachment; filename=dispense_log.xlsx'}
            )

    # قوائم للفلاتر
    all_books     = conn.execute("SELECT id, book_name FROM cold_books ORDER BY book_name, id").fetchall()
    all_locations = conn.execute(
        "SELECT DISTINCT location FROM book_dispense WHERE location IS NOT NULL AND location != '' ORDER BY location"
    ).fetchall()

    # إحصائيات سريعة
    stats = conn.execute("""
        SELECT COUNT(*) as total_dispense,
               COUNT(DISTINCT d.book_id) as books_touched,
               COUNT(DISTINCT d.location) as locations_count
        FROM book_dispense d
    """).fetchone()

    conn.close()
    return render_template('dispense_log.html',
        records=records,
        all_books=all_books,
        all_locations=[r['location'] for r in all_locations],
        f_book=f_book, f_location=f_location,
        f_date_from=f_date_from, f_date_to=f_date_to, f_search=f_search,
        page=page, total_pages=total_pages, total_records=total_records,
        stats=stats
    )

# ── helper: احسب عدد الدفاتر المصروفة وعدد المتبقي ──────────────────────────
def _book_stats(conn, bid, total_books, min_qty):
    dispensed = conn.execute(
        "SELECT COUNT(*) as c FROM book_dispense WHERE book_id=?", (bid,)
    ).fetchone()['c']
    remaining = total_books - dispensed
    return dispensed, remaining, remaining <= min_qty

# ── صرف دفتر واحد ───────────────────────────────────────────────────────────
@app.route('/cold-books/dispense/<int:bid>', methods=['POST'])
@login_required
def dispense_cold_book(bid):
    d        = request.form
    location = d.get('location', '').strip()
    serial   = d.get('serial_number', '').strip()

    if not location:
        return jsonify({'success': False, 'message': 'يجب إدخال الموقع المصروف إليه'})
    if not serial:
        return jsonify({'success': False, 'message': 'يجب إدخال رقم السيريال'})

    conn = get_db()
    book = conn.execute("SELECT * FROM cold_books WHERE id=?", (bid,)).fetchone()
    if not book:
        conn.close()
        return jsonify({'success': False, 'message': 'الدفتر غير موجود'})

    total     = book['quantity'] or 0
    pages     = book['pages_per_book'] or 50
    seq_start = int(book['seq_from'] or 0)
    seq_end   = int(book['seq_to'] or (seq_start + total * pages - 1))
    min_qty   = book['min_quantity'] or 5

    # التحقق أن السيريال ضمن نطاق هذا الدفتر
    try:
        s_num = int(serial)
    except ValueError:
        conn.close()
        return jsonify({'success': False, 'message': 'رقم السيريال يجب أن يكون رقماً صحيحاً'})

    if s_num < seq_start or s_num > seq_end:
        conn.close()
        return jsonify({'success': False, 'message': f'السيريال {serial} خارج نطاق هذا الدفتر ({seq_start} → {seq_end})'})

    # تحديد رقم الدفتر الذي ينتمي إليه هذا السيريال
    idx    = ((s_num - seq_start) // pages) + 1
    s_from = seq_start + (idx - 1) * pages
    s_to   = s_from + pages - 1

    # التحقق أن هذا الدفتر لم يُصرف مسبقاً
    already = conn.execute(
        "SELECT id FROM book_dispense WHERE book_id=? AND book_index=?", (bid, idx)
    ).fetchone()
    if already:
        conn.close()
        return jsonify({'success': False, 'message': f'الدفتر رقم {idx} (سيريال {s_from} → {s_to}) تم صرفه مسبقاً'})

    dispensed_count, remaining, _ = _book_stats(conn, bid, total, min_qty)
    if remaining <= 0:
        conn.close()
        return jsonify({'success': False, 'message': 'لا توجد دفاتر متبقية'})

    now = _now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        "INSERT INTO book_dispense (book_id, book_index, serial_from, serial_to, location, dispensed_at) VALUES (?,?,?,?,?,?)",
        (bid, idx, s_from, s_to, location, now)
    )
    conn.commit()
    _, new_remaining, warning = _book_stats(conn, bid, total, min_qty)
    conn.close()
    return jsonify({
        'success':      True,
        'message':      f'تم صرف الدفتر رقم {idx} (سيريال {s_from} → {s_to})',
        'book_index':   idx,
        'serial_from':  s_from,
        'serial_to':    s_to,
        'remaining':    new_remaining,
        'total':        total,
        'warning':      warning,
        'min_quantity': min_qty
    })


# ── صرف جماعي (نطاق سيريال) ─────────────────────────────────────────────────
@app.route('/cold-books/dispense/bulk/<int:bid>', methods=['POST'])
@login_required
def bulk_dispense_cold_book(bid):
    d            = request.form
    serial_from  = d.get('serial_from', '').strip()
    serial_to    = d.get('serial_to',   '').strip()
    location     = d.get('location',    '').strip()

    if not serial_from or not serial_to:
        return jsonify({'success': False, 'message': 'يجب إدخال نطاق السيريال'})
    if not location:
        return jsonify({'success': False, 'message': 'يجب إدخال الموقع'})
    try:
        s_from = int(serial_from)
        s_to   = int(serial_to)
    except ValueError:
        return jsonify({'success': False, 'message': 'يجب أن تكون السيريالات أرقاماً صحيحة'})
    if s_from > s_to:
        return jsonify({'success': False, 'message': 'رقم البداية يجب أن يكون أصغر من أو يساوي رقم النهاية'})

    conn = get_db()
    book = conn.execute("SELECT * FROM cold_books WHERE id=?", (bid,)).fetchone()
    if not book:
        conn.close()
        return jsonify({'success': False, 'message': 'الدفتر غير موجود'})

    total     = book['quantity'] or 0
    pages     = book['pages_per_book'] or 50
    seq_start = int(book['seq_from'] or 0)
    min_qty   = book['min_quantity'] or 5

    # التحقق أن النطاق المطلوب ضمن نطاق الدفتر المسجل
    book_seq_to = int(book['seq_to'] or (seq_start + total * pages - 1))
    if s_from < seq_start or s_to > book_seq_to:
        conn.close()
        return jsonify({'success': False,
                        'message': f'النطاق المطلوب ({s_from} → {s_to}) خارج نطاق الدفتر ({seq_start} → {book_seq_to})'})

    # تحديد أرقام الدفاتر المطلوبة من النطاق
    book_idx_from = ((s_from - seq_start) // pages) + 1
    book_idx_to   = ((s_to   - seq_start) // pages) + 1

    if (book_idx_to - book_idx_from + 1) > 500:
        conn.close()
        return jsonify({'success': False, 'message': 'الحد الأقصى للصرف الجماعي 500 دفتر في المرة الواحدة'})

    # جلب الدفاتر المصروفة مسبقاً
    dispensed_indices = {r['book_index'] for r in conn.execute(
        "SELECT book_index FROM book_dispense WHERE book_id=? AND book_index IS NOT NULL", (bid,)).fetchall()}

    # التحقق من الكمية المتبقية
    dispensed_count = conn.execute(
        "SELECT COUNT(*) as c FROM book_dispense WHERE book_id=?", (bid,)
    ).fetchone()['c']
    remaining = total - dispensed_count
    books_to_insert = [i for i in range(book_idx_from, book_idx_to + 1) if i not in dispensed_indices]

    if len(books_to_insert) > remaining:
        conn.close()
        return jsonify({'success': False,
                        'message': f'الدفاتر المطلوبة ({len(books_to_insert)}) أكبر من المتبقي ({remaining})'})

    inserted = 0
    skipped  = (book_idx_to - book_idx_from + 1) - len(books_to_insert)
    now = _now().strftime('%Y-%m-%d %H:%M:%S')

    for idx in books_to_insert:
        bf = seq_start + (idx - 1) * pages
        bt = bf + pages - 1
        conn.execute(
            "INSERT INTO book_dispense (book_id, book_index, serial_from, serial_to, location, dispensed_at) VALUES (?,?,?,?,?,?)",
            (bid, idx, bf, bt, location, now)
        )
        inserted += 1

    conn.commit()
    _, new_remaining, warning = _book_stats(conn, bid, total, min_qty)
    conn.close()

    msg = f'تم صرف {inserted} دفتر بنجاح'
    if skipped:
        msg += f' — تم تخطي {skipped} مصروف مسبقاً'
    return jsonify({
        'success':      True,
        'message':      msg,
        'inserted':     inserted,
        'skipped':      skipped,
        'remaining':    new_remaining,
        'total':        total,
        'warning':      warning,
        'min_quantity': min_qty
    })

# ── حذف صرف ────────────────────────────────────────────────────────────────
@app.route('/cold-books/dispense/delete/<int:dispense_id>', methods=['POST'])
@login_required
def delete_dispense(dispense_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM book_dispense WHERE id=?", (dispense_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': 'السجل غير موجود'})
    book_idx = row['book_index']
    s_from   = row['serial_from']
    s_to     = row['serial_to']
    conn.execute("DELETE FROM book_dispense WHERE id=?", (dispense_id,))
    conn.commit()
    conn.close()
    serial_info = f'سيريال {s_from}→{s_to}' if s_from is not None else 'بدون تسلسل'
    return jsonify({
        'success': True,
        'message': f'✅ تم حذف دفتر {book_idx} ({serial_info}) من السجل'
    })

# ── تعديل موقع سجل صرف ────────────────────────────────────────────────────
@app.route('/cold-books/dispense/edit-location/<int:dispense_id>', methods=['POST'])
@login_required
def edit_dispense_location(dispense_id):
    d        = request.form
    location = d.get('location', '').strip()
    if not location:
        return jsonify({'success': False, 'message': 'يجب إدخال الموقع'})

    conn = get_db()
    row = conn.execute("SELECT * FROM book_dispense WHERE id=?", (dispense_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': 'السجل غير موجود'})

    conn.execute("UPDATE book_dispense SET location=? WHERE id=?", (location, dispense_id))
    conn.commit()
    conn.close()
    return jsonify({
        'success':  True,
        'message':  f'✅ تم تعديل الموقع إلى "{location}"',
        'location': location
    })

# ── حذف جماعي لسجلات الصرف ───────────────────────────────────────────────────
@app.route('/cold-books/dispense/delete-bulk', methods=['POST'])
@login_required
def delete_dispense_bulk():
    data = request.get_json(silent=True) or {}
    ids  = data.get('ids', [])
    if not ids or not isinstance(ids, list):
        return jsonify({'success': False, 'message': 'لم يتم تحديد أي سجلات للحذف'})
    try:
        ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'معرّفات غير صالحة'})
    if len(ids) > 500:
        return jsonify({'success': False, 'message': 'الحد الأقصى للحذف الجماعي 500 سجل في المرة'})
    conn = get_db()
    placeholders = ','.join('?' * len(ids))
    deleted = conn.execute(
        f"SELECT COUNT(*) as c FROM book_dispense WHERE id IN ({placeholders})", ids
    ).fetchone()['c']
    conn.execute(f"DELETE FROM book_dispense WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()
    return jsonify({
        'success': True,
        'message': f'✅ تم حذف {deleted} سجل من سجل الصرف',
        'deleted': deleted
    })

# ── تنبيهات المخزون ─────────────────────────────────────────────────────────
@app.route('/cold-books/low-stock-alerts')
@login_required
def low_stock_alerts():
    conn  = get_db()
    books = conn.execute("SELECT * FROM cold_books").fetchall()
    alerts = []
    for book in books:
        total   = book['quantity'] or 0
        min_qty = book['min_quantity'] or 5
        _, remaining, is_low = _book_stats(conn, book['id'], total, min_qty)
        if is_low:
            alerts.append({
                'id':           book['id'],
                'book_name':    book['book_name'],
                'remaining':    remaining,
                'min_quantity': min_qty,
                'total':        total
            })
    conn.close()
    return jsonify({'alerts': alerts})

# ── سجل صرف دفتر ────────────────────────────────────────────────────────────
@app.route('/cold-books/serials/<int:bid>')
@login_required
def get_book_serials(bid):
    conn = get_db()
    book = conn.execute("SELECT * FROM cold_books WHERE id=?", (bid,)).fetchone()
    if not book:
        conn.close()
        return jsonify({'error': 'الدفتر غير موجود'}), 404

    total   = book['quantity'] or 0
    pages   = book['pages_per_book'] or 50
    min_qty = book['min_quantity'] or 5

    # جلب السجلات — تدعم الصيغتين (book_index row أو serial_number rows)
    rows = conn.execute(
        """SELECT id, book_index, serial_from, serial_to, serial_number, location, dispensed_at
           FROM book_dispense WHERE book_id=? ORDER BY dispensed_at ASC, id ASC""",
        (bid,)
    ).fetchall()

    # تجميع السيريالات الفردية إلى دفاتر
    # إذا serial_from موجود → سجل دفتر مباشر
    # إذا فقط serial_number → نجمّع كل 50 متتالي = دفتر واحد
    book_records = []
    individual_serials = []

    for r in rows:
        rd = dict(r)
        # إذا عنده book_index → سجل دفتر كامل مباشرة (بتسلسل أو بدونه)
        if rd.get('book_index') is not None:
            book_records.append(rd)
        # إذا فقط serial_number (صيغة قديمة) → individual_serials للتجميع
        elif rd.get('serial_number') is not None:
            individual_serials.append(rd)

    # تجميع السيريالات الفردية إلى مجموعات حسب نطاقات الدفاتر
    if individual_serials:
        seq_start = int(book['seq_from'] or 0)
        # صنّف كل سيريال إلى رقم دفتره
        book_groups = {}
        for sr in individual_serials:
            try:
                sn = int(sr['serial_number'])
                book_num = ((sn - seq_start) // pages) + 1
            except (ValueError, TypeError):
                book_num = 0
            if book_num not in book_groups:
                book_groups[book_num] = {
                    'id': sr['id'],
                    'book_index': book_num,
                    'serial_from': None,
                    'serial_to': None,
                    'location': sr['location'],
                    'dispensed_at': sr['dispensed_at'],
                    'serial_count': 0
                }
            # تحديث نطاق السيريال
            sn_int = int(sr['serial_number']) if sr['serial_number'] else 0
            if book_groups[book_num]['serial_from'] is None or sn_int < int(book_groups[book_num]['serial_from']):
                book_groups[book_num]['serial_from'] = sr['serial_number']
            if book_groups[book_num]['serial_to'] is None or sn_int > int(book_groups[book_num]['serial_to']):
                book_groups[book_num]['serial_to'] = sr['serial_number']
            book_groups[book_num]['serial_count'] += 1

        for bg in sorted(book_groups.values(), key=lambda x: x['book_index']):
            book_records.append(bg)

    dispensed_count = len(book_records)
    remaining = total - dispensed_count
    conn.close()

    serials_out = []
    for i, rec in enumerate(book_records):
        serials_out.append({
            'id':           rec.get('id'),
            'book_index':   rec.get('book_index') or (i + 1),
            'serial_from':  rec.get('serial_from'),
            'serial_to':    rec.get('serial_to'),
            'location':     rec.get('location') or '',
            'dispensed_at': (rec.get('dispensed_at') or '').replace('T', ' ')[:16],
            'serial_count': rec.get('serial_count') or pages
        })

    return jsonify({
        'book_name':    book['book_name'],
        'total':        total,
        'pages':        pages,
        'dispensed':    dispensed_count,
        'remaining':    remaining,
        'min_quantity': min_qty,
        'warning':      remaining <= min_qty,
        'serials':      serials_out
    })

# ── صرف بالكمية (عدد دفاتر) ─────────────────────────────────────────────────
@app.route('/cold-books/dispense/qty/<int:bid>', methods=['POST'])
@login_required
def dispense_by_qty(bid):
    d        = request.form
    location = d.get('location', '').strip()
    qty_str  = d.get('qty', '').strip()

    if not location:
        return jsonify({'success': False, 'message': 'يجب إدخال الموقع المصروف إليه'})
    if not qty_str:
        return jsonify({'success': False, 'message': 'يجب إدخال الكمية'})
    try:
        qty = int(qty_str)
        if qty <= 0:
            raise ValueError
    except ValueError:
        return jsonify({'success': False, 'message': 'الكمية يجب أن تكون رقماً صحيحاً أكبر من صفر'})

    conn = get_db()
    book = conn.execute("SELECT * FROM cold_books WHERE id=?", (bid,)).fetchone()
    if not book:
        conn.close()
        return jsonify({'success': False, 'message': 'الدفتر غير موجود'})

    total     = book['quantity'] or 0
    pages     = book['pages_per_book'] or 50
    seq_start = book['seq_from']
    min_qty   = book['min_quantity'] or 5

    # حساب المتبقي
    dispensed_count = conn.execute(
        "SELECT COUNT(*) as c FROM book_dispense WHERE book_id=?", (bid,)
    ).fetchone()['c']
    remaining = total - dispensed_count

    if qty > remaining:
        conn.close()
        return jsonify({'success': False, 'message': f'الكمية المطلوبة ({qty}) أكبر من المتبقي ({remaining})'})

    now = _now().strftime('%Y-%m-%d %H:%M:%S')

    # إذا كان هناك تسلسل → نختار الدفاتر التالية بالترتيب
    if seq_start is not None:
        dispensed_indices = {r['book_index'] for r in conn.execute(
            "SELECT book_index FROM book_dispense WHERE book_id=? AND book_index IS NOT NULL", (bid,)
        ).fetchall()}
        # نأخذ أول qty دفتر غير مصروف بالترتيب
        inserted = 0
        for idx in range(1, total + 1):
            if inserted >= qty:
                break
            if idx in dispensed_indices:
                continue
            s_from = int(seq_start) + (idx - 1) * pages
            s_to   = s_from + pages - 1
            conn.execute(
                "INSERT INTO book_dispense (book_id, book_index, serial_from, serial_to, location, dispensed_at) VALUES (?,?,?,?,?,?)",
                (bid, idx, s_from, s_to, location, now)
            )
            inserted += 1
    else:
        # بدون تسلسل → نضيف سجلات بدون serial
        inserted = 0
        # نحسب أعلى book_index موجود لنكمل منه
        max_idx_row = conn.execute(
            "SELECT MAX(book_index) as mx FROM book_dispense WHERE book_id=?", (bid,)
        ).fetchone()
        next_idx = (max_idx_row['mx'] or 0) + 1
        for i in range(qty):
            conn.execute(
                "INSERT INTO book_dispense (book_id, book_index, serial_from, serial_to, location, dispensed_at) VALUES (?,?,?,?,?,?)",
                (bid, next_idx + i, None, None, location, now)
            )
            inserted += 1

    conn.commit()
    _, new_remaining, warning = _book_stats(conn, bid, total, min_qty)
    conn.close()
    return jsonify({
        'success':      True,
        'message':      f'تم صرف {inserted} دفتر بنجاح',
        'inserted':     inserted,
        'remaining':    new_remaining,
        'total':        total,
        'warning':      warning,
        'min_quantity': min_qty
    })


# ── إضافة كمية لصنف موجود ────────────────────────────────────────────────────
@app.route('/cold-books/add-quantity/<int:bid>', methods=['POST'])
@login_required
def add_quantity_cold_book(bid):
    d    = request.form
    conn = get_db()
    book = conn.execute("SELECT * FROM cold_books WHERE id=?", (bid,)).fetchone()
    if not book:
        conn.close()
        return jsonify({'success': False, 'message': 'الصنف غير موجود'}), 404

    try:
        added = int(d.get('added_quantity', 0))
    except (ValueError, TypeError):
        conn.close()
        return jsonify({'success': False, 'message': 'الكمية يجب أن تكون رقماً صحيحاً'})

    if added <= 0:
        conn.close()
        return jsonify({'success': False, 'message': 'يجب إدخال كمية أكبر من صفر'})

    current_qty   = book['quantity'] or 0
    new_qty       = current_qty + added
    min_qty       = book['min_quantity'] or 5
    purchase_date = d.get('last_purchase_date') or book['last_purchase_date']

    new_seq_to = d.get('new_seq_to')
    if new_seq_to:
        try:
            new_seq_to = int(new_seq_to)
            conn.execute(
                "UPDATE cold_books SET quantity=?, last_purchase_date=?, seq_to=? WHERE id=?",
                (new_qty, purchase_date, new_seq_to, bid)
            )
        except (ValueError, TypeError):
            new_seq_to = None
            conn.execute(
                "UPDATE cold_books SET quantity=?, last_purchase_date=? WHERE id=?",
                (new_qty, purchase_date, bid)
            )
    else:
        new_seq_to = None
        conn.execute(
            "UPDATE cold_books SET quantity=?, last_purchase_date=? WHERE id=?",
            (new_qty, purchase_date, bid)
        )

    conn.commit()
    _, remaining, warning = _book_stats(conn, bid, new_qty, min_qty)
    conn.close()

    return jsonify({
        'success':      True,
        'message':      f'تم إضافة {added} للصنف بنجاح — الإجمالي الجديد: {new_qty}',
        'new_quantity': new_qty,
        'remaining':    remaining,
        'warning':      warning,
        'min_quantity': min_qty,
        'new_seq_to':   new_seq_to
    })


# ── إعادة حساب وتصحيح سيريالات دفتر ────────────────────────────────────────
@app.route('/cold-books/recalc/<int:bid>', methods=['POST'])
@login_required
def recalc_cold_book(bid):
    conn = get_db()
    book = conn.execute("SELECT * FROM cold_books WHERE id=?", (bid,)).fetchone()
    if not book:
        conn.close()
        return jsonify({'success': False, 'message': 'الدفتر غير موجود'}), 404

    seq_start = book['seq_from']
    pages     = book['pages_per_book'] or 50
    qty       = book['quantity'] or 0

    if seq_start is None:
        conn.close()
        return jsonify({'success': False, 'message': 'هذا الصنف بدون تسلسل — لا يحتاج إعادة حساب'})

    seq_start  = int(seq_start)
    pages      = int(pages)
    new_seq_to = seq_start + qty * pages - 1

    conn.execute("UPDATE cold_books SET seq_to=? WHERE id=?", (new_seq_to, bid))

    rows = conn.execute(
        "SELECT id, book_index FROM book_dispense WHERE book_id=? AND book_index IS NOT NULL",
        (bid,)
    ).fetchall()

    updated = 0
    for row in rows:
        idx    = row['book_index']
        s_from = seq_start + (idx - 1) * pages
        s_to   = s_from + pages - 1
        conn.execute(
            "UPDATE book_dispense SET serial_from=?, serial_to=? WHERE id=?",
            (s_from, s_to, row['id'])
        )
        updated += 1

    conn.commit()
    min_qty = book['min_quantity'] or 5
    _, remaining, warning = _book_stats(conn, bid, qty, min_qty)
    conn.close()

    return jsonify({
        'success':      True,
        'message':      f'✅ تم إعادة حساب التسلسل — seq_to الجديد: {new_seq_to} — تم تصحيح {updated} سجل صرف',
        'new_seq_to':   new_seq_to,
        'updated_rows': updated,
        'remaining':    remaining,
        'warning':      warning,
        'min_quantity': min_qty
    })


# ── إضافة دفتر ──────────────────────────────────────────────────────────────
@app.route('/cold-books/add', methods=['POST'])
@login_required
def add_cold_book():
    d     = request.form
    conn  = get_db()
    add_mode = d.get('add_mode', 'serial')  # 'serial' أو 'no_serial'
    if add_mode == 'no_serial':
        # إضافة بالروزمة والكمية فقط — بدون تسلسل
        conn.execute(
            "INSERT INTO cold_books (book_name, seq_from, seq_to, pages_per_book, last_purchase_date, quantity, min_quantity) VALUES (?,?,?,?,?,?,?)",
            (d.get('book_name'), None, None, None,
             d.get('last_purchase_date'), d.get('quantity'), d.get('min_quantity') or 5)
        )
    else:
        # إضافة بالتسلسل
        seq_to = d.get('seq_to') or None
        pages  = d.get('pages_per_book') or 50
        conn.execute(
            "INSERT INTO cold_books (book_name, seq_from, seq_to, pages_per_book, last_purchase_date, quantity, min_quantity) VALUES (?,?,?,?,?,?,?)",
            (d.get('book_name'), d.get('seq_from'), seq_to, pages,
             d.get('last_purchase_date'), d.get('quantity'), d.get('min_quantity') or 5)
        )
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم إضافة الدفتر بنجاح'})

# ── تعديل دفتر ──────────────────────────────────────────────────────────────
@app.route('/cold-books/edit/<int:bid>', methods=['GET', 'POST'])
@login_required
def edit_cold_book(bid):
    conn = get_db()
    if request.method == 'POST':
        d = request.form
        add_mode = d.get('add_mode', 'serial')
        if add_mode == 'no_serial':
            conn.execute(
                "UPDATE cold_books SET book_name=?, seq_from=?, seq_to=?, pages_per_book=?, last_purchase_date=?, quantity=?, min_quantity=? WHERE id=?",
                (d.get('book_name'), None, None, None,
                 d.get('last_purchase_date'), d.get('quantity'), d.get('min_quantity') or 5, bid)
            )
        else:
            seq_to = d.get('seq_to') or None
            pages  = d.get('pages_per_book') or 50
            conn.execute(
                "UPDATE cold_books SET book_name=?, seq_from=?, seq_to=?, pages_per_book=?, last_purchase_date=?, quantity=?, min_quantity=? WHERE id=?",
                (d.get('book_name'), d.get('seq_from'), seq_to, pages,
                 d.get('last_purchase_date'), d.get('quantity'), d.get('min_quantity') or 5, bid)
            )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'تم التحديث بنجاح'})
    b = conn.execute("SELECT * FROM cold_books WHERE id=?", (bid,)).fetchone()
    conn.close()
    if not b:
        return jsonify({'error': 'الدفتر غير موجود'}), 404
    return jsonify(dict(b))

# ── حذف دفتر ────────────────────────────────────────────────────────────────
@app.route('/cold-books/delete/<int:bid>', methods=['POST'])
@login_required
def delete_cold_book(bid):
    conn = get_db()
    conn.execute("DELETE FROM cold_books WHERE id=?", (bid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم حذف الدفتر'})

# ─── Module 5: Admin Management ──────────────────────────────────────────────

@app.route('/admins')
@login_required
def admins():
    conn = get_db()
    search = request.args.get('search', '')
    query = "SELECT id, username FROM admins WHERE 1=1"
    params = []
    if search:
        query += " AND username LIKE ?"
        params.append(f'%{search}%')
    query += " ORDER BY id ASC"
    admins_list = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('admins.html', admins=admins_list, search=search,
                           current_admin_id=session['admin_id'])

@app.route('/admins/add', methods=['POST'])
@login_required
def add_admin():
    d = request.form
    username = d.get('username', '').strip()
    password = d.get('password', '').strip()
    secondary = d.get('secondary_password', '').strip()
    if not username or not password or not secondary:
        return jsonify({'success': False, 'message': 'جميع الحقول مطلوبة'})
    conn = get_db()
    existing = conn.execute("SELECT id FROM admins WHERE username=?", (username,)).fetchone()
    if existing:
        conn.close()
        return jsonify({'success': False, 'message': 'اسم المستخدم موجود بالفعل'})
    conn.execute("INSERT INTO admins (username, password, secondary_password) VALUES (?, ?, ?)",
                 (username, generate_password_hash(password), generate_password_hash(secondary)))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم إضافة المستخدم بنجاح'})

@app.route('/admins/edit/<int:aid>', methods=['GET', 'POST'])
@login_required
def edit_admin(aid):
    conn = get_db()
    if request.method == 'POST':
        d = request.form
        username = d.get('username', '').strip()
        password = d.get('password', '').strip()
        secondary = d.get('secondary_password', '').strip()
        if not username:
            conn.close()
            return jsonify({'success': False, 'message': 'اسم المستخدم مطلوب'})
        # Check username not taken by another admin
        existing = conn.execute("SELECT id FROM admins WHERE username=? AND id!=?", (username, aid)).fetchone()
        if existing:
            conn.close()
            return jsonify({'success': False, 'message': 'اسم المستخدم مستخدم بالفعل'})
        if password and secondary:
            conn.execute("UPDATE admins SET username=?, password=?, secondary_password=? WHERE id=?",
                         (username, generate_password_hash(password), generate_password_hash(secondary), aid))
        elif password:
            conn.execute("UPDATE admins SET username=?, password=? WHERE id=?",
                         (username, generate_password_hash(password), aid))
        elif secondary:
            conn.execute("UPDATE admins SET username=?, secondary_password=? WHERE id=?",
                         (username, generate_password_hash(secondary), aid))
        else:
            conn.execute("UPDATE admins SET username=? WHERE id=?", (username, aid))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'تم تحديث البيانات بنجاح'})
    admin = conn.execute("SELECT id, username FROM admins WHERE id=?", (aid,)).fetchone()
    conn.close()
    if not admin:
        return jsonify({'error': 'المستخدم غير موجود'}), 404
    return jsonify(dict(admin))

@app.route('/admins/delete/<int:aid>', methods=['POST'])
@login_required
def delete_admin(aid):
    if aid == session['admin_id']:
        return jsonify({'success': False, 'message': 'لا يمكنك حذف حسابك الحالي'})
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM admins").fetchone()['c']
    if count <= 1:
        conn.close()
        return jsonify({'success': False, 'message': 'يجب أن يبقى مستخدم واحد على الأقل'})
    conn.execute("DELETE FROM admins WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم حذف المستخدم بنجاح'})

# ─── تصدير / استرداد قاعدة البيانات بالكامل (ملف JSON واحد) ──────────────────
# شغّال على SQLite و PostgreSQL بنفس الكود (من خلال get_db)، فتقدر تصدّر من واحد وتسترد على التاني.
DB_TABLES = ['admins', 'users', 'sap_transactions', 'other_users', 'cold_books',
             'book_dispense', 'weight_diff_receipts', 'duplicate_receipts_dismissed', 'page_visits']
DB_BACKUP_FORMAT = 'erp-db-backup-v1'

@app.route('/db/export')
@login_required
def db_export():
    import json
    from flask import Response
    conn = get_db()
    try:
        tables = {t: [dict(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY id').fetchall()]
                  for t in DB_TABLES}
    finally:
        conn.close()
    now = _now()
    payload = json.dumps({'format': DB_BACKUP_FORMAT, 'exported_at': now.strftime('%Y-%m-%d %H:%M:%S'),
                          'tables': tables}, ensure_ascii=False, default=str)
    return Response(payload, mimetype='application/json; charset=utf-8', headers={
        'Content-Disposition': f'attachment; filename=erp_backup_{now.strftime("%Y%m%d_%H%M%S")}.json'})

@app.route('/db/import', methods=['POST'])
@login_required
def db_import():
    import json
    f = request.files.get('file')
    try:
        doc = json.loads(f.read().decode('utf-8-sig'))
        tables = doc['tables']
        assert doc.get('format') == DB_BACKUP_FORMAT and all(isinstance(tables.get(t), list) for t in DB_TABLES)
    except Exception:
        return jsonify({'success': False, 'message': 'الملف غير صالح — لازم يكون ملف نسخة احتياطية تم تصديره من النظام'})
    if not tables['admins']:
        return jsonify({'success': False, 'message': 'النسخة لا تحتوي على أي مستخدم أدمن — تم إلغاء الاسترداد'})

    conn = get_db()
    try:
        for t in DB_TABLES:
            cols = [d[0] for d in conn.execute(f'SELECT * FROM {t} LIMIT 0').description]
            pg_types = {}
            if USE_PG:
                pg_types = {r['column_name']: r['data_type'] for r in conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = ?", (t,))}
            rows = tables[t]
            conn.execute(f'DELETE FROM {t}')
            if not USE_PG:
                conn.execute('DELETE FROM sqlite_sequence WHERE name=?', (t,))
            if not rows:
                continue
            cols = [c for c in cols if c in rows[0]]

            def conv(c, v):   # PostgreSQL صارم في الأنواع (SQLite بيخزّن أي نوع في أي عمود)
                ty = pg_types.get(c)
                if v is None or not ty:
                    return v
                if ty in ('integer', 'bigint', 'smallint'):
                    return _safe_int(v)
                return v if isinstance(v, str) else str(v)
            conn.executemany(
                f'INSERT INTO {t} ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                [tuple(conv(c, r.get(c)) for c in cols) for r in rows])
            if USE_PG and 'id' in cols:
                conn.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                             f"COALESCE((SELECT MAX(id) FROM {t}), 1), (SELECT MAX(id) FROM {t}) IS NOT NULL)")
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': f'فشل الاسترداد وتم التراجع بدون أي تغيير: {e}'})
    finally:
        conn.close()
    session.clear()   # حسابات الأدمن اتبدّلت بالنسخة → لازم دخول جديد
    return jsonify({'success': True, 'logout': True,
                    'message': 'تم استرداد قاعدة البيانات بالكامل ✅ — سيتم تحويلك لتسجيل الدخول'})

# ─── مساعد: استخراج السنة من نص تاريخ بأي صيغة ────────────────────────────
def extract_year(date_str):
    """يحاول يطلع السنة من نص تاريخ مهما كانت صيغته (2026-01-01, 01-Jan-26, 2026-01-01 10:30:00 ...)"""
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s:
        return None
    m = re.search(r'(19|20)\d{2}', s)
    if m:
        return m.group(0)
    m = re.search(r'[-/](\d{2})(?:\s|$)', s)
    if m:
        yy = int(m.group(1))
        return str(2000 + yy) if yy < 70 else str(1900 + yy)
    return None


# ─── استعلام عام عن دفتر إيصال شحن (بدون تسجيل دخول) ──────────────────────────
@app.route('/receipt-inquiry')
def receipt_inquiry():
    serial   = request.args.get('serial', '').strip()
    result   = None
    searched = bool(serial)
    weight_diff_matches = []

    log_page_visit('receipt_inquiry', search_query=serial if searched else None)

    if searched:
        conn = get_db()
        row = conn.execute("""
            SELECT d.serial_from, d.serial_to, d.serial_number, d.location,
                   d.dispensed_at, d.book_index, b.book_name
            FROM book_dispense d
            LEFT JOIN cold_books b ON b.id = d.book_id
            WHERE b.book_name LIKE ?
              AND (
                    (d.serial_from IS NOT NULL AND d.serial_to IS NOT NULL
                     AND d.serial_to >= d.serial_from
                     AND safe_int(?) BETWEEN d.serial_from AND d.serial_to)
                    OR d.serial_number = ?
                  )
            ORDER BY d.dispensed_at DESC, d.id ASC
            LIMIT 1
        """, ('%شحن%', serial, serial)).fetchone()
        result = row

        # فحص مستقل: هل هذا الرقم مسجّل ضمن "ايصالات فروق أوزان"؟
        # يُعرض دايمًا لو موجود، حتى لو مش مُصرف أصلاً في دفتر الشحن العادي
        weight_diff_matches = conn.execute("""
            SELECT dispense_room, receipt_date, category, car_number, permit_number, entity as dispense_to, product_type
            FROM weight_diff_receipts
            WHERE receipt_number = ?
            ORDER BY id DESC
        """, (serial,)).fetchall()

        conn.close()

    return render_template('receipt_inquiry.html',
                            serial=serial, result=result, searched=searched,
                            weight_diff_matches=weight_diff_matches)


# ─── ايصالات فروق أوزان ────────────────────────────────────────────────────
@app.route('/weight-diff-receipts')
@login_required
def weight_diff_receipts():
    conn = get_db()
    f_search = request.args.get('search', '').strip()
    f_type   = request.args.get('type', 'all').strip()
    if f_type not in ('all', 'potato', 'corn', 'qanater'):
        f_type = 'all'
    page     = max(1, int(request.args.get('page', 1)))
    per_page = 50

    query  = "SELECT * FROM weight_diff_receipts WHERE 1=1"
    params = []
    if f_type != 'all':
        query += " AND product_type = ?"
        params.append(f_type)
    if f_search:
        query += """ AND (receipt_number LIKE ? OR dispense_room LIKE ?
                     OR car_number LIKE ? OR category LIKE ?
                     OR entity LIKE ? OR permit_number LIKE ?)"""
        params += [f'%{f_search}%'] * 6

    count_q = f"SELECT COUNT(*) as c FROM ({query}) AS q"
    total_records = conn.execute(count_q, params).fetchone()['c']
    total_pages   = max(1, (total_records + per_page - 1) // per_page)
    page          = min(page, total_pages)
    offset        = (page - 1) * per_page

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [per_page, offset]
    records = conn.execute(query, params).fetchall()

    type_filter_sql = ""
    type_params = []
    if f_type != 'all':
        type_filter_sql = " WHERE product_type = ?"
        type_params = [f_type]

    stats = conn.execute(f"""
        SELECT COUNT(*) as total,
               COUNT(DISTINCT receipt_number) as unique_receipts,
               COUNT(DISTINCT dispense_room) as rooms_count
        FROM weight_diff_receipts{type_filter_sql}
    """, type_params).fetchone()

    counts = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN product_type = 'potato' THEN 1 ELSE 0 END) as potato_count,
            SUM(CASE WHEN product_type = 'corn' THEN 1 ELSE 0 END) as corn_count,
            SUM(CASE WHEN product_type = 'qanater' THEN 1 ELSE 0 END) as qanater_count
        FROM weight_diff_receipts
    """).fetchone()

    conn.close()
    return render_template('weight_diff.html',
        records=records, f_search=f_search, f_type=f_type,
        page=page, total_pages=total_pages, total_records=total_records,
        stats=stats, counts=counts)


@app.route('/weight-diff-receipts/import', methods=['POST'])
@login_required
def import_weight_diff_receipts():
    product_type = request.form.get('product_type', 'potato').strip()
    if product_type not in ('potato', 'corn', 'qanater'):
        product_type = 'potato'
    product_label = {
        'potato':   'البطاطس',
        'corn':     'الذرة',
        'qanater':  'استرداد من غرفة القناطر',
    }[product_type]

    # الموقع اللي بيتصرف منه ثابت لكل سجلات "استرداد من غرفة القناطر"
    QANATER_FIXED_ROOM = 'سامية صقر القناطر'

    file = request.files.get('excel_file')
    if not file or file.filename == '':
        return jsonify({'success': False, 'message': 'من فضلك اختر ملف اكسيل أولاً'})
    if not file.filename.lower().endswith(('.xlsx', '.xlsm')):
        return jsonify({'success': False, 'message': 'الملف لازم يكون بصيغة Excel (.xlsx)'})

    try:
        import openpyxl
        wb = openpyxl.load_workbook(file, data_only=True)
    except Exception as e:
        return jsonify({'success': False, 'message': f'تعذّر قراءة الملف: {e}'})

    rows_to_insert = []   # كل الصفوف الصالحة بعد إزالة التكرار
    seen_keys       = set()  # مفتاح التكرار (يختلف حسب نوع الصنف - انظر أدناه)
    total_skipped_missing = 0   # صفوف بدون رقم ايصال شحن
    total_skipped_dupe    = 0   # صفوف مكررة تم تجاهلها
    sheets_summary = []    # [(sheet_name, inserted, skipped), ...]
    empty_sheets   = []

    # يتم المرور على كل الورقات (الثلاجات) الموجودة في الملف واستيرادها دفعة واحدة
    for ws in wb.worksheets:
        # ورقة "TOTAL" في ملف الثلاجات هي ورقة تجميع/إجمالي عام وليست بيانات فعلية، بنتخطاها
        if product_type == 'qanater' and ws.title.strip().upper() == 'TOTAL':
            continue

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            empty_sheets.append(ws.title)
            continue

        sheet_inserted = 0
        sheet_skipped  = 0

        if product_type == 'qanater':
            # ─── ملف الثلاجات: لكل ورقة، رقم ايصال الشحن دايمًا في العمود O، واسم
            # الموقع اللي اتصرف ليه دايمًا في العمود M. الموقع اللي اتصرف منه ثابت.
            # صفوف العناوين هنا بتكون على شكل أكتر من صف (مش صف واحد بسيط)، فبندور
            # على صف العناوين الفعلي (اللي فيه "TONS"/"Origin") ونبدأ البيانات بعده.
            header_idx = None
            for idx, row in enumerate(rows[:20]):
                joined = ' '.join(str(v) for v in row if v is not None)
                if 'TONS' in joined or 'Origin' in joined:
                    header_idx = idx
                    break
            start_idx = (header_idx + 1) if header_idx is not None else 0

            for row in rows[start_idx:]:
                if row is None or all(v is None or str(v).strip() == '' for v in row):
                    continue
                row = list(row) + [None] * (15 - len(row))
                entity_val = row[12]   # العمود M = الموقع اللي اتصرف ليه
                receipt_no = row[14]   # العمود O = رقم ايصال الشحن

                # تنضيف أرقام الايصال اللي بتيجي كـ float صحيح (مثلاً 4310.0)
                if isinstance(receipt_no, float) and receipt_no.is_integer():
                    receipt_no = int(receipt_no)
                receipt_no_str = str(receipt_no).strip() if receipt_no is not None else ''
                if not receipt_no_str:
                    sheet_skipped += 1
                    total_skipped_missing += 1
                    continue

                entity_str = str(entity_val).strip() if entity_val is not None else ''

                # مفتاح التكرار: نفس الورقة + نفس الموقع المرسل إليه + نفس رقم الايصال
                dedupe_key = (ws.title, entity_str, receipt_no_str)
                if dedupe_key in seen_keys:
                    sheet_skipped += 1
                    total_skipped_dupe += 1
                    continue
                seen_keys.add(dedupe_key)

                rows_to_insert.append((
                    None, None, None, None, None,
                    entity_str,
                    QANATER_FIXED_ROOM,
                    receipt_no_str,
                    ws.title,
                    product_type
                ))
                sheet_inserted += 1

        else:
            # تخطي صف العناوين لو موجود
            start_idx = 0
            first_row = rows[0]
            joined = ' '.join(str(v) for v in first_row if v is not None)
            first_cell = str(first_row[0]).strip() if first_row and first_row[0] is not None else ''
            if 'ايصال' in joined or 'التاريخ' in joined or first_cell in ('م', 'M'):
                start_idx = 1

            for row in rows[start_idx:]:
                if row is None or all(v is None or str(v).strip() == '' for v in row):
                    continue
                row = list(row) + [None] * (8 - len(row))
                # ترتيب الأعمدة مختلف بين البطاطس والذرة:
                # بطاطس: A=م B=التاريخ C=رقم السيارة D=الاذن     E=الصنف       F=جهة التعنيف G=رقم الغرفة  H=ايصال الشحن
                # ذرة:   A=م B=التاريخ C=رقم السيارة D=الصنف      E=الاذن       F=رقم الغرفة  G=جهة التعنيف H=ايصال الشحن
                if product_type == 'corn':
                    seq_no, r_date, car_no, category, permit_no, room, entity, receipt_no = row[:8]
                else:
                    seq_no, r_date, car_no, permit_no, category, entity, room, receipt_no = row[:8]

                receipt_no_str = str(receipt_no).strip() if receipt_no is not None else ''
                if not receipt_no_str:
                    sheet_skipped += 1
                    total_skipped_missing += 1
                    continue

                if hasattr(r_date, 'strftime'):
                    r_date_str = r_date.strftime('%d-%b-%y')
                else:
                    r_date_str = str(r_date).strip() if r_date is not None else ''

                car_no_str = str(car_no).strip() if car_no is not None else ''
                room_str   = str(room).strip() if room is not None else ''

                # مفتاح التكرار: نفس رقم السيارة + نفس التاريخ + نفس رقم غرفة المنصرف + نفس رقم ايصال الشحن
                dedupe_key = (car_no_str, r_date_str, room_str, receipt_no_str)
                if dedupe_key in seen_keys:
                    sheet_skipped += 1
                    total_skipped_dupe += 1
                    continue
                seen_keys.add(dedupe_key)

                rows_to_insert.append((
                    str(seq_no).strip() if seq_no is not None else '',
                    r_date_str,
                    car_no_str,
                    str(permit_no).strip() if permit_no is not None else '',
                    str(category).strip() if category is not None else '',
                    str(entity).strip() if entity is not None else '',
                    room_str,
                    receipt_no_str,
                    ws.title,
                    product_type
                ))
                sheet_inserted += 1

        if sheet_inserted or sheet_skipped:
            sheets_summary.append((ws.title, sheet_inserted, sheet_skipped))
        else:
            empty_sheets.append(ws.title)

    if not rows_to_insert:
        return jsonify({
            'success': False,
            'message': 'لم يتم العثور على أي بيانات صالحة للاستيراد في أي ورقة بالملف'
        })

    # الاستيراد بيستبدل بيانات هذا الصنف (بطاطس/ذرة) بس بالكامل بالبيانات الجديدة المستوردة الآن
    # (الصنف التاني بتاعه ميتأثرش خالص، لأن كل صنف بياناته منفصلة تمامًا)
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM weight_diff_receipts WHERE product_type = ?", (product_type,))
        c.executemany("""INSERT INTO weight_diff_receipts
            (seq_no, receipt_date, car_number, permit_number, category, entity, dispense_room, receipt_number, source_sheet, product_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows_to_insert)
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': f'حدث خطأ أثناء الحفظ، تم التراجع عن أي تغييرات: {e}'})
    conn.close()

    total_inserted = len(rows_to_insert)
    msg = f'تم استبدال بيانات {product_label} القديمة بالكامل، وتم استيراد {total_inserted} صف من {len(sheets_summary)} ورقة بنجاح ✅'
    extra = []
    if total_skipped_dupe:
        extra.append(f'تجاهل {total_skipped_dupe} صف مكرر')
    if total_skipped_missing:
        extra.append(f'تخطي {total_skipped_missing} صف بدون رقم ايصال شحن')
    if extra:
        msg += ' (' + '، '.join(extra) + ')'

    return jsonify({
        'success': True,
        'message': msg,
        'inserted': total_inserted,
        'skipped': total_skipped_dupe + total_skipped_missing,
        'skipped_duplicate': total_skipped_dupe,
        'skipped_missing': total_skipped_missing,
        'sheets': [{'name': n, 'inserted': i, 'skipped': s} for n, i, s in sheets_summary],
        'empty_sheets': empty_sheets
    })


@app.route('/weight-diff-receipts/delete/<int:rid>', methods=['POST'])
@login_required
def delete_weight_diff_receipt(rid):
    conn = get_db()
    conn.execute("DELETE FROM weight_diff_receipts WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'تم حذف السجل بنجاح'})


@app.route('/weight-diff-receipts/delete-bulk', methods=['POST'])
@login_required
def delete_weight_diff_receipt_bulk():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'success': False, 'message': 'لا توجد سجلات محددة'})
    conn = get_db()
    conn.executemany("DELETE FROM weight_diff_receipts WHERE id=?", [(i,) for i in ids])
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': f'تم حذف {len(ids)} سجل بنجاح'})

@app.route('/weight-diff-receipts/delete-all', methods=['POST'])
@login_required
def delete_weight_diff_receipt_all():
    data = request.get_json(silent=True) or {}
    product_type = data.get('product_type', 'all')
    if product_type not in ('potato', 'corn', 'qanater'):
        product_type = 'all'

    conn = get_db()
    if product_type == 'all':
        count = conn.execute("SELECT COUNT(*) as c FROM weight_diff_receipts").fetchone()['c']
        conn.execute("DELETE FROM weight_diff_receipts")
        label = 'كل السجلات (كل الأصناف)'
    else:
        count = conn.execute("SELECT COUNT(*) as c FROM weight_diff_receipts WHERE product_type=?", (product_type,)).fetchone()['c']
        conn.execute("DELETE FROM weight_diff_receipts WHERE product_type=?", (product_type,))
        label = 'سجلات ' + {
            'potato':  'البطاطس',
            'corn':    'الذرة',
            'qanater': 'استرداد القناطر',
        }[product_type]
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': f'تم حذف {label} ({count} سجل) بنجاح'})


@app.route('/weight-diff-receipts/duplicates')
@login_required
def weight_diff_duplicates():
    f_type = request.args.get('type', 'all').strip()
    if f_type not in ('all', 'potato', 'corn', 'qanater'):
        f_type = 'all'

    conn = get_db()
    type_sql = ""
    type_params = []
    if f_type != 'all':
        type_sql = " AND product_type = ?"
        type_params = [f_type]

    rows = conn.execute(f"""
        SELECT receipt_number, source_sheet, receipt_date, dispense_room, car_number, entity, category, product_type
        FROM weight_diff_receipts
        WHERE receipt_number IN (
            SELECT receipt_number FROM weight_diff_receipts
            WHERE 1=1{type_sql}
            GROUP BY receipt_number HAVING COUNT(*) > 1
        ){type_sql}
        ORDER BY receipt_number, id
    """, type_params + type_params).fetchall()
    conn.close()

    groups = {}
    for r in rows:
        rn = r['receipt_number']
        groups.setdefault(rn, []).append({
            'source_sheet': r['source_sheet'] or '—',
            'receipt_date': r['receipt_date'] or '—',
            'dispense_room': r['dispense_room'] or '—',
            'car_number': r['car_number'] or '—',
            'entity': r['entity'] or '—',
            'category': r['category'] or '—',
            'product_type': r['product_type'] or 'potato',
        })

    result = [{'receipt_number': rn, 'count': len(entries), 'entries': entries}
              for rn, entries in groups.items()]

    return jsonify({'success': True, 'duplicates': result, 'total_receipts': len(result)})


# ─── شاشة الايصالات المكررة (على مستوى كل شيء: دفتر الشحن + بطاطس + ذرة) ───
@app.route('/duplicate-receipts')
@login_required
def duplicate_receipts():
    conn = get_db()

    events = []  # كل الأحداث اللي هنقارن بينها: دفتر شحن + بطاطس + ذرة

    # 1) دفتر إيصال الشحن — فك أي نطاق (من - إلى) لأرقام مفردة
    disp_rows = conn.execute("""
        SELECT d.serial_from, d.serial_to, d.serial_number, d.location,
               d.dispensed_at, b.book_name
        FROM book_dispense d
        LEFT JOIN cold_books b ON b.id = d.book_id
    """).fetchall()
    for r in disp_rows:
        yr = extract_year(r['dispensed_at'])
        room = r['location'] or '—'
        book_label = r['book_name'] or 'دفتر غير معروف'
        disp_date = (r['dispensed_at'] or '')[:10]
        if r['serial_number']:
            events.append({
                'receipt_number': str(r['serial_number']).strip(),
                'year': yr, 'room': room, 'source_label': book_label,
                'source_type': 'book', 'date': disp_date, 'permit_number': '',
            })
        elif r['serial_from'] is not None and r['serial_to'] is not None and r['serial_to'] >= r['serial_from']:
            for n in range(int(r['serial_from']), int(r['serial_to']) + 1):
                events.append({
                    'receipt_number': str(n),
                    'year': yr, 'room': room, 'source_label': book_label,
                    'source_type': 'book', 'date': disp_date, 'permit_number': '',
                })

    # 2) ايصالات فروق الأوزان (بطاطس + ذرة معًا)
    wd_rows = conn.execute("""
        SELECT receipt_number, receipt_date, dispense_room, source_sheet, product_type, permit_number
        FROM weight_diff_receipts
    """).fetchall()
    for r in wd_rows:
        rn = str(r['receipt_number'] or '').strip()
        if not rn:
            continue
        events.append({
            'receipt_number': rn,
            'year': extract_year(r['receipt_date']),
            'room': r['dispense_room'] or '—',
            'source_label': r['source_sheet'] or '—',
            'source_type': r['product_type'] or 'potato',
            'date': r['receipt_date'] or '—',
            'permit_number': str(r['permit_number']).strip() if r['permit_number'] is not None else '',
        })
    conn.close()

    # تجميع حسب (السنة + رقم الايصال) — التكرار معناه أكتر من حدث لنفس المفتاح
    groups = {}
    for e in events:
        key = (e['year'] or 'غير معروفة', e['receipt_number'])
        groups.setdefault(key, []).append(e)

    # ايصال واحد في دفتر الشحن + نفسه في فروق الأوزان = وضع طبيعي متوقع مش تكرار.
    # التكرار الحقيقي: أكتر من قيد في دفتر الشحن لنفس الرقم، أو أكتر من قيد في
    # فروق الأوزان (بطاطس/ذرة) لنفس الرقم — بشرط إن التواريخ مختلفة **و** الاذن مختلف.
    # لو نفس الرقم اتسجل أكتر من مرة لكن كلها في نفس اليوم (نفس التاريخ)، أو كلها
    # بنفس رقم الاذن، يبقى ده وضع طبيعي (نفس الايصال بيكوّت على أكتر من غرفة) ومش
    # هيحسب تكرار. التكرار الحقيقي هو لما التاريخ مختلف والاذن مختلف مع بعض.
    all_duplicate_groups = []
    for (yr, rn), items in groups.items():
        book_items = [it for it in items if it['source_type'] == 'book']
        wd_items = [it for it in items if it['source_type'] != 'book']

        # دفتر إيصال الشحن مفيهوش رقم اذن، فالمعيار الوحيد ليه هو التاريخ
        book_dates = {it['date'] for it in book_items}
        book_is_dup = len(book_items) > 1 and len(book_dates) > 1

        # فروق الأوزان: مش تكرار لو كل القيود بنفس اليوم، أو كل القيود بنفس الاذن
        wd_dates = {it['date'] for it in wd_items}
        wd_permits = {it.get('permit_number', '') for it in wd_items}
        wd_is_dup = (
            len(wd_items) > 1
            and len(wd_dates) > 1
            and len(wd_permits) > 1
        )

        is_dup = book_is_dup or wd_is_dup

        if is_dup:
            # سبب التكرار — بنعرضه قدام كل ايصال مكرر عشان المراجعة تبقى أسهل
            reasons = []
            if book_is_dup:
                reasons.append(f"دفتر إيصال الشحن: اتسجل في {len(book_dates)} تواريخ مختلفة ({', '.join(sorted(book_dates))})")
            if wd_is_dup:
                permits_txt = '، '.join(sorted(p for p in wd_permits if p)) or 'غير مسجل'
                reasons.append(f"فروق الأوزان: اتسجل بتواريخ مختلفة ({', '.join(sorted(wd_dates))}) وأرقام اذن مختلفة ({permits_txt})")
            reason_text = ' | '.join(reasons)

            all_duplicate_groups.append({
                'year': yr, 'receipt_number': rn,
                'entries': items, 'count': len(items),
                'reason': reason_text,
            })

    # ─── استبعاد الايصالات اللي المستخدم راجعها وقرر يتخطاها (مش تكرار حقيقي) ───
    conn2 = get_db()
    dismissed_rows = conn2.execute("""
        SELECT year, receipt_number, reason, dismissed_by, dismissed_at
        FROM duplicate_receipts_dismissed
    """).fetchall()
    conn2.close()
    dismissed_map = {(d['year'], d['receipt_number']): d for d in dismissed_rows}

    active_all_groups = []
    dismissed_groups = []
    for g in all_duplicate_groups:
        key = (g['year'], g['receipt_number'])
        if key in dismissed_map:
            d = dismissed_map[key]
            g2 = dict(g)
            g2['dismiss_reason'] = d['reason'] or ''
            g2['dismissed_by'] = d['dismissed_by'] or '—'
            g2['dismissed_at'] = d['dismissed_at'] or ''
            dismissed_groups.append(g2)
        else:
            active_all_groups.append(g)

    years_available = sorted({g['year'] for g in active_all_groups}, reverse=True)

    f_year   = request.args.get('year', '').strip()
    f_search = request.args.get('search', '').strip()

    duplicate_groups = active_all_groups
    if f_year:
        duplicate_groups = [g for g in duplicate_groups if g['year'] == f_year]
    if f_search:
        duplicate_groups = [g for g in duplicate_groups if f_search in g['receipt_number']]

    duplicate_groups.sort(key=lambda g: (g['year'], g['receipt_number']))
    dismissed_groups.sort(key=lambda g: (g['year'], g['receipt_number']))

    total_duplicate_events = sum(g['count'] for g in duplicate_groups)

    return render_template('duplicate_receipts.html',
        duplicate_groups=duplicate_groups,
        dismissed_groups=dismissed_groups,
        years_available=years_available,
        f_year=f_year, f_search=f_search,
        total_groups=len(active_all_groups),
        shown_groups=len(duplicate_groups),
        total_duplicate_events=total_duplicate_events,
        total_events_scanned=len(events),
        total_dismissed=len(dismissed_groups))


@app.route('/duplicate-receipts/skip', methods=['POST'])
@login_required
def skip_duplicate_receipt():
    year = (request.form.get('year') or '').strip()
    receipt_number = (request.form.get('receipt_number') or '').strip()
    reason = (request.form.get('reason') or '').strip()
    if not year or not receipt_number:
        return jsonify({'success': False, 'message': 'بيانات ناقصة، لم يتم التخطي'})

    conn = get_db()
    conn.execute("""
        INSERT INTO duplicate_receipts_dismissed (year, receipt_number, reason, dismissed_by, dismissed_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(year, receipt_number) DO UPDATE SET
            reason = excluded.reason,
            dismissed_by = excluded.dismissed_by,
            dismissed_at = datetime('now')
    """, (year, receipt_number, reason, session.get('admin_username', '—')))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': f'تم تخطي الايصال رقم {receipt_number} بنجاح'})


@app.route('/duplicate-receipts/skip-all', methods=['POST'])
@login_required
def skip_all_duplicate_receipts():
    raw_items = (request.form.get('items') or '').strip()
    reason = (request.form.get('reason') or '').strip()
    if not raw_items:
        return jsonify({'success': False, 'message': 'لا توجد ايصالات لتخطيها'})

    # الصيغة: "year1|receipt1,year2|receipt2,..."
    pairs = []
    for chunk in raw_items.split(','):
        chunk = chunk.strip()
        if not chunk or '|' not in chunk:
            continue
        yr, rn = chunk.split('|', 1)
        yr, rn = yr.strip(), rn.strip()
        if yr and rn:
            pairs.append((yr, rn))

    if not pairs:
        return jsonify({'success': False, 'message': 'لا توجد ايصالات صالحة لتخطيها'})

    conn = get_db()
    by = session.get('admin_username', '—')
    for yr, rn in pairs:
        conn.execute("""
            INSERT INTO duplicate_receipts_dismissed (year, receipt_number, reason, dismissed_by, dismissed_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(year, receipt_number) DO UPDATE SET
                reason = excluded.reason,
                dismissed_by = excluded.dismissed_by,
                dismissed_at = datetime('now')
        """, (yr, rn, reason, by))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': f'تم تخطي {len(pairs)} ايصال بنجاح'})


@app.route('/duplicate-receipts/restore', methods=['POST'])
@login_required
def restore_duplicate_receipt():
    year = (request.form.get('year') or '').strip()
    receipt_number = (request.form.get('receipt_number') or '').strip()
    if not year or not receipt_number:
        return jsonify({'success': False, 'message': 'بيانات ناقصة'})

    conn = get_db()
    conn.execute("""
        DELETE FROM duplicate_receipts_dismissed WHERE year=? AND receipt_number=?
    """, (year, receipt_number))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': f'تم استرجاع الايصال رقم {receipt_number} لقائمة المكررات'})


@app.route('/connected-users')
@login_required
def connected_users():
    conn = get_db()
    f_search = request.args.get('search', '').strip()
    page     = max(1, int(request.args.get('page', 1)))
    per_page = 50

    query  = "SELECT * FROM page_visits WHERE 1=1"
    params = []
    if f_search:
        query += " AND (username LIKE ? OR ip_address LIKE ? OR search_query LIKE ?)"
        params += [f'%{f_search}%'] * 3

    count_q = f"SELECT COUNT(*) as c FROM ({query}) AS q"
    total_records = conn.execute(count_q, params).fetchone()['c']
    total_pages   = max(1, (total_records + per_page - 1) // per_page)
    page          = min(page, total_pages)
    offset        = (page - 1) * per_page

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [per_page, offset]
    records = conn.execute(query, params).fetchall()

    stats = conn.execute("""
        SELECT COUNT(*) as total,
               COUNT(DISTINCT ip_address) as unique_ips,
               COUNT(DISTINCT username) as unique_users
        FROM page_visits
    """).fetchone()

    conn.close()
    return render_template('connected_users.html',
        records=records, f_search=f_search,
        page=page, total_pages=total_pages, total_records=total_records,
        stats=stats)


@app.route('/connected-users/delete-all', methods=['POST'])
@login_required
def delete_connected_users_all():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM page_visits").fetchone()['c']
    conn.execute("DELETE FROM page_visits")
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': f'تم حذف كل السجلات ({count} سجل) بنجاح'})


# ─── الدفاتر الفارغة والمستخدمة ────────────────────────────────────────────────
@app.route('/api/get-locations-by-book-name')
@login_required
def get_locations_by_book_name():
    """API endpoint: يرجع المواقع المتاحة لصنف دفتر معين"""
    book_name = request.args.get('book_name', '').strip()
    
    if not book_name or book_name == 'all':
        # إذا لم يتم تحديد صنف أو تم اختيار "جميع الأصناف"، ارجع جميع المواقع
        conn = get_db()
        locations = conn.execute("""
            SELECT DISTINCT location FROM book_dispense 
            WHERE location IS NOT NULL AND location != ''
            ORDER BY location
        """).fetchall()
        conn.close()
        return jsonify({
            'success': True,
            'locations': [row['location'] for row in locations]
        })
    
    # جلب المواقع لصنف دفتر محدد
    conn = get_db()
    locations = conn.execute("""
        SELECT DISTINCT bd.location 
        FROM book_dispense bd
        LEFT JOIN cold_books cb ON cb.id = bd.book_id
        WHERE cb.book_name = ? AND bd.location IS NOT NULL AND bd.location != ''
        ORDER BY bd.location
    """, (book_name,)).fetchall()
    conn.close()
    
    return jsonify({
        'success': True,
        'locations': [row['location'] for row in locations]
    })


@app.route('/empty-books')
@login_required
def empty_books():
    """
    عرض الدفاتر المصروفة:
    - الفارغة: مصروفة لكن لا توجد ايصالات عليها
    - المستخدمة: فيها ايصالات، وتعرض عدد الصفح الفارغة
    
    يدعم التصفية حسب:
    - صنف الدفتر (book_name) - من cold_books
    - الموقع (location) - من book_dispense
    """
    log_page_visit('empty_books')
    
    # جلب معاملات التصفية من الـ request
    filter_book_name = request.args.get('book_name', 'all').strip()
    filter_location = request.args.get('location', 'all').strip()
    
    conn = get_db()
    
    # جلب جميع أسماء الدفاتر المتاحة
    book_names = conn.execute("""
        SELECT DISTINCT book_name FROM cold_books 
        WHERE book_name IS NOT NULL
        ORDER BY book_name
    """).fetchall()
    book_names = [row['book_name'] for row in book_names]
    
    # جلب جميع المواقع المتاحة
    locations = conn.execute("""
        SELECT DISTINCT location FROM book_dispense 
        WHERE location IS NOT NULL AND location != ''
        ORDER BY location
    """).fetchall()
    locations = [row['location'] for row in locations]
    
    # جلب جميع الدفاتر المصروفة
    dispensed_books_data = conn.execute("""
        SELECT 
            d.id as dispense_id,
            d.book_id,
            d.book_index,
            d.serial_from,
            d.serial_to,
            d.location,
            d.dispensed_at,
            b.book_name,
            b.pages_per_book
        FROM book_dispense d
        LEFT JOIN cold_books b ON b.id = d.book_id
        ORDER BY d.dispensed_at DESC, d.id ASC
    """).fetchall()
    
    books_list = []
    
    for row in dispensed_books_data:
        book_id = row['book_id']
        book_index = row['book_index']
        serial_from = row['serial_from']
        serial_to = row['serial_to']
        location = row['location'] or '—'
        book_name = row['book_name'] or '—'
        pages_per_book = row['pages_per_book'] or 50
        
        # عد الايصالات في هذا النطاق
        receipts_count = 0
        
        if serial_from is not None and serial_to is not None:
            receipts_count = conn.execute("""
                SELECT COUNT(*) as cnt
                FROM weight_diff_receipts
                WHERE safe_int(receipt_number) BETWEEN ? AND ?
            """, (serial_from, serial_to)).fetchone()['cnt'] or 0
        
        # تحديد حالة الدفتر
        is_empty = (receipts_count == 0)
        remaining_pages = pages_per_book - receipts_count if receipts_count > 0 else pages_per_book
        
        # تطبيق التصفية
        should_include = True
        
        # تصفية حسب اسم الدفتر
        if filter_book_name != 'all':
            if book_name != filter_book_name:
                should_include = False
        
        # تصفية حسب الموقع
        if filter_location != 'all':
            if location != filter_location:
                should_include = False
        
        if should_include:
            books_list.append({
                'dispense_id': row['dispense_id'],
                'book_name': book_name,
                'book_index': row['book_index'] or 'N/A',
                'serial_range': f"{serial_from} — {serial_to}" if serial_from and serial_to else 'بدون نطاق',
                'location': location,
                'dispensed_at': row['dispensed_at'],
                'is_empty': is_empty,
                'receipts_count': receipts_count,
                'pages_per_book': pages_per_book,
                'remaining_pages': remaining_pages,
                'usage_percent': int((receipts_count / pages_per_book * 100)) if pages_per_book > 0 else 0
            })
    
    # فصل الفارغة عن المستخدمة
    empty = [b for b in books_list if b['is_empty']]
    used = [b for b in books_list if not b['is_empty']]
    
    conn.close()
    
    return render_template('empty_books.html',
        empty_books=empty,
        used_books=used,
        total_dispensed=len(books_list),
        total_empty=len(empty),
        total_used=len(used),
        book_names=book_names,
        locations=locations,
        filter_book_name=filter_book_name,
        filter_location=filter_location)


# ─── Run ─────────────────────────────────────────────────────────────────────

if USE_PG:
    init_db()   # gunicorn بيستورد الملف بدون __main__، فلازم نجهّز الجداول هنا

if __name__ == '__main__':
    init_db()
    import threading
    def open_browser():
        time.sleep(1.5)
        webbrowser.open(url)
    threading.Thread(target=open_browser, daemon=True).start()
    lan_ip = get_lan_ip()
    print("=" * 60)
    print(f"  النظام شغّال محلياً على   : http://127.0.0.1:5050")
    print(f"  النظام شغّال على الشبكة  : http://{lan_ip}:5050")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5050, debug=False, threaded=True)
