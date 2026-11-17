import hashlib
import hmac
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from database import get_cairo_now, get_cairo_today_str, get_connection, init_db, seed_db


# تحميل ملف .env يدوياً بدون مكتبات إضافية
def load_env_file(filepath: str = ".env"):
    if not os.path.exists(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env_file()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")
SECRET_KEY = os.getenv("SECRET_KEY", "default_secret_key_change_me").encode("utf-8")

# قفل لمنع تضارب أرقام الطابور عند الحجز المتزامن
queue_lock = threading.Lock()

# Rate Limiter بسيط لمنع السبام على الحجز (بحد أقصى 5 محاولات في الدقيقة لكل IP)
booking_attempts: dict[str, list[float]] = defaultdict(list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("static/uploads", exist_ok=True)
    init_db()
    seed_db()
    yield


app = FastAPI(title="Clinic Booking & Live Queue System", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ========================
# دوال الأمان والتحقق
# ========================
def create_admin_token() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(SECRET_KEY, ts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def verify_admin(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="غير مصرح بالدخول. برجاء تسجيل الدخول أولاً.")
    token = authorization.split(" ", 1)[1].strip()
    if "." not in token:
        raise HTTPException(status_code=401, detail="رمز الجلسة غير صالح.")
    ts_str, sig = token.split(".", 1)
    expected_sig = hmac.new(SECRET_KEY, ts_str.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise HTTPException(status_code=401, detail="رمز الجلسة غير صحيح.")
    # صلاحية الجلسة 24 ساعة
    if time.time() - int(ts_str) > 86400:
        raise HTTPException(status_code=401, detail="انتهت صلاحية الجلسة، برجاء تسجيل الدخول مرة أخرى.")


def check_rate_limit(ip: str):
    now = time.time()
    recent = [t for t in booking_attempts[ip] if now - t < 60]
    if len(recent) >= 5:
        raise HTTPException(status_code=429, detail="محاولات حجز كثيرة في وقت قصير. برجاء الانتظار دقيقة.")
    recent.append(now)
    booking_attempts[ip] = recent


def normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"[\s\-\(\)]", "", phone.strip())
    if not re.match(r"^(\+20|0)?1[0125][0-9]{8}$", cleaned) and not re.match(r"^[0-9]{8,15}$", cleaned):
        raise HTTPException(status_code=400, detail="رقم الموبايل غير صحيح. برجاء إدخال رقم موبايل صحيح.")
    return cleaned


def generate_unique_booking_code(cur: sqlite3.Cursor) -> str:
    for _ in range(20):
        code = f"CLN-{random.randint(1000, 9999)}"
        cur.execute("SELECT 1 FROM appointments WHERE booking_code = ?", (code,))
        if not cur.fetchone():
            return code
    return f"CLN-{uuid.uuid4().hex[:6].upper()}"


# ========================
# نماذج البيانات (Schemas)
# ========================
class BookingRequest(BaseModel):
    patient_name: str = Field(..., min_length=2, max_length=100)
    patient_phone: str = Field(..., min_length=8, max_length=20)
    appointment_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    visit_type: str = Field(..., pattern=r"^(New|FollowUp)$")


class WalkInRequest(BaseModel):
    patient_name: str = Field(..., min_length=2, max_length=100)
    patient_phone: str = Field(default="01000000000", max_length=20)
    visit_type: str = Field(..., pattern=r"^(New|FollowUp)$")


class StatusUpdateRequest(BaseModel):
    status: str = Field(..., pattern=r"^(Waiting|InConsultation|Completed|NoShow|Cancelled)$")


class LoginRequest(BaseModel):
    password: str


class ProfileUpdateRequest(BaseModel):
    name: str = Field(..., min_length=2)
    specialty: str = Field(..., min_length=2)
    bio: str = Field(..., min_length=5)
    address: str = Field(..., min_length=5)
    maps_url: str = Field(..., min_length=5)
    phone: str = Field(..., min_length=5)
    new_visit_price: int = Field(..., ge=0)
    followup_price: int = Field(..., ge=0)
    avg_consultation_mins: int = Field(..., ge=1, le=120)


class ScheduleDayItem(BaseModel):
    weekday_num: int = Field(..., ge=0, le=6)
    is_open: int = Field(..., ge=0, le=1)
    start_time: str
    end_time: str
    max_patients: int = Field(..., ge=0, le=200)


class HolidayRequest(BaseModel):
    holiday_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str = Field(default="إجازة بالعيادة", min_length=2, max_length=120)


# ========================
# صفحات HTML
# ========================
@app.get("/")
def serve_patient_portal():
    return FileResponse("static/index.html")


@app.get("/admin")
def serve_admin_portal():
    return FileResponse("static/admin.html")


# ========================
# مسارات المريض (Public APIs)
# ========================
@app.get("/api/clinic-info")
def get_clinic_info():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM doctor_profile WHERE id = 1")
    profile = dict(cur.fetchone())

    cur.execute("SELECT * FROM doctor_certificates ORDER BY id DESC")
    certificates = [dict(r) for r in cur.fetchall()]

    # ترتيب الأيام من السبت للجمعة
    order_map = {5: 1, 6: 2, 0: 3, 1: 4, 2: 5, 3: 6, 4: 7}
    cur.execute("SELECT * FROM clinic_schedule")
    schedule = sorted([dict(r) for r in cur.fetchall()], key=lambda x: order_map.get(x["weekday_num"], 99))

    conn.close()
    return {"profile": profile, "certificates": certificates, "schedule": schedule}


@app.get("/api/available-dates")
def get_available_dates():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM clinic_schedule")
    schedule_map = {r["weekday_num"]: dict(r) for r in cur.fetchall()}

    cur.execute("SELECT holiday_date, reason FROM holiday_exceptions")
    holidays_map = {r["holiday_date"]: r["reason"] for r in cur.fetchall()}

    today = get_cairo_now().date()
    dates_list = []

    for offset in range(14):
        d = today + timedelta(days=offset)
        d_str = d.strftime("%Y-%m-%d")
        wk = d.weekday()
        day_cfg = schedule_map.get(wk)

        cur.execute("""
            SELECT COUNT(*) FROM appointments
            WHERE appointment_date = ? AND status != 'Cancelled'
        """, (d_str,))
        booked_count = cur.fetchone()[0]

        is_available = True
        reason = "متاح للحجز"
        max_patients = day_cfg["max_patients"] if day_cfg else 0

        if not day_cfg or day_cfg["is_open"] == 0:
            is_available = False
            reason = "إجازة أسبوعية"
        elif d_str in holidays_map:
            is_available = False
            reason = holidays_map[d_str]
        elif booked_count >= max_patients:
            is_available = False
            reason = "اكتمل العدد"

        dates_list.append({
            "date": d_str,
            "day_name": day_cfg["day_name"] if day_cfg else "",
            "start_time": day_cfg["start_time"] if day_cfg else "",
            "end_time": day_cfg["end_time"] if day_cfg else "",
            "is_available": is_available,
            "reason": reason,
            "booked_count": booked_count,
            "max_patients": max_patients,
            "remaining": max(0, max_patients - booked_count) if is_available else 0,
        })

    conn.close()
    return {"dates": dates_list}


@app.post("/api/appointments/book")
def book_appointment(payload: BookingRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip)

    phone = normalize_phone(payload.patient_phone)
    today_str = get_cairo_today_str()

    if payload.appointment_date < today_str:
        raise HTTPException(status_code=400, detail="لا يمكن الحجز في تاريخ سابق.")

    with queue_lock:
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # التحقق من جدول العيادة
            y, m, d = map(int, payload.appointment_date.split("-"))
            from datetime import date
            wk = date(y, m, d).weekday()

            cur.execute("SELECT * FROM clinic_schedule WHERE weekday_num = ?", (wk,))
            day_cfg = cur.fetchone()
            if not day_cfg or day_cfg["is_open"] == 0:
                raise HTTPException(status_code=400, detail="العيادة مغلقة في هذا اليوم (إجازة أسبوعية).")

            # التحقق من الإجازات الطارئة
            cur.execute("SELECT reason FROM holiday_exceptions WHERE holiday_date = ?", (payload.appointment_date,))
            holiday = cur.fetchone()
            if holiday:
                raise HTTPException(status_code=400, detail=f"العيادة مغلقة في هذا اليوم: {holiday['reason']}")

            # التحقق من عدم تكرار الحجز لنفس الموبايل في نفس اليوم
            cur.execute("""
                SELECT booking_code, queue_number FROM appointments
                WHERE appointment_date = ? AND patient_phone = ? AND status NOT IN ('Cancelled', 'NoShow')
            """, (payload.appointment_date, phone))
            existing = cur.fetchone()
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=f"يوجد حجز مسجل بالفعل لهذا الرقم في نفس اليوم (كود: {existing['booking_code']} - رقم الدور: {existing['queue_number']})."
                )

            # التحقق من الحد الأقصى للحجوزات
            cur.execute("""
                SELECT COUNT(*) FROM appointments
                WHERE appointment_date = ? AND status != 'Cancelled'
            """, (payload.appointment_date,))
            active_count = cur.fetchone()[0]
            if active_count >= day_cfg["max_patients"]:
                raise HTTPException(status_code=400, detail="عذراً، اكتمل العدد المتاح للحجز في هذا اليوم.")

            # توليد رقم الدور التسلسلي بأمان
            cur.execute("""
                SELECT COALESCE(MAX(queue_number), 0) FROM appointments
                WHERE appointment_date = ?
            """, (payload.appointment_date,))
            next_queue = cur.fetchone()[0] + 1
            booking_code = generate_unique_booking_code(cur)
            now_str = get_cairo_now().strftime("%Y-%m-%d %H:%M:%S")

            cur.execute("""
                INSERT INTO appointments (
                    booking_code, patient_name, patient_phone, appointment_date,
                    visit_type, source, queue_number, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'Online', ?, 'Waiting', ?)
            """, (
                booking_code, payload.patient_name.strip(), phone,
                payload.appointment_date, payload.visit_type, next_queue, now_str
            ))

            conn.commit()
            return {
                "message": "تم تأكيد حجزك بنجاح",
                "booking_code": booking_code,
                "queue_number": next_queue,
                "appointment_date": payload.appointment_date,
                "patient_name": payload.patient_name.strip(),
                "visit_type": payload.visit_type
            }
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"حدث خطأ أثناء الحجز: {str(e)}")
        finally:
            conn.close()


@app.get("/api/queue/status")
def get_queue_status(query: str):
    q = query.strip().upper()
    if not q:
        raise HTTPException(status_code=400, detail="برجاء إدخال رقم الموبايل أو كود الحجز.")

    today_str = get_cairo_today_str()
    conn = get_connection()
    cur = conn.cursor()

    # البحث أولاً في حجوزات اليوم، ثم في أقرب حجز قادم
    cur.execute("""
        SELECT * FROM appointments
        WHERE (UPPER(booking_code) = ? OR patient_phone = ?)
          AND appointment_date >= ?
          AND status != 'Cancelled'
        ORDER BY
          CASE WHEN appointment_date = ? THEN 0 ELSE 1 END,
          appointment_date ASC,
          id DESC
        LIMIT 1
    """, (q, query.strip(), today_str, today_str))
    appt = cur.fetchone()

    if not appt:
        conn.close()
        raise HTTPException(status_code=404, detail="لم يتم العثور على حجز نشط بهذا الرقم أو الكود.")

    appt_dict = dict(appt)
    appt_date = appt_dict["appointment_date"]

    # جلب متوسط مدة الكشف
    cur.execute("SELECT avg_consultation_mins FROM doctor_profile WHERE id = 1")
    avg_mins = cur.fetchone()["avg_consultation_mins"]

    # الرقم الحالي داخل غرفة الكشف في نفس اليوم
    cur.execute("""
        SELECT queue_number FROM appointments
        WHERE appointment_date = ? AND status = 'InConsultation'
        ORDER BY queue_number DESC LIMIT 1
    """, (appt_date,))
    in_room_row = cur.fetchone()
    current_in_room = in_room_row["queue_number"] if in_room_row else 0

    # عدد المرضى المنتظرين قبل هذا المريض
    cur.execute("""
        SELECT COUNT(*) FROM appointments
        WHERE appointment_date = ?
          AND status = 'Waiting'
          AND queue_number < ?
    """, (appt_date, appt_dict["queue_number"]))
    people_ahead = cur.fetchone()[0]

    # حساب الوقت المتوقع
    extra_slot = 1 if (current_in_room > 0 and appt_dict["status"] == "Waiting") else 0
    estimated_wait_mins = (people_ahead + extra_slot) * avg_mins if appt_dict["status"] == "Waiting" else 0

    conn.close()
    return {
        "appointment": appt_dict,
        "is_today": appt_date == today_str,
        "current_in_consultation": current_in_room,
        "people_ahead": people_ahead,
        "estimated_wait_mins": estimated_wait_mins
    }


# ========================
# مسارات الإدارة والسكرتارية (Admin APIs)
# ========================
@app.post("/api/auth/login")
def admin_login(payload: LoginRequest):
    if not hmac.compare_digest(payload.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="كلمة المرور غير صحيحة.")
    return {"token": create_admin_token()}


@app.get("/api/admin/queue", dependencies=[Depends(verify_admin)])
def admin_get_queue(date: Optional[str] = None):
    target_date = date or get_cairo_today_str()
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT * FROM appointments
        WHERE appointment_date = ?
        ORDER BY queue_number ASC
    """, (target_date,))
    rows = [dict(r) for r in cur.fetchall()]

    stats = {
        "total": len([r for r in rows if r["status"] != "Cancelled"]),
        "waiting": len([r for r in rows if r["status"] == "Waiting"]),
        "in_consultation": next((r["queue_number"] for r in rows if r["status"] == "InConsultation"), 0),
        "completed": len([r for r in rows if r["status"] == "Completed"]),
        "no_show": len([r for r in rows if r["status"] == "NoShow"]),
    }

    conn.close()
    return {"date": target_date, "stats": stats, "appointments": rows}


@app.post("/api/admin/queue/walk-in", dependencies=[Depends(verify_admin)])
def admin_add_walkin(payload: WalkInRequest):
    today_str = get_cairo_today_str()
    phone = payload.patient_phone.strip() or "01000000000"

    with queue_lock:
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            cur.execute("""
                SELECT COALESCE(MAX(queue_number), 0) FROM appointments
                WHERE appointment_date = ?
            """, (today_str,))
            next_queue = cur.fetchone()[0] + 1
            booking_code = generate_unique_booking_code(cur)
            now_str = get_cairo_now().strftime("%Y-%m-%d %H:%M:%S")

            cur.execute("""
                INSERT INTO appointments (
                    booking_code, patient_name, patient_phone, appointment_date,
                    visit_type, source, queue_number, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'WalkIn', ?, 'Waiting', ?)
            """, (
                booking_code, payload.patient_name.strip(), phone,
                today_str, payload.visit_type, next_queue, now_str
            ))

            conn.commit()
            return {"message": "تم إضافة المريض للطابور", "queue_number": next_queue, "booking_code": booking_code}
        finally:
            conn.close()


@app.patch("/api/admin/appointments/{appt_id}/status", dependencies=[Depends(verify_admin)])
def admin_update_status(appt_id: int, payload: StatusUpdateRequest):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,))
    appt = cur.fetchone()
    if not appt:
        conn.close()
        raise HTTPException(status_code=404, detail="الحجز غير موجود.")

    # لو المريض دخل الكشف، نقفل تلقائياً أي مريض تاني كان حالته "بالداخل" في نفس اليوم
    if payload.status == "InConsultation":
        cur.execute("""
            UPDATE appointments
            SET status = 'Completed'
            WHERE appointment_date = ? AND status = 'InConsultation' AND id != ?
        """, (appt["appointment_date"], appt_id))

    cur.execute("UPDATE appointments SET status = ? WHERE id = ?", (payload.status, appt_id))
    conn.commit()
    conn.close()
    return {"message": "تم تحديث حالة المريض بنجاح"}


@app.put("/api/admin/profile", dependencies=[Depends(verify_admin)])
def admin_update_profile(payload: ProfileUpdateRequest):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE doctor_profile
        SET name = ?, specialty = ?, bio = ?, address = ?, maps_url = ?,
            phone = ?, new_visit_price = ?, followup_price = ?, avg_consultation_mins = ?
        WHERE id = 1
    """, (
        payload.name.strip(), payload.specialty.strip(), payload.bio.strip(),
        payload.address.strip(), payload.maps_url.strip(), payload.phone.strip(),
        payload.new_visit_price, payload.followup_price, payload.avg_consultation_mins
    ))
    conn.commit()
    conn.close()
    return {"message": "تم تحديث بيانات الدكتور بنجاح"}


@app.post("/api/admin/certificates", dependencies=[Depends(verify_admin)])
async def admin_upload_certificate(
    title: str = Form(...),
    issuer: str = Form(...),
    year: str = Form(...),
    image: UploadFile = File(...)
):
    ext = os.path.splitext(image.filename or "")[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(status_code=400, detail="صيغة الصورة غير مدعومة. استخدم JPG أو PNG أو WebP.")

    filename = f"cert_{uuid.uuid4().hex[:10]}{ext}"
    filepath = os.path.join("static", "uploads", filename)

    content = await image.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="حجم الصورة كبير جداً (الحد الأقصى 5 ميجابايت).")

    with open(filepath, "wb") as f:
        f.write(content)

    image_url = f"/static/uploads/{filename}"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO doctor_certificates (title, issuer, year, image_url)
        VALUES (?, ?, ?, ?)
    """, (title.strip(), issuer.strip(), year.strip(), image_url))
    conn.commit()
    conn.close()
    return {"message": "تم رفع الشهادة بنجاح", "image_url": image_url}


@app.delete("/api/admin/certificates/{cert_id}", dependencies=[Depends(verify_admin)])
def admin_delete_certificate(cert_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT image_url FROM doctor_certificates WHERE id = ?", (cert_id,))
    row = cur.fetchone()
    if row:
        disk_path = row["image_url"].lstrip("/")
        if os.path.exists(disk_path):
            try:
                os.remove(disk_path)
            except OSError:
                pass
        cur.execute("DELETE FROM doctor_certificates WHERE id = ?", (cert_id,))
        conn.commit()
    conn.close()
    return {"message": "تم حذف الشهادة"}


@app.put("/api/admin/schedule", dependencies=[Depends(verify_admin)])
def admin_update_schedule(days: List[ScheduleDayItem]):
    conn = get_connection()
    cur = conn.cursor()
    for d in days:
        cur.execute("""
            UPDATE clinic_schedule
            SET is_open = ?, start_time = ?, end_time = ?, max_patients = ?
            WHERE weekday_num = ?
        """, (d.is_open, d.start_time, d.end_time, d.max_patients, d.weekday_num))
    conn.commit()
    conn.close()
    return {"message": "تم تحديث جدول مواعيد العيادة"}


@app.get("/api/admin/holidays", dependencies=[Depends(verify_admin)])
def admin_get_holidays():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM holiday_exceptions ORDER BY holiday_date ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"holidays": rows}


@app.post("/api/admin/holidays", dependencies=[Depends(verify_admin)])
def admin_add_holiday(payload: HolidayRequest):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO holiday_exceptions (holiday_date, reason)
            VALUES (?, ?)
        """, (payload.holiday_date, payload.reason.strip()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="هذا التاريخ مسجل كإجازة بالفعل.")
    conn.close()
    return {"message": "تم إغلاق اليوم بنجاح"}


@app.delete("/api/admin/holidays/{holiday_id}", dependencies=[Depends(verify_admin)])
def admin_delete_holiday(holiday_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM holiday_exceptions WHERE id = ?", (holiday_id,))
    conn.commit()
    conn.close()
    return {"message": "تم إعادة فتح اليوم"}