from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    BlockViewSet,
    ConversationViewSet,
    InvitationViewSet,
    NotificationViewSet,
    ReviewViewSet,
    UserProfileAPIView,
    UserSearchAPIView,
)

router = DefaultRouter()
router.register(r"invitations", InvitationViewSet, basename="social-invitations")
router.register(r"blocks", BlockViewSet, basename="social-blocks")
router.register(r"reviews", ReviewViewSet, basename="social-reviews")
router.register(r"conversations", ConversationViewSet, basename="social-conversations")
router.register(r"notifications", NotificationViewSet, basename="social-notifications")

urlpatterns = [
    path("users/search/", UserSearchAPIView.as_view(), name="social-user-search"),
    path("users/<int:user_id>/profile/", UserProfileAPIView.as_view(), name="social-user-profile"),
    path("", include(router.urls)),
]
