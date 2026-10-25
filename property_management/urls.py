from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"properties", views.PropertyViewSet, basename="pm-property")
router.register(r"floors", views.FloorViewSet, basename="pm-floor")
router.register(r"floor-sections", views.FloorSectionViewSet, basename="pm-floor-section")
router.register(r"units", views.RentalUnitViewSet, basename="pm-unit")
router.register(r"tenants", views.TenantViewSet, basename="pm-tenant")
router.register(r"agreements", views.AgreementViewSet, basename="pm-agreement")
router.register(r"reminders", views.AgreementReminderViewSet, basename="pm-reminder")
router.register(r"charges", views.ChargeViewSet, basename="pm-charge")
router.register(r"rent-payments", views.PaymentViewSet, basename="pm-payment")
router.register(r"recurring-charges", views.RecurringChargeViewSet, basename="pm-recurring")
router.register(r"expenses", views.ExpenseViewSet, basename="pm-expense")
router.register(r"electricity/meters", views.MeterViewSet, basename="pm-meter")
router.register(r"electricity/meter-assignments", views.MeterAssignmentViewSet, basename="pm-meter-assignment")
router.register(r"electricity/readings", views.MeterReadingViewSet, basename="pm-reading")
router.register(r"electricity/bills", views.ElectricityBillViewSet, basename="pm-bill")
router.register(r"electricity/allocations", views.ElectricityAllocationViewSet, basename="pm-allocation")
router.register(r"security-deposits", views.DepositTransactionViewSet, basename="pm-deposit")
router.register(r"documents", views.DocumentViewSet, basename="pm-document")

urlpatterns = [
    path("dashboard/", views.DashboardView.as_view(), name="pm-dashboard"),
    path("rent-collection/", views.RentCollectionView.as_view(), name="pm-rent-collection"),
    path("reports/monthly/", views.MonthlyReportView.as_view(), name="pm-report-monthly"),
    path("reminder-settings/", views.ReminderSettingsView.as_view(), name="pm-reminder-settings"),
    path("options/", views.OptionsView.as_view(), name="pm-options"),
    path("files/<str:token>/", views.SignedFileView.as_view(), name="pm-signed-file"),
    path("", include(router.urls)),
]
