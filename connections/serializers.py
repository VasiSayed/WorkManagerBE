import os

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from .models import (
    ChatMessage,
    ConnectionInvitation,
    Conversation,
    ConversationParticipant,
    Notification,
    UserBlock,
    UserReview,
)

User = get_user_model()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".zip"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_DOCUMENT_SIZE = 25 * 1024 * 1024
MAX_VIDEO_SIZE = 80 * 1024 * 1024
MAX_VIDEO_SECONDS = 120


def user_display(user):
    return user.full_name or user.get_full_name() or user.username or user.email


def public_user_payload(user):
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name or user.get_full_name() or "",
        "profile_photo": user.profile_photo.url if getattr(user, "profile_photo", None) else None,
    }


def blocks_between(a, b):
    return UserBlock.objects.filter(Q(blocker=a, blocked=b) | Q(blocker=b, blocked=a)).exists()


def accepted_connection_between(a, b):
    return ConnectionInvitation.objects.filter(
        Q(sender=a, recipient=b, status=ConnectionInvitation.Status.ACCEPTED)
        | Q(sender=b, recipient=a, status=ConnectionInvitation.Status.ACCEPTED)
    ).exists()


class PublicUserSerializer(serializers.ModelSerializer):
    display_name = serializers.SerializerMethodField()
    profile_photo_url = serializers.SerializerMethodField()
    connection_status = serializers.SerializerMethodField()
    is_blocked_by_me = serializers.SerializerMethodField()
    has_blocked_me = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "full_name",
            "display_name",
            "profile_photo_url",
            "connection_status",
            "is_blocked_by_me",
            "has_blocked_me",
        ]

    def get_display_name(self, obj):
        return user_display(obj)

    def get_profile_photo_url(self, obj):
        if getattr(obj, "profile_photo", None):
            request = self.context.get("request")
            url = obj.profile_photo.url
            return request.build_absolute_uri(url) if request else url
        return None

    def get_is_blocked_by_me(self, obj):
        request = self.context.get("request")
        return bool(request and request.user.is_authenticated and UserBlock.objects.filter(blocker=request.user, blocked=obj).exists())

    def get_has_blocked_me(self, obj):
        request = self.context.get("request")
        return bool(request and request.user.is_authenticated and UserBlock.objects.filter(blocker=obj, blocked=request.user).exists())

    def get_connection_status(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return "none"
        user = request.user
        if obj.id == user.id:
            return "self"
        if UserBlock.objects.filter(blocker=user, blocked=obj).exists():
            return "blocked_by_me"
        if UserBlock.objects.filter(blocker=obj, blocked=user).exists():
            return "restricted"
        invitation = ConnectionInvitation.objects.filter(
            Q(sender=user, recipient=obj) | Q(sender=obj, recipient=user)
        ).order_by("-updated_at").first()
        if not invitation:
            return "none"
        if invitation.status == ConnectionInvitation.Status.ACCEPTED:
            return "connected"
        if invitation.status == ConnectionInvitation.Status.PENDING:
            return "pending_sent" if invitation.sender_id == user.id else "pending_received"
        if invitation.status == ConnectionInvitation.Status.REJECTED:
            return "rejected_sent" if invitation.sender_id == user.id else "rejected_received"
        return invitation.status


class NotificationSerializer(serializers.ModelSerializer):
    actor_user = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ["id", "actor", "actor_user", "type", "title", "message", "payload", "is_read", "created_at", "updated_at"]
        read_only_fields = fields

    def get_actor_user(self, obj):
        return public_user_payload(obj.actor) if obj.actor_id else None


class ConnectionInvitationSerializer(serializers.ModelSerializer):
    sender_user = serializers.SerializerMethodField()
    recipient_user = serializers.SerializerMethodField()
    direction = serializers.SerializerMethodField()
    can_retry_after = serializers.SerializerMethodField()

    class Meta:
        model = ConnectionInvitation
        fields = [
            "id",
            "sender",
            "sender_user",
            "recipient",
            "recipient_user",
            "direction",
            "status",
            "message",
            "responded_at",
            "rejected_until",
            "can_retry_after",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_sender_user(self, obj):
        return public_user_payload(obj.sender)

    def get_recipient_user(self, obj):
        return public_user_payload(obj.recipient)

    def get_direction(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return "outgoing" if obj.sender_id == request.user.id else "incoming"
        return "unknown"

    def get_can_retry_after(self, obj):
        if obj.status == ConnectionInvitation.Status.REJECTED and obj.rejected_until:
            return obj.rejected_until
        return None


class SendInvitationSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=180)
    message = serializers.CharField(required=False, allow_blank=True)

    def validate_identifier(self, value):
        identifier = value.strip()
        if not identifier:
            raise serializers.ValidationError("Enter username or email.")
        return identifier


class UserBlockSerializer(serializers.ModelSerializer):
    blocked_user = serializers.SerializerMethodField()

    class Meta:
        model = UserBlock
        fields = ["id", "blocked", "blocked_user", "reason", "created_at"]
        read_only_fields = ["id", "blocked_user", "created_at"]

    def get_blocked_user(self, obj):
        return public_user_payload(obj.blocked)

    def validate_blocked(self, value):
        request = self.context["request"]
        if value == request.user:
            raise serializers.ValidationError("You cannot block yourself.")
        return value

    def create(self, validated_data):
        request = self.context["request"]
        block, _ = UserBlock.objects.get_or_create(
            blocker=request.user,
            blocked=validated_data["blocked"],
            defaults={"reason": validated_data.get("reason", "")},
        )
        if validated_data.get("reason") and block.reason != validated_data["reason"]:
            block.reason = validated_data["reason"]
            block.save(update_fields=["reason"])
        return block


class UserReviewSerializer(serializers.ModelSerializer):
    reviewer_user = serializers.SerializerMethodField()
    reviewed_user_public = serializers.SerializerMethodField()

    class Meta:
        model = UserReview
        fields = ["id", "reviewer", "reviewer_user", "reviewed_user", "reviewed_user_public", "rating", "text", "created_at", "updated_at"]
        read_only_fields = ["id", "reviewer", "reviewer_user", "reviewed_user_public", "created_at", "updated_at"]

    def get_reviewer_user(self, obj):
        return public_user_payload(obj.reviewer)

    def get_reviewed_user_public(self, obj):
        return public_user_payload(obj.reviewed_user)

    def validate(self, attrs):
        request = self.context["request"]
        reviewed = attrs.get("reviewed_user") or getattr(self.instance, "reviewed_user", None)
        if not reviewed:
            raise serializers.ValidationError({"reviewed_user": "Reviewed user is required."})
        if reviewed == request.user:
            raise serializers.ValidationError({"reviewed_user": "You cannot review yourself."})
        if blocks_between(request.user, reviewed):
            raise serializers.ValidationError("You are restricted from reviewing this user.")
        if not accepted_connection_between(request.user, reviewed):
            raise serializers.ValidationError("You can review only accepted invitations / connected users.")
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        review, _ = UserReview.objects.update_or_create(
            reviewer=request.user,
            reviewed_user=validated_data["reviewed_user"],
            defaults={"rating": validated_data["rating"], "text": validated_data.get("text", "")},
        )
        Notification.objects.create(
            recipient=review.reviewed_user,
            actor=request.user,
            type=Notification.Type.REVIEW_RECEIVED,
            title="New profile review",
            message=f"{user_display(request.user)} reviewed your profile.",
            payload={"review_id": review.id, "user_id": request.user.id},
        )
        return review


class ConversationParticipantSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()

    class Meta:
        model = ConversationParticipant
        fields = ["id", "user", "role", "is_active", "joined_at"]
        read_only_fields = fields

    def get_user(self, obj):
        return public_user_payload(obj.user)


class ChatMessageSerializer(serializers.ModelSerializer):
    sender_user = serializers.SerializerMethodField()
    attachment_url = serializers.SerializerMethodField()

    class Meta:
        model = ChatMessage
        fields = [
            "id",
            "conversation",
            "sender",
            "sender_user",
            "text",
            "attachment",
            "attachment_url",
            "attachment_type",
            "attachment_name",
            "video_duration_seconds",
            "is_deleted",
            "created_at",
        ]
        read_only_fields = ["id", "conversation", "sender", "sender_user", "attachment_url", "attachment_name", "created_at", "is_deleted"]
        extra_kwargs = {"attachment": {"required": False, "allow_null": True}}

    def get_sender_user(self, obj):
        return public_user_payload(obj.sender)

    def get_attachment_url(self, obj):
        if not obj.attachment:
            return None
        request = self.context.get("request")
        url = obj.attachment.url
        return request.build_absolute_uri(url) if request else url

    def validate(self, attrs):
        text = (attrs.get("text") or "").strip()
        file = attrs.get("attachment")
        attachment_type = attrs.get("attachment_type") or ChatMessage.AttachmentType.NONE

        if not text and not file:
            raise serializers.ValidationError("Type a message or attach a file.")
        if file and attachment_type == ChatMessage.AttachmentType.NONE:
            raise serializers.ValidationError({"attachment_type": "Choose image, video, or document."})
        if not file and attachment_type != ChatMessage.AttachmentType.NONE:
            attrs["attachment_type"] = ChatMessage.AttachmentType.NONE
            return attrs

        if file:
            ext = os.path.splitext(file.name or "")[1].lower()
            if attachment_type == ChatMessage.AttachmentType.IMAGE:
                if ext not in IMAGE_EXTENSIONS:
                    raise serializers.ValidationError({"attachment": "Allowed images: jpg, jpeg, png, gif, webp."})
                if file.size > MAX_IMAGE_SIZE:
                    raise serializers.ValidationError({"attachment": "Image must be 10 MB or less."})
            elif attachment_type == ChatMessage.AttachmentType.VIDEO:
                if ext not in VIDEO_EXTENSIONS:
                    raise serializers.ValidationError({"attachment": "Allowed videos: mp4, mov, webm, m4v."})
                if file.size > MAX_VIDEO_SIZE:
                    raise serializers.ValidationError({"attachment": "Video must be 80 MB or less."})
                duration = attrs.get("video_duration_seconds")
                if duration is not None and int(duration) > MAX_VIDEO_SECONDS:
                    raise serializers.ValidationError({"video_duration_seconds": "Video must be 2 minutes or less."})
            elif attachment_type == ChatMessage.AttachmentType.DOCUMENT:
                if ext not in DOCUMENT_EXTENSIONS:
                    raise serializers.ValidationError({"attachment": "Allowed documents: pdf, doc, docx, xls, xlsx, ppt, pptx, txt, zip."})
                if file.size > MAX_DOCUMENT_SIZE:
                    raise serializers.ValidationError({"attachment": "Document must be 25 MB or less."})
            else:
                raise serializers.ValidationError({"attachment_type": "Invalid attachment type."})
            attrs["attachment_name"] = file.name
        return attrs


class ConversationSerializer(serializers.ModelSerializer):
    participants = ConversationParticipantSerializer(source="memberships", many=True, read_only=True)
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    display_title = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "type", "title", "display_title", "created_by", "participants", "last_message", "unread_count", "created_at", "updated_at"]
        read_only_fields = fields

    def get_last_message(self, obj):
        msg = obj.messages.order_by("-created_at", "-id").first()
        if not msg:
            return None
        return ChatMessageSerializer(msg, context=self.context).data

    def get_unread_count(self, obj):
        # Placeholder for future read receipts. Notifications already carry unread message alerts.
        return 0

    def get_display_title(self, obj):
        request = self.context.get("request")
        if obj.type == Conversation.Type.GROUP:
            return obj.title or f"Group #{obj.id}"
        if request and request.user.is_authenticated:
            other = obj.participants.exclude(id=request.user.id).first()
            if other:
                return user_display(other)
        return obj.title or "Direct chat"


class CreateConversationSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=Conversation.Type.choices)
    title = serializers.CharField(required=False, allow_blank=True, max_length=180)
    user_id = serializers.IntegerField(required=False)
    participant_ids = serializers.ListField(child=serializers.IntegerField(), required=False, allow_empty=True)

    def validate(self, attrs):
        request = self.context["request"]
        if attrs["type"] == Conversation.Type.DIRECT:
            user_id = attrs.get("user_id")
            if not user_id:
                raise serializers.ValidationError({"user_id": "Direct chat requires user_id."})
            try:
                other = User.objects.get(pk=user_id)
            except User.DoesNotExist:
                raise serializers.ValidationError({"user_id": "User not found."})
            if other == request.user:
                raise serializers.ValidationError({"user_id": "You cannot chat with yourself."})
            if blocks_between(request.user, other):
                raise serializers.ValidationError("Your Are restricted")
            if not accepted_connection_between(request.user, other):
                raise serializers.ValidationError("Personal chat is allowed only after accepted invitation.")
            attrs["other_user"] = other
        else:
            ids = list(dict.fromkeys([int(x) for x in attrs.get("participant_ids", []) if int(x) != request.user.id]))
            if len(ids) < 2:
                raise serializers.ValidationError({"participant_ids": "Group needs at least 3 users including you."})
            users = list(User.objects.filter(id__in=ids))
            if len(users) != len(ids):
                raise serializers.ValidationError({"participant_ids": "One or more users were not found."})
            attrs["participant_users"] = users
        return attrs
