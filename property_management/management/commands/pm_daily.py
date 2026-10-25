from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from property_management.models import Agreement
from property_management.services import generate_charges, generate_reminders, month_start, today


class Command(BaseCommand):
    help = "Generate this month's rent/fixed charges and due renewal reminders (idempotent; safe to run daily via cron/Task Scheduler)."

    def add_arguments(self, parser):
        parser.add_argument("--month", help="YYYY-MM (defaults to the current month)")

    def handle(self, *args, **options):
        from property_management.services import parse_month

        period = parse_month(options.get("month")) if options.get("month") else month_start(today())
        owner_ids = Agreement.objects.values_list("owner_id", flat=True).distinct()
        for owner in get_user_model().objects.filter(id__in=owner_ids):
            charges = generate_charges(owner, period)
            reminders = generate_reminders(owner)
            self.stdout.write(f"{owner}: {charges} charge(s), {reminders} reminder(s)")
