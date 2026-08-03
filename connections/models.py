from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


User = settings.AUTH_USER_MODEL


def chat_upload_path(instance, filename):
    conversation_id = instance.conversation_id or "new"
    today = timezone.now().date()
    return f"chat/{conversation_id}/{today:%Y/%m/%d}/{filename}"


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Notification(TimeStampedModel):
    class Type(models.TextChoices):
        INVITATION_RECEIVED = "invitation_received", "Invitation Received"
        INVITATION_ACCEPTED = "invitation_accepted", "Invitation Accepted"
        INVITATION_REJECTED = "invitation_rejected", "Invitation Rejected"
        MESSAGE_RECEIVED = "message_received", "Message Received"
        GROUP_ADDED = "group_added", "Added To Group"
        BLOCKED = "blocked", "Blocked"
        REVIEW_RECEIVED = "review_received", "Review Received"
        NOTE_SHARED = "note_shared", "Note Shared"
        NOTE_UPDATED = "note_updated", "Shared Note Updated"
        NOTE_CHECKLIST_UPDATED = "note_checklist_updated", "Note Checklist Updated"

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="social_notifications")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="social_notifications_sent")
    type = models.CharField(max_length=40, choices=Type.choices)
    title = models.CharField(max_length=180)
    message = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["recipient", "is_read", "created_at"]),
            models.Index(fields=["recipient", "type"]),
        ]

    def __str__(self):
        return f"{self.title} → {self.recipient_id}"


class UserBlock(models.Model):
    blocker = models.ForeignKey(User, on_delete=models.CASCADE, related_name="blocks_created")
    blocked = models.ForeignKey(User, on_delete=models.CASCADE, related_name="blocks_received")
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["blocker", "blocked"], name="unique_user_block"),
            models.CheckConstraint(check=~Q(blocker=models.F("blocked")), name="prevent_self_block"),
        ]
        indexes = [
            models.Index(fields=["blocker", "blocked"]),
            models.Index(fields=["blocked", "blocker"]),
        ]

    def __str__(self):
        return f"{self.blocker_id} blocked {self.blocked_id}"


class ConnectionInvitation(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_connection_invitations")
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="received_connection_invitations")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    message = models.TextField(blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    rejected_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["sender", "recipient"], name="unique_connection_invitation_pair"),
            models.CheckConstraint(check=~Q(sender=models.F("recipient")), name="prevent_self_invitation"),
        ]
        indexes = [
            models.Index(fields=["sender", "recipient", "status"]),
            models.Index(fields=["recipient", "status"]),
        ]

    def __str__(self):
        return f"{self.sender_id} → {self.recipient_id}: {self.status}"

    @property
    def can_retry_at(self):
        if self.status == self.Status.REJECTED:
            return self.rejected_until
        return None


class UserReview(TimeStampedModel):
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews_written")
    reviewed_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews_received")
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    text = models.TextField(blank=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["reviewer", "reviewed_user"], name="unique_user_review"),
            models.CheckConstraint(check=~Q(reviewer=models.F("reviewed_user")), name="prevent_self_review"),
        ]
        indexes = [models.Index(fields=["reviewed_user", "rating"])]

    def __str__(self):
        return f"{self.rating}★ for {self.reviewed_user_id} by {self.reviewer_id}"


class Conversation(TimeStampedModel):
    class Type(models.TextChoices):
        DIRECT = "direct", "Direct"
        GROUP = "group", "Group"

    type = models.CharField(max_length=20, choices=Type.choices)
    title = models.CharField(max_length=180, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_conversations")
    participants = models.ManyToManyField(User, through="ConversationParticipant", related_name="social_conversations")

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [models.Index(fields=["type", "updated_at"])]

    def __str__(self):
        return self.title or f"{self.type} conversation #{self.pk}"


class ConversationParticipant(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="conversation_memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["joined_at", "id"]
        constraints = [models.UniqueConstraint(fields=["conversation", "user"], name="unique_conversation_participant")]
        indexes = [models.Index(fields=["user", "is_active"])]

    def __str__(self):
        return f"{self.user_id} in {self.conversation_id}"


class ChatMessage(models.Model):
    class AttachmentType(models.TextChoices):
        NONE = "none", "None"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        DOCUMENT = "document", "Document"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_chat_messages")
    text = models.TextField(blank=True)
    attachment = models.FileField(upload_to=chat_upload_path, null=True, blank=True)
    attachment_type = models.CharField(max_length=20, choices=AttachmentType.choices, default=AttachmentType.NONE)
    attachment_name = models.CharField(max_length=255, blank=True)
    video_duration_seconds = models.PositiveSmallIntegerField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
            models.Index(fields=["sender", "created_at"]),
        ]

    def __str__(self):
        return f"Message #{self.pk} in {self.conversation_id}"
