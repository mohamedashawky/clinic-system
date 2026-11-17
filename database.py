import os
import sqlite3
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    CAIRO_TZ = ZoneInfo("Africa/Cairo")
except Exception:
    CAIRO_TZ = timezone(timedelta(hours=2))


def get_db_path() -> str:
    return os.getenv("DB_PATH", "clinic.db")


def get_cairo_now() -> datetime:
    return datetime.now(CAIRO_TZ)


def get_cairo_today_str() -> str:
    return get_cairo_now().strftime("%Y-%m-%d")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # 1. جدول بروفايل الدكتور وإعدادات العيادة
    cur.execute("""
    CREATE TABLE IF NOT EXISTS doctor_profile (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        name TEXT NOT NULL,
        specialty TEXT NOT NULL,
        bio TEXT NOT NULL,
        address TEXT NOT NULL,
        maps_url TEXT NOT NULL,
        phone TEXT NOT NULL,
        new_visit_price INTEGER NOT NULL DEFAULT 300,
        followup_price INTEGER NOT NULL DEFAULT 150,
        avg_consultation_mins INTEGER NOT NULL DEFAULT 15
    );
    """)

    # 2. جدول الشهادات والزمالات
    cur.execute("""
    CREATE TABLE IF NOT EXISTS doctor_certificates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        issuer TEXT NOT NULL,
        year TEXT NOT NULL,
        image_url TEXT NOT NULL
    );
    """)

    # 3. جدول مواعيد العمل الأسبوعية (0=Monday .. 6=Sunday حسب Python weekday)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS clinic_schedule (
        weekday_num INTEGER PRIMARY KEY,
        day_name TEXT NOT NULL,
        is_open INTEGER NOT NULL DEFAULT 1,
        start_time TEXT NOT NULL DEFAULT '17:00',
        end_time TEXT NOT NULL DEFAULT '22:00',
        max_patients INTEGER NOT NULL DEFAULT 25
    );
    """)

    # 4. جدول الإجازات الطارئة والأيام المغلقة
    cur.execute("""
    CREATE TABLE IF NOT EXISTS holiday_exceptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        holiday_date TEXT NOT NULL UNIQUE,
        reason TEXT NOT NULL
    );
    """)

    # 5. جدول الحجوزات وطابور الانتظار
    cur.execute("""
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_code TEXT NOT NULL UNIQUE,
        patient_name TEXT NOT NULL,
        patient_phone TEXT NOT NULL,
        appointment_date TEXT NOT NULL,
        visit_type TEXT NOT NULL,          -- 'New' | 'FollowUp'
        source TEXT NOT NULL,              -- 'Online' | 'WalkIn'
        queue_number INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'Waiting', -- 'Waiting' | 'InConsultation' | 'Completed' | 'NoShow' | 'Cancelled'
        created_at TEXT NOT NULL,
        UNIQUE(appointment_date, queue_number)
    );
    """)

    conn.commit()
    conn.close()


def seed_db():
    conn = get_connection()
    cur = conn.cursor()

    # إضافة بيانات الدكتور الافتراضية لو القاعدة فاضية
    cur.execute("SELECT COUNT(*) FROM doctor_profile")
    if cur.fetchone()[0] == 0:
        cur.execute("""
        INSERT INTO doctor_profile (
            id, name, specialty, bio, address, maps_url, phone,
            new_visit_price, followup_price, avg_consultation_mins
        ) VALUES (
            1,
            'د. أحمد محمود الشناوي',
            'استشاري أمراض الباطنة والقلب والأوعية الدموية',
            'خبرة أكثر من 15 عاماً في تشخيص وعلاج أمراض القلب وارتفاع ضغط الدم والسكري. عضو الجمعية المصرية لأمراض القلب، واستشاري سابق بمستشفيات القصر العيني.',
            'القاهرة - مدينة نصر - شارع عباس العقاد - برج الأطباء - الدور الرابع',
            'https://maps.google.com/?q=30.0561,31.3301',
            '01001234567',
            400,
            200,
            15
        );
        """)

    # إضافة جدول المواعيد الأسبوعي الافتراضي
    cur.execute("SELECT COUNT(*) FROM clinic_schedule")
    if cur.fetchone()[0] == 0:
        days = [
            (5, "السبت", 1, "17:00", "22:00", 25),
            (6, "الأحد", 1, "17:00", "22:00", 25),
            (0, "الاثنين", 1, "17:00", "22:00", 25),
            (1, "الثلاثاء", 1, "17:00", "22:00", 25),
            (2, "الأربعاء", 1, "17:00", "22:00", 25),
            (3, "الخميس", 1, "16:00", "21:00", 20),
            (4, "الجمعة", 0, "00:00", "00:00", 0),
        ]
        cur.executemany("""
        INSERT INTO clinic_schedule (weekday_num, day_name, is_open, start_time, end_time, max_patients)
        VALUES (?, ?, ?, ?, ?, ?);
        """, days)

    conn.commit()
    conn.close()