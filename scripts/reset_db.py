"""Delete the database and user-uploaded files, keeping the two seed media
files so the demo posts still look right after the next server start.

Run from the project root or from scripts/:
    python scripts/reset_db.py
"""
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE, "backend", "nava.db")
UPLOAD_DIR = os.path.join(BASE, "backend", "uploads")
KEEP = {"seed_arch.jpg", "seed_tar.wav", ".gitkeep"}

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
    print("پایگاه‌داده حذف شد:", DB_PATH)
else:
    print("پایگاه‌داده‌ای پیدا نشد (شاید قبلاً پاک شده).")

removed = 0
if os.path.isdir(UPLOAD_DIR):
    for name in os.listdir(UPLOAD_DIR):
        if name in KEEP:
            continue
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.isfile(path):
            os.remove(path)
            removed += 1
print(f"{removed} فایل آپلودی پاک شد.")
print("دفعه‌ی بعد که سرور را اجرا کنی، پایگاه‌داده دوباره با کاربران نمایشی ساخته می‌شود.")
