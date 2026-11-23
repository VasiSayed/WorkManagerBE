from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"assets", views.AssetViewSet, basename="findit-asset")
router.register(r"purposes", views.PurposeViewSet, basename="findit-purpose")

urlpatterns = [
    path("household/", views.HouseholdView.as_view(), name="findit-household"),
    path("household/members/", views.HouseholdMembersView.as_view(), name="findit-members"),
    path("household/members/<int:member_id>/", views.HouseholdMembersView.as_view(), name="findit-member"),
    path("labels/pdf/", views.LabelsPdfView.as_view(), name="findit-labels-pdf"),
    path("files/<str:token>/", views.FileView.as_view(), name="findit-file"),
    path("", include(router.urls)),
]
