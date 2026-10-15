"""
نقل البيانات الحالية من SQLite (instance/erp.db) إلى PostgreSQL — مرة واحدة فقط.

الاستخدام (من جهازك، والمشروع متثبّت عليه requirements):

    set DATABASE_URL=postgresql://...        (Windows CMD)
    $env:DATABASE_URL="postgresql://..."     (PowerShell)
    export DATABASE_URL=postgresql://...     (Linux/Mac)

    python migrate_sqlite_to_postgres.py

ملاحظات:
  • استخدم DATABASE_PUBLIC_URL (الرابط العام) من Railway لو بتشغّل السكريبت من جهازك.
  • لو قاعدة PostgreSQL فيها بيانات بالفعل السكريبت هيقف — استخدم --force لمسحها واستبدالها.
  • السكريبت بيقرأ من SQLite فقط (مبيعدّلش في الملف الأصلي).
"""
import argparse
import os
import sqlite3
import sys

import psycopg2
from psycopg2.extras import execute_values

# ترتيب الجداول (مفيش FK، بس الترتيب ثابت للوضوح)
TABLES = ['admins', 'users', 'sap_transactions', 'other_users', 'cold_books',
          'book_dispense', 'weight_diff_receipts', 'duplicate_receipts_dismissed',
          'page_visits']
CHUNK = 1000


def to_int(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    t = str(v).strip()
    try:
        return int(t)
    except ValueError:
        try:
            f = float(t)
            return int(f) if f.is_integer() else None
        except ValueError:
            return None


def to_text(v):
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bytes):
        return v.decode('utf-8', 'replace')
    return str(v)


def main():
    ap = argparse.ArgumentParser(description='SQLite → PostgreSQL')
    ap.add_argument('--sqlite', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'erp.db'))
    ap.add_argument('--database-url', default=os.environ.get('DATABASE_URL', ''))
    ap.add_argument('--force', action='store_true', help='امسح بيانات PostgreSQL الحالية واستبدلها')
    args = ap.parse_args()

    if not args.database_url.startswith(('postgres://', 'postgresql://')):
        sys.exit('✗ لازم تحدد DATABASE_URL (متغير بيئة أو --database-url)')
    if not os.path.exists(args.sqlite):
        sys.exit(f'✗ ملف SQLite مش موجود: {args.sqlite}')

    # إنشاء الجداول بنفس المخطط اللي التطبيق بيستخدمه (مصدر واحد للمخطط)
    os.environ['DATABASE_URL'] = args.database_url
    from app import init_db
    init_db()

    src = sqlite3.connect(args.sqlite)
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(args.database_url)
    cur = dst.cursor()

    try:
        # أمان: ماتكتبش فوق بيانات موجودة إلا بـ --force
        existing = {}
        for t in TABLES:
            if t == 'admins':
                continue
            cur.execute(f'SELECT COUNT(*) FROM {t}')
            n = cur.fetchone()[0]
            if n:
                existing[t] = n
        if existing and not args.force:
            print('✗ قاعدة PostgreSQL فيها بيانات بالفعل:')
            for t, n in existing.items():
                print(f'    {t}: {n} صف')
            sys.exit('  لو عايز تستبدلها بالكامل شغّل السكريبت بـ --force')
        if args.force:
            cur.execute('TRUNCATE ' + ', '.join(TABLES) + ' RESTART IDENTITY')
        else:
            cur.execute('DELETE FROM admins')  # الأدمن الافتراضي اللي init_db عمله

        report = []
        for t in TABLES:
            cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = %s", (t,))
            pg_cols = dict(cur.fetchall())
            sq_cols = [r[1] for r in src.execute(f'PRAGMA table_info({t})')]
            if not sq_cols:
                report.append((t, 0, 0, 'مش موجود في SQLite'))
                continue
            cols = [c for c in sq_cols if c in pg_cols]
            lost = [c for c in sq_cols if c not in pg_cols]
            if lost:
                print(f'⚠ {t}: أعمدة مش موجودة في PostgreSQL ومش هتتنقل: {lost}')
            conv = [to_int if pg_cols[c] in ('integer', 'bigint', 'smallint') else to_text for c in cols]

            rows = src.execute(f'SELECT {", ".join(cols)} FROM {t} ORDER BY id').fetchall()
            bad = 0
            out = []
            for r in rows:
                vals = []
                for c, f, v in zip(cols, conv, r):
                    nv = f(v)
                    if nv is None and v is not None and v != '' and f is to_int:
                        bad += 1
                    vals.append(nv)
                out.append(tuple(vals))
            sql = f'INSERT INTO {t} ({", ".join(cols)}) VALUES %s'
            for i in range(0, len(out), CHUNK):
                execute_values(cur, sql, out[i:i + CHUNK])
            if 'id' in cols:
                cur.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                            f"COALESCE((SELECT MAX(id) FROM {t}), 1), (SELECT MAX(id) FROM {t}) IS NOT NULL)")
            cur.execute(f'SELECT COUNT(*) FROM {t}')
            report.append((t, len(rows), cur.fetchone()[0], f'{bad} قيمة رقمية غير صالحة اتحوّلت NULL' if bad else ''))

        dst.commit()
    except SystemExit:
        dst.rollback()
        raise
    except Exception as e:
        dst.rollback()
        sys.exit(f'✗ فشل النقل وتم التراجع عن كل حاجة: {e}')
    finally:
        src.close()

    print('\nالجدول'.ljust(34), 'SQLite'.rjust(8), 'PostgreSQL'.rjust(12))
    ok = True
    for t, a, b, note in report:
        flag = '✓' if a == b else '✗'
        ok &= (a == b)
        print(f'{flag} {t}'.ljust(32), str(a).rjust(8), str(b).rjust(12), ('  ' + note) if note else '')
    dst.close()
    print('\n✅ تم نقل البيانات بنجاح' if ok else '\n✗ فيه اختلاف في الأعداد — راجع الجدول أعلاه')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
