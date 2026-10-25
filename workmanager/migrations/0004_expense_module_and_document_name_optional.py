from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import workmanager.models


class Migration(migrations.Migration):
    dependencies = [
        ("workmanager", "0003_documentcategory_icon_image_note_visibility_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="document",
            name="name",
            field=models.CharField(blank=True, default="", max_length=180),
        ),
        migrations.CreateModel(
            name="ExpenseCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="%(class)s_items", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddConstraint(
            model_name="expensecategory",
            constraint=models.UniqueConstraint(fields=("owner", "name"), name="unique_expense_category_owner_name"),
        ),
        migrations.AddIndex(
            model_name="expensecategory",
            index=models.Index(fields=["owner", "is_active"], name="wm_exp_cat_owner_act_idx"),
        ),
        migrations.CreateModel(
            name="Expense",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("transaction_type", models.CharField(choices=[("given", "Given"), ("received", "Received")], default="given", max_length=12)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal("0.01"))])),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                ("date", models.DateField(default=django.utils.timezone.localdate)),
                ("category", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="expenses", to="workmanager.expensecategory")),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="%(class)s_items", to=settings.AUTH_USER_MODEL)),
                ("related_user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="related_expenses", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
        migrations.AddIndex(model_name="expense", index=models.Index(fields=["owner", "date"], name="wm_exp_owner_date_idx")),
        migrations.AddIndex(model_name="expense", index=models.Index(fields=["owner", "transaction_type"], name="wm_exp_owner_type_idx")),
        migrations.AddIndex(model_name="expense", index=models.Index(fields=["owner", "category"], name="wm_exp_owner_cat_idx")),
        migrations.CreateModel(
            name="ExpenseFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to=workmanager.models.expense_upload_path)),
                ("original_name", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expense", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="files", to="workmanager.expense")),
                ("uploaded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="uploaded_expense_files", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddIndex(model_name="expensefile", index=models.Index(fields=["expense", "created_at"], name="wm_ef_exp_create_idx")),
    ]
