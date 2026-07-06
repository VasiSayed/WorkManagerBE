# Generated manually for the social invitation/chat app.

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import connections.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Conversation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('type', models.CharField(choices=[('direct', 'Direct'), ('group', 'Group')], max_length=20)),
                ('title', models.CharField(blank=True, max_length=180)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_conversations', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-updated_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='ConnectionInvitation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('accepted', 'Accepted'), ('rejected', 'Rejected'), ('cancelled', 'Cancelled')], default='pending', max_length=20)),
                ('message', models.TextField(blank=True)),
                ('responded_at', models.DateTimeField(blank=True, null=True)),
                ('rejected_until', models.DateTimeField(blank=True, null=True)),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='received_connection_invitations', to=settings.AUTH_USER_MODEL)),
                ('sender', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sent_connection_invitations', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-updated_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='Notification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('type', models.CharField(choices=[('invitation_received', 'Invitation Received'), ('invitation_accepted', 'Invitation Accepted'), ('invitation_rejected', 'Invitation Rejected'), ('message_received', 'Message Received'), ('group_added', 'Added To Group'), ('blocked', 'Blocked'), ('review_received', 'Review Received')], max_length=40)),
                ('title', models.CharField(max_length=180)),
                ('message', models.TextField(blank=True)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('is_read', models.BooleanField(default=False)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='social_notifications_sent', to=settings.AUTH_USER_MODEL)),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='social_notifications', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='UserBlock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reason', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('blocked', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='blocks_received', to=settings.AUTH_USER_MODEL)),
                ('blocker', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='blocks_created', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='UserReview',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('rating', models.PositiveSmallIntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(5)])),
                ('text', models.TextField(blank=True)),
                ('reviewed_user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reviews_received', to=settings.AUTH_USER_MODEL)),
                ('reviewer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reviews_written', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-updated_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='ConversationParticipant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[('owner', 'Owner'), ('admin', 'Admin'), ('member', 'Member')], default='member', max_length=20)),
                ('is_active', models.BooleanField(default=True)),
                ('joined_at', models.DateTimeField(auto_now_add=True)),
                ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memberships', to='connections.conversation')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='conversation_memberships', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['joined_at', 'id'],
            },
        ),
        migrations.AddField(
            model_name='conversation',
            name='participants',
            field=models.ManyToManyField(related_name='social_conversations', through='connections.ConversationParticipant', to=settings.AUTH_USER_MODEL),
        ),
        migrations.CreateModel(
            name='ChatMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('text', models.TextField(blank=True)),
                ('attachment', models.FileField(blank=True, null=True, upload_to=connections.models.chat_upload_path)),
                ('attachment_type', models.CharField(choices=[('none', 'None'), ('image', 'Image'), ('video', 'Video'), ('document', 'Document')], default='none', max_length=20)),
                ('attachment_name', models.CharField(blank=True, max_length=255)),
                ('video_duration_seconds', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('is_deleted', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='connections.conversation')),
                ('sender', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sent_chat_messages', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddIndex(model_name='conversation', index=models.Index(fields=['type', 'updated_at'], name='connection_type_20e4c6_idx')),
        migrations.AddIndex(model_name='connectioninvitation', index=models.Index(fields=['sender', 'recipient', 'status'], name='connection_sender__b62fb7_idx')),
        migrations.AddIndex(model_name='connectioninvitation', index=models.Index(fields=['recipient', 'status'], name='connection_recipie_2fbad1_idx')),
        migrations.AddConstraint(model_name='connectioninvitation', constraint=models.UniqueConstraint(fields=('sender', 'recipient'), name='unique_connection_invitation_pair')),
        migrations.AddConstraint(model_name='connectioninvitation', constraint=models.CheckConstraint(condition=models.Q(('sender', models.F('recipient')), _negated=True), name='prevent_self_invitation')),
        migrations.AddIndex(model_name='notification', index=models.Index(fields=['recipient', 'is_read', 'created_at'], name='connection_recipie_12cc85_idx')),
        migrations.AddIndex(model_name='notification', index=models.Index(fields=['recipient', 'type'], name='connection_recipie_6d7e14_idx')),
        migrations.AddIndex(model_name='userblock', index=models.Index(fields=['blocker', 'blocked'], name='connection_blocker_56b3da_idx')),
        migrations.AddIndex(model_name='userblock', index=models.Index(fields=['blocked', 'blocker'], name='connection_blocked_a8cd2f_idx')),
        migrations.AddConstraint(model_name='userblock', constraint=models.UniqueConstraint(fields=('blocker', 'blocked'), name='unique_user_block')),
        migrations.AddConstraint(model_name='userblock', constraint=models.CheckConstraint(condition=models.Q(('blocker', models.F('blocked')), _negated=True), name='prevent_self_block')),
        migrations.AddIndex(model_name='userreview', index=models.Index(fields=['reviewed_user', 'rating'], name='connection_reviewe_e14c31_idx')),
        migrations.AddConstraint(model_name='userreview', constraint=models.UniqueConstraint(fields=('reviewer', 'reviewed_user'), name='unique_user_review')),
        migrations.AddConstraint(model_name='userreview', constraint=models.CheckConstraint(condition=models.Q(('reviewer', models.F('reviewed_user')), _negated=True), name='prevent_self_review')),
        migrations.AddIndex(model_name='conversationparticipant', index=models.Index(fields=['user', 'is_active'], name='connection_user_id_894f3f_idx')),
        migrations.AddConstraint(model_name='conversationparticipant', constraint=models.UniqueConstraint(fields=('conversation', 'user'), name='unique_conversation_participant')),
        migrations.AddIndex(model_name='chatmessage', index=models.Index(fields=['conversation', 'created_at'], name='connection_convers_91f2fb_idx')),
        migrations.AddIndex(model_name='chatmessage', index=models.Index(fields=['sender', 'created_at'], name='connection_sender__c2dafa_idx')),
    ]
