from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Let a user manage who gets which module (Settings > Users & Access). Does not give Django admin site access."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--remove", action="store_true", help="Take the admin role away instead")

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(email__iexact=options["email"]).first()
        if not user:
            raise CommandError(f"No user with email {options['email']}.")
        user.is_staff = not options["remove"]
        user.save(update_fields=["is_staff"])
        state = "now an app admin" if user.is_staff else "no longer an app admin"
        self.stdout.write(self.style.SUCCESS(f"{user.email} is {state}."))
