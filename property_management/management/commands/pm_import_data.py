import json
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, transaction

from property_management.management.transfer import FORMAT_VERSION, has_owner


class Command(BaseCommand):
    help = (
        "Load rental-management data written by pm_export_data. Rows keep their IDs; the owner is linked to the "
        "user with --email (default: the email stored in the file). Refuses to run if it would overwrite rows."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", nargs="?", default="pm_data.json")
        parser.add_argument("--email", help="Owner on this server (default: owner_email from the file)")
        parser.add_argument("--dry-run", action="store_true", help="Check everything, then roll back")

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"{path} not found.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("format") != FORMAT_VERSION:
            raise CommandError("Unsupported file format.")

        email = options.get("email") or payload["owner_email"]
        user = get_user_model().objects.filter(email__iexact=email).first()
        if not user:
            raise CommandError(f"No user with email {email} on this server. Register/log in once, then re-run.")

        # One-per-user rows (e.g. reminder settings) may already exist if the rental pages were opened
        # before importing; those are updated in place instead of inserted.
        singletons = {}
        for table in payload["tables"]:
            model = apps.get_model(table["model"])
            owner_field = next((f for f in model._meta.fields if f.name == "owner"), None)
            if owner_field is not None and owner_field.unique:
                existing = model.objects.filter(owner=user).first()
                if existing:
                    singletons[table["model"]] = existing

        # Never overwrite: loading a row whose ID already exists would replace that row.
        clashes = []
        for table in payload["tables"]:
            model = apps.get_model(table["model"])
            if table["model"] in singletons:
                continue
            ids = [row["pk"] for row in table["rows"]]
            taken = model.objects.filter(pk__in=ids).count()
            if taken:
                clashes.append(f"{model.__name__}: {taken} of {len(ids)} IDs already used")
        if clashes:
            raise CommandError(
                "Nothing imported — this server already has rental rows with the same IDs "
                "(was the data imported before?):\n  " + "\n  ".join(clashes)
            )

        touched = []
        with transaction.atomic():
            for table in payload["tables"]:
                model = apps.get_model(table["model"])
                rows = table["rows"]
                existing = singletons.get(table["model"])
                if existing:
                    for name, value in rows[0]["fields"].items():
                        if name not in ("created_at", "updated_at"):
                            setattr(existing, model._meta.get_field(name).attname, value)
                    existing.save()
                    self.stdout.write(f"  {model.__name__:24} 1 (updated existing)")
                    continue
                if has_owner(model):
                    for row in rows:
                        row["fields"]["owner"] = user.pk
                for obj in serializers.deserialize("json", json.dumps(rows)):
                    obj.save()
                touched.append(model)
                self.stdout.write(f"  {model.__name__:24} {len(rows)}")
            # Explicit IDs were inserted; make sure new rows created later get fresh IDs (PostgreSQL etc.).
            statements = connection.ops.sequence_reset_sql(no_style(), touched)
            if statements:
                with connection.cursor() as cursor:
                    for sql in statements:
                        cursor.execute(sql)
            if options["dry_run"]:
                transaction.set_rollback(True)

        total = sum(len(t["rows"]) for t in payload["tables"])
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"Dry run OK: {total} rows would be imported for {user.email}. Nothing saved."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Imported {total} rows for {user.email}."))
