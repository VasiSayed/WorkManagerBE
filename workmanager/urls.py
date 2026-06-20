from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AttachmentViewSet,
    DashboardAPIView,
    DesignationHistoryViewSet,
    DesignationReportAPIView,
    EmailLogViewSet,
    EmailReportAPIView,
    EmailTemplateViewSet,
    EmailVariableViewSet,
    EmailTypeViewSet,
    EmployeeReportAPIView,
    FreelanceSourceViewSet,
    JobViewSet,
    LeaveRecordViewSet,
    LeaveReportAPIView,
    MeetingReportAPIView,
    MeetingViewSet,
    OptionsAPIView,
    ProjectViewSet,
    ReportsHubAPIView,
    SMTPConfigViewSet,
    SalaryChangeHistoryViewSet,
    SalaryRecordViewSet,
    SalaryReportAPIView,
    SalaryRuleViewSet,
    TaskReportAPIView,
    TaskViewSet,
    WorkMemberViewSet,
    NoteViewSet,
    DocumentCategoryViewSet,
    DocumentViewSet
)

router = DefaultRouter()

# Daily pages
router.register(r"tasks", TaskViewSet, basename="tasks")
router.register(r"meetings", MeetingViewSet, basename="meetings")
router.register(r"leave", LeaveRecordViewSet, basename="leave")
router.register(r"salary", SalaryRecordViewSet, basename="salary")
router.register(r"emails", EmailLogViewSet, basename="emails")
router.register(r"notes", NoteViewSet, basename="notes")
router.register(r"document-categories", DocumentCategoryViewSet, basename="document-categories")
router.register(r"documents", DocumentViewSet, basename="documents")

# Management pages
router.register(r"management/jobs", JobViewSet, basename="management-jobs")
router.register(r"management/sources", FreelanceSourceViewSet, basename="management-sources")
router.register(r"management/freelancing", FreelanceSourceViewSet, basename="management-freelancing")
router.register(r"management/projects", ProjectViewSet, basename="management-projects")
router.register(r"management/employees", WorkMemberViewSet, basename="management-employees")
router.register(r"management/email-types", EmailTypeViewSet, basename="management-email-types")
router.register(r"management/email-templates", EmailTemplateViewSet, basename="management-email-templates")
router.register(r"management/email-variables", EmailVariableViewSet, basename="management-email-variables")
router.register(r"management/smtp", SMTPConfigViewSet, basename="management-smtp")
router.register(r"management/salary-rules", SalaryRuleViewSet, basename="management-salary-rules")
router.register(r"management/designations", DesignationHistoryViewSet, basename="management-designations")
router.register(r"management/salary-changes", SalaryChangeHistoryViewSet, basename="management-salary-changes")
router.register(r"management/attachments", AttachmentViewSet, basename="management-attachments")

urlpatterns = [
    path("", include(router.urls)),
    path("options/", OptionsAPIView.as_view(), name="api-options"),
    path("dashboard/", DashboardAPIView.as_view(), name="dashboard"),
    path("reports/", ReportsHubAPIView.as_view(), name="reports-hub"),
    path("reports/tasks/", TaskReportAPIView.as_view(), name="reports-tasks"),
    path("reports/meetings/", MeetingReportAPIView.as_view(), name="reports-meetings"),
    path("reports/salary/", SalaryReportAPIView.as_view(), name="reports-salary"),
    path("reports/leave/", LeaveReportAPIView.as_view(), name="reports-leave"),
    path("reports/emails/", EmailReportAPIView.as_view(), name="reports-emails"),
    path("reports/employees/", EmployeeReportAPIView.as_view(), name="reports-employees"),
    path("reports/designations/", DesignationReportAPIView.as_view(), name="reports-designations"),
]
