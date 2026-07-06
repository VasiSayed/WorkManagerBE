from django.contrib import admin

from .models import ChatMessage, ConnectionInvitation, Conversation, ConversationParticipant, Notification, UserBlock, UserReview


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "recipient", "actor", "is_read", "created_at")
    list_filter = ("type", "is_read")
    search_fields = ("title", "message", "recipient__email", "actor__email")


@admin.register(ConnectionInvitation)
class ConnectionInvitationAdmin(admin.ModelAdmin):
    list_display = ("sender", "recipient", "status", "rejected_until", "updated_at")
    list_filter = ("status",)
    search_fields = ("sender__email", "sender__username", "recipient__email", "recipient__username")


@admin.register(UserBlock)
class UserBlockAdmin(admin.ModelAdmin):
    list_display = ("blocker", "blocked", "created_at")
    search_fields = ("blocker__email", "blocked__email", "reason")


@admin.register(UserReview)
class UserReviewAdmin(admin.ModelAdmin):
    list_display = ("reviewer", "reviewed_user", "rating", "updated_at")
    list_filter = ("rating",)
    search_fields = ("reviewer__email", "reviewed_user__email", "text")


class ConversationParticipantInline(admin.TabularInline):
    model = ConversationParticipant
    extra = 0


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "title", "created_by", "updated_at")
    list_filter = ("type",)
    search_fields = ("title", "created_by__email")
    inlines = [ConversationParticipantInline]


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "sender", "attachment_type", "created_at")
    list_filter = ("attachment_type", "is_deleted")
    search_fields = ("text", "sender__email", "attachment_name")
