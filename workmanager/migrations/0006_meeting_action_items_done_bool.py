from django.db import migrations, models


def normalize_meeting_action_items(apps, schema_editor):
    Meeting = apps.get_model('workmanager', 'Meeting')
    for meeting in Meeting.objects.all().only('id', 'action_items'):
        value = meeting.action_items or []
        if isinstance(value, str):
            rows = [{"text": line.strip(), "done": False} for line in value.splitlines() if line.strip()]
        elif isinstance(value, list):
            rows = []
            for item in value:
                if isinstance(item, str):
                    text = item.strip()
                    done = False
                elif isinstance(item, dict):
                    text = str(item.get('text') or item.get('label') or item.get('title') or '').strip()
                    done = bool(item.get('done')) if 'done' in item else str(item.get('status') or '').lower() in {'done', 'completed', 'closed'}
                else:
                    continue
                if text:
                    rows.append({'text': text, 'done': done})
        else:
            rows = []
        if rows != value:
            meeting.action_items = rows
            meeting.save(update_fields=['action_items'])


class Migration(migrations.Migration):

    dependencies = [
        ('workmanager', '0005_meeting_action_items_and_salary_month'),
    ]

    operations = [
        migrations.AlterField(
            model_name='meeting',
            name='action_items',
            field=models.JSONField(blank=True, default=list, help_text='Point-wise MOM actions: [{text, done}]. done defaults to false.'),
        ),
        migrations.RunPython(normalize_meeting_action_items, migrations.RunPython.noop),
    ]
