from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from openpyxl import load_workbook

from quiz.models import Participant, Enrollment


def norm(v) -> str:
    return str(v or "").strip()


class Command(BaseCommand):
    help = "Sync enrollments from Excel file (participant national_id + domain)."

    def add_arguments(self, parser):
        parser.add_argument("--path", default="data/participants_import_ready.xlsx")
        parser.add_argument("--sheet", default=None)
        parser.add_argument("--col-nat", default="A")
        parser.add_argument("--col-domain", default="B")
        parser.add_argument("--strict-domains", action="store_true")

    def handle(self, *args, **opts):
        path = opts["path"]
        sheet_name = opts["sheet"]
        col_nat = opts["col_nat"]
        col_domain = opts["col_domain"]
        strict = opts["strict_domains"]

        MAP = {
            "deputy": "وكيل/وكيلة",
            "principal": "مدير/مديرة",
            "supervisor": "مشرف/مشرفة",
        }

        wb = load_workbook(path)
        ws = wb[sheet_name] if sheet_name else wb.active

        rows = ws.max_row
        self.stdout.write(f"Rows in sheet: {rows}")

        created = 0
        updated = 0
        skipped_no_participant = 0
        skipped_bad_domain = 0

        with transaction.atomic():
            for r in range(2, rows + 1):
                nat = norm(ws[f"{col_nat}{r}"].value)
                domain = norm(ws[f"{col_domain}{r}"].value).lower()

                if not nat:
                    continue

                if not domain or (strict and domain not in MAP):
                    skipped_bad_domain += 1
                    continue

                participant = Participant.objects.filter(national_id=nat).first()
                if not participant:
                    skipped_no_participant += 1
                    continue

                obj, was_created = Enrollment.objects.get_or_create(
                    participant=participant,
                    domain=domain,
                    defaults={"is_allowed": True},
                )
                if was_created:
                    created += 1
                else:
                    if not obj.is_allowed:
                        obj.is_allowed = True
                        obj.save(update_fields=["is_allowed"])
                        updated += 1

        self.stdout.write(f"Enrollments created: {created}")
        self.stdout.write(f"Enrollments updated: {updated}")
        self.stdout.write(f"Skipped (bad/no domain): {skipped_bad_domain}")
        self.stdout.write(f"Skipped (no participant): {skipped_no_participant}")
        self.stdout.write(f"Enrollments total: {Enrollment.objects.count()}")
