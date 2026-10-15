# 🏢 نظام ERP — إدارة الموارد المؤسسية

## 📁 هيكل المشروع

```
erp\\\_system/
├── app.py                 # تطبيق Flask الرئيسي (Backend + Routes)
├── launcher.py            # مشغّل التطبيق كـ Desktop باستخدام PyWebView
├── requirements.txt       # المتطلبات
├── instance/
│   └── erp.db            # قاعدة بيانات SQLite (تُنشأ تلقائياً)
├── static/
│   ├── css/
│   │   └── style.css     # تصميم Glassmorphism ERP
│   └── js/
│       └── main.js       # JavaScript (البحث، الفلاتر، الـ Modals)
└── templates/
    ├── base.html          # القالب الأساسي (Sidebar + Layout)
    ├── login.html         # صفحة تسجيل الدخول
    ├── dashboard.html     # لوحة التحكم
    ├── users.html         # وحدة إدارة المستخدمين
    ├── sap.html           # وحدة معاملات SAP
    ├── secondary\\\_auth.html # بوابة المصادقة الثانوية
    ├── other\\\_users.html   # وحدة اليوزرات الأخرى (محمية)
    └── cold\\\_books.html    # وحدة دفاتر المخازن المبردة
```

## 🚀 تشغيل النظام

### 1\. تثبيت المتطلبات

```bash
pip install -r requirements.txt
```

### 2\. تشغيل كتطبيق Desktop (PyWebView)

```bash
python launcher.py
```

### 3\. تشغيل كتطبيق ويب فقط (بدون PyWebView)

```bash
python app.py
# ثم افتح المتصفح على: http://127.0.0.1:5050
```

## 🔐 بيانات الدخول الافتراضية

|الحقل|القيمة|
|-|-|
|اسم المستخدم|`admin`|
|كلمة المرور|`admin123`|
|كلمة المرور الثانوية|`secure456`|

## 📌 الوحدات المتاحة

|الوحدة|الوصف|
|-|-|
|📊 لوحة التحكم|إحصائيات ونظرة عامة على النظام|
|👥 إدارة المستخدمين|CRUD كامل مع فلتر الموقع والبحث|
|🔄 معاملات SAP|إدارة معاملات MyIDM/SAP|
|🔐 يوزرات أخرى|وحدة محمية بكلمة مرور ثانوية|
|❄️ دفاتر المخازن المبردة|إدارة الدفاتر مع ترتيب بالتاريخ|

## 🎨 التصميم

* **Glassmorphism** Sidebar شفاف مع Blur
* **Gradient** خلفية داكنة (أزرق + بنفسجي)
* **RTL** كامل + خط Cairo العربي
* **Animations** سلسة على البطاقات والـ Modals
* **Responsive** يعمل على جميع أحجام النوافذ


## ☁️ النشر على Railway مع PostgreSQL

1. ارفع المشروع على GitHub (من غير مجلد `instance/` — متحمّي بـ `.gitignore`).
2. في Railway: **New Project → Deploy from GitHub repo**، وبعدها **+ New → Database → Add PostgreSQL**.
3. من خدمة التطبيق → **Variables** ضيف:

|المتغير|القيمة|
|-|-|
|`DATABASE_URL`|`${{Postgres.DATABASE_URL}}` (Reference للـ Postgres)|
|`SECRET_KEY`|أي نص عشوائي طويل (اختياري لكن مُستحسن)|
|`APP_TIMEZONE`|`Africa/Cairo` (افتراضي — غيّره لو توقيتك مختلف)|

4. من **Settings → Networking → Generate Domain** عشان تاخد الرابط.
5. الجداول بتتعمل تلقائياً أول تشغيل. لنقل بياناتك الحالية من SQLite (مرة واحدة فقط) من جهازك:

```bash
pip install -r requirements.txt
export DATABASE_URL="<DATABASE_PUBLIC_URL من Postgres في Railway>"
python migrate_sqlite_to_postgres.py
```

> لو `DATABASE_URL` مش موجود، النظام بيشتغل على SQLite المحلي (`instance/erp.db`) زي ما كان بالظبط.
