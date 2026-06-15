from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from workmanager.models import EmailType, EmailVariable, WorkMember


class Command(BaseCommand):
    help = "Seed default email types and a Self member for a user."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Owner user email")

    def handle(self, *args, **options):
        User = get_user_model()
        user = User.objects.filter(email=options["email"]).first()
        if not user:
            raise CommandError("User not found")

        defaults = [
            ("Leave", "leave", "Leave request emails"),
            ("Work Update", "work-update", "Daily or weekly work update"),
            ("MOM", "mom", "Minutes of Meeting"),
            ("Task Update", "task-update", "Task progress update"),
            ("Salary Follow-up", "salary-follow-up", "Salary/payment follow-up"),
            ("Custom", "custom", "Custom email"),
        ]
        for name, code, description in defaults:
            EmailType.objects.get_or_create(owner=user, code=code, defaults={"name": name, "description": description})

        variables = [
            ("name", "Recipient Name", "person", "Name of recipient or contact"),
            ("date", "Date", "common", "Current or selected date"),
            ("project_name", "Project Name", "project", "Selected project name"),
            ("company_name", "Company Name", "job", "Company or job source name"),
            ("task_name", "Task Name", "task", "Task title/name"),
            ("meeting_title", "Meeting Title", "meeting", "Meeting title"),
            ("mom", "Minutes of Meeting", "meeting", "MOM content"),
            ("leave_date", "Leave Date", "leave", "Leave date"),
            ("leave_reason", "Leave Reason", "leave", "Leave reason"),
            ("month", "Month", "salary", "Salary/payment month"),
            ("amount", "Amount", "salary", "Salary/payment amount"),
            ("pending_amount", "Pending Amount", "salary", "Pending salary/payment amount"),
        ]
        for key, label, category, description in variables:
            EmailVariable.objects.get_or_create(
                owner=user, key=key,
                defaults={"label": label, "category": category, "description": description, "is_active": True},
            )

        WorkMember.objects.get_or_create(
            owner=user,
            member_type=WorkMember.MemberType.SELF,
            email=user.email,
            defaults={"name": user.full_name or user.email, "company": "Self"},
        )
        self.stdout.write(self.style.SUCCESS("Default data seeded."))
