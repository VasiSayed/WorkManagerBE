from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    ChatMessage,
    ConnectionInvitation,
    Conversation,
    ConversationParticipant,
    Notification,
    UserBlock,
    UserReview,
)
from .serializers import (
    ChatMessageSerializer,
    ConnectionInvitationSerializer,
    ConversationSerializer,
    CreateConversationSerializer,
    NotificationSerializer,
    PublicUserSerializer,
    SendInvitationSerializer,
    UserBlockSerializer,
    UserReviewSerializer,
    accepted_connection_between,
    blocks_between,
    public_user_payload,
    user_display,
)

User = get_user_model()


def notify(recipient, actor, type_value, title, message="", payload=None):
    if recipient and actor and recipient.id == actor.id:
        return None
    return Notification.objects.create(
        recipient=recipient,
        actor=actor,
        type=type_value,
        title=title,
        message=message,
        payload=payload or {},
    )


def current_work_for(user):
    try:
        from workmanager.models import Job
    except Exception:
        return None
    job = Job.objects.filter(owner=user, status__in=["active", "probation"]).order_by("-joining_date", "-id").first()
    if not job:
        return None
    return {
        "company_name": job.company_name,
        "job_title": job.job_title,
        "current_designation": job.current_designation,
        "status": job.status,
        "joining_date": job.joining_date,
    }


def accepted_connections_qs(user):
    return ConnectionInvitation.objects.filter(
        Q(sender=user, status=ConnectionInvitation.Status.ACCEPTED)
        | Q(recipient=user, status=ConnectionInvitation.Status.ACCEPTED)
    )


def connected_user_ids(user):
    ids = set()
    for invitation in accepted_connections_qs(user).only("sender_id", "recipient_id"):
        ids.add(invitation.recipient_id if invitation.sender_id == user.id else invitation.sender_id)
    return ids


def is_connected_to_any(target, users):
    user_ids = [u.id if hasattr(u, "id") else int(u) for u in users]
    return ConnectionInvitation.objects.filter(status=ConnectionInvitation.Status.ACCEPTED).filter(
        Q(sender=target, recipient_id__in=user_ids) | Q(recipient=target, sender_id__in=user_ids)
    ).exists()


def blocked_pair_exists(user_a, user_b):
    return UserBlock.objects.filter(Q(blocker=user_a, blocked=user_b) | Q(blocker=user_b, blocked=user_a)).exists()


def validate_group_members(actor, participant_users):
    selected = [actor, *participant_users]
    for index, user_a in enumerate(selected):
        for user_b in selected[index + 1 :]:
            if blocked_pair_exists(user_a, user_b):
                raise ValueError(f"{user_display(user_b)} is restricted from this group.")
    for target in participant_users:
        others = [u for u in selected if u.id != target.id]
        if not is_connected_to_any(target, others):
            raise ValueError(f"{user_display(target)} cannot be added because no accepted invitation links them to this group.")


class UserSearchAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        qs = User.objects.exclude(id=request.user.id).order_by("full_name", "email")
        if q:
            qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(full_name__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
        else:
            qs = qs.none()
        qs = qs[:25]
        return Response(PublicUserSerializer(qs, many=True, context={"request": request}).data)


class UserProfileAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, user_id):
        target = get_object_or_404(User, pk=user_id)
        blocked_by_me = UserBlock.objects.filter(blocker=request.user, blocked=target).exists()
        has_blocked_me = UserBlock.objects.filter(blocker=target, blocked=request.user).exists()
        if target != request.user and (blocked_by_me or has_blocked_me):
            return Response({
                "restricted": True,
                "message": "Your Are restricted",
                "user": public_user_payload(target) if blocked_by_me else {"id": target.id},
                "is_blocked_by_me": blocked_by_me,
                "has_blocked_me": has_blocked_me,
            })

        profile = PublicUserSerializer(target, context={"request": request}).data
        reviews = UserReview.objects.filter(reviewed_user=target).select_related("reviewer")[:20]
        groups = Conversation.objects.filter(
            type=Conversation.Type.GROUP,
            memberships__user=request.user,
            memberships__is_active=True,
        ).filter(memberships__user=target, memberships__is_active=True).distinct()[:10]
        return Response({
            "restricted": False,
            "user": profile,
            "currently_working_at": current_work_for(target),
            "reviews": UserReviewSerializer(reviews, many=True, context={"request": request}).data,
            "groups_in_common": ConversationSerializer(groups, many=True, context={"request": request}).data,
        })


class InvitationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = ConnectionInvitationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = ConnectionInvitation.objects.filter(Q(sender=user) | Q(recipient=user)).select_related("sender", "recipient")
        status_filter = self.request.query_params.get("status")
        direction = self.request.query_params.get("direction")
        if status_filter:
            qs = qs.filter(status=status_filter)
        if direction == "incoming":
            qs = qs.filter(recipient=user)
        elif direction == "outgoing":
            qs = qs.filter(sender=user)
        return qs

    def create(self, request):
        serializer = SendInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        identifier = serializer.validated_data["identifier"]
        message = serializer.validated_data.get("message", "")
        target = User.objects.filter(Q(email__iexact=identifier) | Q(username__iexact=identifier)).first()
        if not target:
            return Response({"detail": "No user found with this username or email."}, status=status.HTTP_404_NOT_FOUND)
        if target == request.user:
            return Response({"detail": "You cannot invite yourself."}, status=status.HTTP_400_BAD_REQUEST)
        if blocks_between(request.user, target):
            return Response({"detail": "Your Are restricted"}, status=status.HTTP_403_FORBIDDEN)

        reverse = ConnectionInvitation.objects.filter(sender=target, recipient=request.user).first()
        if reverse and reverse.status == ConnectionInvitation.Status.PENDING:
            return Response({"detail": "This user already invited you. Accept or reject their invitation."}, status=status.HTTP_400_BAD_REQUEST)
        if reverse and reverse.status == ConnectionInvitation.Status.ACCEPTED:
            return Response({"detail": "You are already connected."}, status=status.HTTP_400_BAD_REQUEST)

        invitation, created = ConnectionInvitation.objects.get_or_create(sender=request.user, recipient=target)
        now = timezone.now()
        if not created:
            if invitation.status == ConnectionInvitation.Status.ACCEPTED:
                return Response({"detail": "You are already connected."}, status=status.HTTP_400_BAD_REQUEST)
            if invitation.status == ConnectionInvitation.Status.PENDING:
                return Response({"detail": "Invitation is already pending."}, status=status.HTTP_400_BAD_REQUEST)
            if invitation.status == ConnectionInvitation.Status.REJECTED and invitation.rejected_until and now < invitation.rejected_until:
                return Response({
                    "detail": "This user rejected your invitation. You can try again after 24 hours.",
                    "can_retry_after": invitation.rejected_until,
                }, status=status.HTTP_429_TOO_MANY_REQUESTS)
        invitation.status = ConnectionInvitation.Status.PENDING
        invitation.message = message
        invitation.responded_at = None
        invitation.rejected_until = None
        invitation.save()
        notify(
            recipient=target,
            actor=request.user,
            type_value=Notification.Type.INVITATION_RECEIVED,
            title="New invitation received",
            message=f"{user_display(request.user)} sent you an invitation.",
            payload={"invitation_id": invitation.id, "user_id": request.user.id},
        )
        return Response(ConnectionInvitationSerializer(invitation, context={"request": request}).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        invitation = self.get_object()
        if invitation.recipient_id != request.user.id:
            return Response({"detail": "Only the invited user can accept this invitation."}, status=status.HTTP_403_FORBIDDEN)
        if blocks_between(invitation.sender, invitation.recipient):
            return Response({"detail": "Your Are restricted"}, status=status.HTTP_403_FORBIDDEN)
        invitation.status = ConnectionInvitation.Status.ACCEPTED
        invitation.responded_at = timezone.now()
        invitation.rejected_until = None
        invitation.save(update_fields=["status", "responded_at", "rejected_until", "updated_at"])
        notify(
            recipient=invitation.sender,
            actor=request.user,
            type_value=Notification.Type.INVITATION_ACCEPTED,
            title="Invitation accepted",
            message=f"{user_display(request.user)} accepted your invitation.",
            payload={"invitation_id": invitation.id, "user_id": request.user.id},
        )
        return Response(ConnectionInvitationSerializer(invitation, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        invitation = self.get_object()
        if invitation.recipient_id != request.user.id:
            return Response({"detail": "Only the invited user can reject this invitation."}, status=status.HTTP_403_FORBIDDEN)
        invitation.status = ConnectionInvitation.Status.REJECTED
        invitation.responded_at = timezone.now()
        invitation.rejected_until = timezone.now() + timedelta(hours=24)
        invitation.save(update_fields=["status", "responded_at", "rejected_until", "updated_at"])
        notify(
            recipient=invitation.sender,
            actor=request.user,
            type_value=Notification.Type.INVITATION_REJECTED,
            title="Invitation rejected",
            message=f"{user_display(request.user)} rejected your invitation. You can try again after 24 hours.",
            payload={"invitation_id": invitation.id, "user_id": request.user.id, "can_retry_after": invitation.rejected_until.isoformat()},
        )
        return Response(ConnectionInvitationSerializer(invitation, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        invitation = self.get_object()
        if invitation.sender_id != request.user.id:
            return Response({"detail": "Only sender can cancel this invitation."}, status=status.HTTP_403_FORBIDDEN)
        if invitation.status != ConnectionInvitation.Status.PENDING:
            return Response({"detail": "Only pending invitations can be cancelled."}, status=status.HTTP_400_BAD_REQUEST)
        invitation.status = ConnectionInvitation.Status.CANCELLED
        invitation.save(update_fields=["status", "updated_at"])
        return Response(ConnectionInvitationSerializer(invitation, context={"request": request}).data)


class BlockViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    serializer_class = UserBlockSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return UserBlock.objects.filter(blocker=self.request.user).select_related("blocked")

    def perform_destroy(self, instance):
        instance.delete()


class ReviewViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    serializer_class = UserReviewSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = UserReview.objects.filter(Q(reviewer=user) | Q(reviewed_user=user)).select_related("reviewer", "reviewed_user")
        reviewed_user = self.request.query_params.get("reviewed_user")
        if reviewed_user:
            qs = qs.filter(reviewed_user_id=reviewed_user)
        return qs

    def perform_create(self, serializer):
        serializer.save(reviewer=self.request.user)

    def update(self, request, *args, **kwargs):
        review = self.get_object()
        if review.reviewer_id != request.user.id:
            return Response({"detail": "Only reviewer can edit this review."}, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        serializer.save(reviewer=self.request.user)

    def destroy(self, request, *args, **kwargs):
        review = self.get_object()
        if review.reviewer_id != request.user.id:
            return Response({"detail": "Only reviewer can delete this review."}, status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)


class ConversationViewSet(viewsets.ModelViewSet):
    serializer_class = ConversationSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        return Conversation.objects.filter(memberships__user=self.request.user, memberships__is_active=True).distinct().prefetch_related("memberships__user", "messages").order_by("-updated_at", "-id")

    def create(self, request, *args, **kwargs):
        serializer = CreateConversationSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data["type"] == Conversation.Type.DIRECT:
            other = data["other_user"]
            existing = Conversation.objects.filter(type=Conversation.Type.DIRECT, memberships__user=request.user).filter(memberships__user=other).annotate(member_count=Count("memberships")).filter(member_count=2).first()
            if existing:
                return Response(ConversationSerializer(existing, context={"request": request}).data)
            conversation = Conversation.objects.create(type=Conversation.Type.DIRECT, created_by=request.user)
            ConversationParticipant.objects.bulk_create([
                ConversationParticipant(conversation=conversation, user=request.user, role=ConversationParticipant.Role.OWNER),
                ConversationParticipant(conversation=conversation, user=other, role=ConversationParticipant.Role.MEMBER),
            ])
        else:
            participant_users = data["participant_users"]
            try:
                validate_group_members(request.user, participant_users)
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            conversation = Conversation.objects.create(type=Conversation.Type.GROUP, title=data.get("title") or "New Group", created_by=request.user)
            ConversationParticipant.objects.create(conversation=conversation, user=request.user, role=ConversationParticipant.Role.OWNER)
            ConversationParticipant.objects.bulk_create([
                ConversationParticipant(conversation=conversation, user=user, role=ConversationParticipant.Role.MEMBER)
                for user in participant_users
            ])
            for user in participant_users:
                notify(
                    recipient=user,
                    actor=request.user,
                    type_value=Notification.Type.GROUP_ADDED,
                    title="Added to group",
                    message=f"{user_display(request.user)} added you to {conversation.title}.",
                    payload={"conversation_id": conversation.id},
                )
        return Response(ConversationSerializer(conversation, context={"request": request}).data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, *args, **kwargs):
        conversation = self.get_object()
        for member in conversation.participants.exclude(id=request.user.id):
            if blocked_pair_exists(request.user, member):
                return Response({"restricted": True, "message": "Your Are restricted"}, status=status.HTTP_403_FORBIDDEN)
        return Response(ConversationSerializer(conversation, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="add-participants")
    def add_participants(self, request, pk=None):
        conversation = self.get_object()
        membership = conversation.memberships.filter(user=request.user, is_active=True).first()
        if conversation.type != Conversation.Type.GROUP:
            return Response({"detail": "Only groups can add participants."}, status=status.HTTP_400_BAD_REQUEST)
        if not membership or membership.role not in [ConversationParticipant.Role.OWNER, ConversationParticipant.Role.ADMIN]:
            return Response({"detail": "Only group owner/admin can add participants."}, status=status.HTTP_403_FORBIDDEN)
        ids = list(dict.fromkeys([int(x) for x in request.data.get("participant_ids", [])]))
        users = list(User.objects.filter(id__in=ids).exclude(conversation_memberships__conversation=conversation, conversation_memberships__is_active=True))
        try:
            validate_group_members(request.user, list(conversation.participants.all()) + users)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        for user in users:
            ConversationParticipant.objects.get_or_create(conversation=conversation, user=user, defaults={"role": ConversationParticipant.Role.MEMBER})
            notify(user, request.user, Notification.Type.GROUP_ADDED, "Added to group", f"{user_display(request.user)} added you to {conversation.title}.", {"conversation_id": conversation.id})
        return Response(ConversationSerializer(conversation, context={"request": request}).data)

    @action(detail=True, methods=["get", "post"], parser_classes=[JSONParser, MultiPartParser, FormParser])
    def messages(self, request, pk=None):
        conversation = self.get_object()
        if not conversation.memberships.filter(user=request.user, is_active=True).exists():
            return Response({"detail": "You are not in this conversation."}, status=status.HTTP_403_FORBIDDEN)
        for member in conversation.participants.exclude(id=request.user.id):
            if blocked_pair_exists(request.user, member):
                return Response({"detail": "Your Are restricted"}, status=status.HTTP_403_FORBIDDEN)
        if request.method == "GET":
            qs = conversation.messages.select_related("sender").order_by("created_at", "id")
            return Response(ChatMessageSerializer(qs, many=True, context={"request": request}).data)

        serializer = ChatMessageSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        msg = serializer.save(conversation=conversation, sender=request.user)
        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["updated_at"])
        for member in conversation.participants.exclude(id=request.user.id):
            notify(
                recipient=member,
                actor=request.user,
                type_value=Notification.Type.MESSAGE_RECEIVED,
                title="New message",
                message=f"{user_display(request.user)} sent a message in {conversation.title or 'chat'}.",
                payload={"conversation_id": conversation.id, "message_id": msg.id},
            )
        return Response(ChatMessageSerializer(msg, context={"request": request}).data, status=status.HTTP_201_CREATED)


class NotificationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Notification.objects.filter(recipient=self.request.user).select_related("actor")
        is_read = self.request.query_params.get("is_read")
        if is_read in ["true", "false"]:
            qs = qs.filter(is_read=is_read == "true")
        return qs

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        notification.is_read = True
        notification.save(update_fields=["is_read", "updated_at"])
        return Response(NotificationSerializer(notification, context={"request": request}).data)

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        count = self.get_queryset().filter(is_read=False).update(is_read=True, updated_at=timezone.now())
        return Response({"updated": count})

    @action(detail=False, methods=["get"])
    def count(self, request):
        return Response({"unread": self.get_queryset().filter(is_read=False).count()})
