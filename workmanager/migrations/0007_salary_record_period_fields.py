from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workmanager", "0006_meeting_action_items_done_bool"),
    ]

    operations = [
        migrations.AddField(
            model_name="salaryrecord",
            name="period_start",
            field=models.DateField(blank=True, help_text="Actual earning period start date. Used mainly for freelance running ledger.", null=True),
        ),
        migrations.AddField(
            model_name="salaryrecord",
            name="period_end",
            field=models.DateField(blank=True, help_text="Actual earning period end date. Used mainly for freelance running ledger.", null=True),
        ),
    ]
