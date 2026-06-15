# Manual migration for working-day salary rules and dynamic email variables.
from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workmanager", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="job",
            name="working_days",
            field=models.JSONField(blank=True, default=[0, 1, 2, 3, 4], help_text="Salary working weekdays. Monday=0 ... Sunday=6. Default is Monday-Friday."),
        ),
        migrations.AlterField(
            model_name="freelancesource",
            name="default_monthly_amount",
            field=models.DecimalField(blank=True, decimal_places=2, default=0, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="salaryrule",
            name="working_days",
            field=models.JSONField(blank=True, default=[0, 1, 2, 3, 4], help_text="Salary working weekdays. Monday=0 ... Sunday=6."),
        ),
        migrations.AlterField(
            model_name="salaryrule",
            name="working_days_per_month",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=5),
        ),
        migrations.AddField(
            model_name="emailtemplate",
            name="available_variables",
            field=models.JSONField(blank=True, default=list, help_text="Optional extra variable names to ask while sending this template."),
        ),
        migrations.AddField(
            model_name="emaillog",
            name="variable_values",
            field=models.JSONField(blank=True, default=dict, help_text="Values used to replace {{variables}} in email subject/body."),
        ),
    ]
