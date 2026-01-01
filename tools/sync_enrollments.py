# tools/sync_enrollments.py
from openpyxl import load_workbook
from django.db import transaction
from quiz.models import Participant, Quiz, Enrollment

PATH = "data/participants_import_ready.xlsx"

# domain -> quiz title
MAP = {
    "deputy": "وكيل",
    "counselor": "موجه طلابي",
    "activity": "رائد نشاط",
    "وكيل": "وكيل",
    "موجه طلابي": "موجه طلابي",
    "رائد نشاط": "رائد نشاط",
}

def norm(v):
    return str(v or "").strip()

wb = load_workbook(PATH, data_only=True)
ws = wb.worksheets[0]

headers = [norm(c.value) for c in next(ws.iter_rows(min_row=1, max_row=1))]
idx = {h: i for i, h in enumerate(headers)}

required = ["national_id", "domain", "is_allowed", "has_taken_exam"]
missing = [h for h in required if h not in idx]
if missing:
    raise SystemExit(f"Missing columns in xlsx: {missing} / headers={headers}")

quizzes_by_title = {q.title.strip(): q for q in Quiz.objects.all()}

def set_if_exists(obj, field, value):
    if hasattr(obj, field):
        setattr(obj, field, value)

created = 0
updated = 0
skipped_no_participant = 0
skipped_no_quiz = 0

rows = list(ws.iter_rows(min_row=2, values_only=True))

with transaction.atomic():
    for r in rows:
        national_id = norm(r[idx["national_id"]])
        dom = norm(r[idx["domain"]]).lower()
        wanted_title = MAP.get(dom, dom)
        qz = quizzes_by_title.get(wanted_title)

        if not qz:
            skipped_no_quiz += 1
            continue

        try:
            p = Participant.objects.get(national_id=national_id)
        except Participant.DoesNotExist:
            skipped_no_participant += 1
            continue

        # تحديث بيانات participant إن لزم
        is_allowed = bool(r[idx["is_allowed"]])
        has_taken = bool(r[idx["has_taken_exam"]])

        # خزّن المجال في locked_domain (لأن domain غير موجود)
        set_if_exists(p, "locked_domain", dom)
        set_if_exists(p, "is_allowed", is_allowed)
        set_if_exists(p, "has_taken_exam", has_taken)
        p.save(update_fields=[f for f in ["locked_domain","is_allowed","has_taken_exam"] if hasattr(p, f)])

        # إنشاء/تحديث Enrollment
        enr, was_created = Enrollment.objects.get_or_create(participant=p, quiz=qz)
        set_if_exists(enr, "is_allowed", is_allowed)
        set_if_exists(enr, "has_taken_exam", has_taken)
        enr.save()

        if was_created:
            created += 1
        else:
            updated += 1

print("Rows in sheet:", len(rows))
print("Enrollments created:", created)
print("Enrollments updated:", updated)
print("Skipped (no quiz match):", skipped_no_quiz)
print("Skipped (no participant):", skipped_no_participant)
print("Enrollments total:", Enrollment.objects.count())
