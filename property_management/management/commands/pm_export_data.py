import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import models

from property_management.management.transfer import FORMAT_VERSION, has_owner, ordered_models, owned_queryset


class Command(BaseCommand):
    help = "Export one user's rental-management data to a JSON file (load it elsewhere with pm_import_data)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Owner whose data is exported")
        parser.add_argument("--output", default="pm_data.json", help="File to write (default: pm_data.json)")

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(email__iexact=options["email"]).first()
        if not user:
            raise CommandError(f"No user with email {options['email']}.")

        tables = []
        file_count = 0
        for model in ordered_models():
            rows = list(owned_queryset(model, user))
            if not rows:
                continue
            data = json.loads(serializers.serialize("json", rows))
            for row in data:
                if has_owner(model):
                    row["fields"].pop("owner", None)
            file_fields = [f.name for f in model._meta.fields if isinstance(f, models.FileField)]
            file_count += sum(1 for row in rows for name in file_fields if getattr(row, name))
            tables.append({"model": model._meta.label_lower, "rows": data})
            self.stdout.write(f"  {model.__name__:24} {len(data)}")

        payload = {"format": FORMAT_VERSION, "owner_email": user.email, "tables": tables}
        out = Path(options["output"])
        out.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        total = sum(len(t["rows"]) for t in tables)
        self.stdout.write(self.style.SUCCESS(f"Wrote {total} rows to {out.resolve()}"))
        if file_count:
            self.stdout.write(self.style.WARNING(
                f"{file_count} uploaded file(s) are referenced. Copy the media/property_management folder to the server too."
            ))
