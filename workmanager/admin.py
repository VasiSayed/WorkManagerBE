from django.contrib import admin

from .models import (
    Attachment,
    DesignationHistory,
    EmailLog,
    EmailTemplate,
    EmailVariable,
    EmailType,
    FreelanceSource,
    Job,
    LeaveRecord,
    Meeting,
    MeetingMember,
    Project,
    SMTPConfig,
    SalaryCalculationLine,
    SalaryChangeHistory,
    SalaryRecord,
    SalaryRule,
    Task,
    WorkMember,
    Note,
    NoteFolder,
    NoteShare,
    DocumentCategory,
    Document,
    DocumentFile,
    ExpenseCategory,
    ExpenseContact,
    ExpenseTag,
    Expense,
    ExpenseFile,
)


class OwnerAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")


@admin.register(WorkMember)
class WorkMemberAdmin(OwnerAdmin):
    list_display = ("name", "member_type", "company", "email", "owner", "is_active")
    search_fields = ("name", "email", "company")
    list_filter = ("member_type", "is_active")


@admin.register(Job)
class JobAdmin(OwnerAdmin):
    list_display = ("company_name", "job_title", "current_designation", "current_salary", "status", "owner")
    search_fields = ("company_name", "job_title", "current_designation")
    list_filter = ("status",)


@admin.register(FreelanceSource)
class FreelanceSourceAdmin(OwnerAdmin):
    list_display = ("name", "primary_contact_name", "platform", "status", "owner")
    list_filter = ("platform", "status")
    search_fields = ("name", "primary_contact_name", "email")


@admin.register(Project)
class ProjectAdmin(OwnerAdmin):
    list_display = ("name", "source_type", "source_display", "status", "budget", "owner")
    list_filter = ("source_type", "status")
    search_fields = ("name", "manual_source_name")


@admin.register(Task)
class TaskAdmin(OwnerAdmin):
    list_display = ("name", "task_type", "date", "priority", "status", "owner")
    list_filter = ("task_type", "priority", "status")
    search_fields = ("name", "description")


class MeetingMemberInline(admin.TabularInline):
    model = MeetingMember
    extra = 0


@admin.register(Meeting)
class MeetingAdmin(OwnerAdmin):
    list_display = ("title", "meeting_type", "date", "start_time", "status", "owner")
    list_filter = ("meeting_type", "status")
    search_fields = ("title", "agenda", "mom")
    inlines = [MeetingMemberInline]


@admin.register(LeaveRecord)
class LeaveRecordAdmin(OwnerAdmin):
    list_display = ("title", "job", "leave_type", "date", "is_paid", "deduction_amount", "status", "owner")
    list_filter = ("leave_type", "status", "is_paid")


class SalaryCalculationLineInline(admin.TabularInline):
    model = SalaryCalculationLine
    extra = 0


@admin.register(SalaryRecord)
class SalaryRecordAdmin(OwnerAdmin):
    list_display = ("source_display", "salary_type", "month", "expected_amount", "received_amount", "pending_amount", "status", "owner")
    list_filter = ("salary_type", "status")
    inlines = [SalaryCalculationLineInline]


@admin.register(EmailLog)
class EmailLogAdmin(OwnerAdmin):
    list_display = ("subject", "email_type", "status", "sent_at", "owner")
    list_filter = ("status", "email_type")
    search_fields = ("subject", "body", "to_emails")


admin.site.register(DesignationHistory, OwnerAdmin)
admin.site.register(SalaryChangeHistory, OwnerAdmin)
admin.site.register(SalaryRule, OwnerAdmin)
admin.site.register(EmailType, OwnerAdmin)

@admin.register(EmailVariable)
class EmailVariableAdmin(OwnerAdmin):
    list_display = ("key", "label", "category", "is_required", "is_active", "owner")
    search_fields = ("key", "label", "description")
    list_filter = ("category", "is_required", "is_active")

admin.site.register(EmailTemplate, OwnerAdmin)
admin.site.register(SMTPConfig, OwnerAdmin)
admin.site.register(Attachment, OwnerAdmin)


@admin.register(NoteFolder)
class NoteFolderAdmin(OwnerAdmin):
    list_display = ("name", "parent", "owner", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "description")


class NoteShareInline(admin.TabularInline):
    model = NoteShare
    extra = 0


@admin.register(Note)
class NoteAdmin(OwnerAdmin):
    list_display = ("label", "folder", "visibility", "data_type", "status", "owner", "is_active")
    list_filter = ("visibility", "data_type", "status", "is_active")
    search_fields = ("label", "description", "folder__name")
    inlines = [NoteShareInline]


@admin.register(DocumentCategory)
class DocumentCategoryAdmin(OwnerAdmin):
    list_display = ("name", "parent", "owner", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "description")


class DocumentFileInline(admin.TabularInline):
    model = DocumentFile
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(Document)
class DocumentAdmin(OwnerAdmin):
    list_display = ("name", "category", "owner", "is_active", "updated_at")
    list_filter = ("is_active", "category")
    search_fields = ("name", "description")
    inlines = [DocumentFileInline]

@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(OwnerAdmin):
    list_display = ("name", "parent", "owner", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "description")


@admin.register(ExpenseContact)
class ExpenseContactAdmin(OwnerAdmin):
    list_display = ("name", "phone", "owner", "updated_at")
    search_fields = ("name", "phone")


@admin.register(ExpenseTag)
class ExpenseTagAdmin(OwnerAdmin):
    list_display = ("name", "owner")
    search_fields = ("name",)


class ExpenseFileInline(admin.TabularInline):
    model = ExpenseFile
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(Expense)
class ExpenseAdmin(OwnerAdmin):
    list_display = ("date", "transaction_type", "amount", "name", "category", "related_user", "owner")
    list_filter = ("transaction_type", "category", "date")
    search_fields = ("name", "category__name", "related_user__email", "related_user__full_name")
    inlines = [ExpenseFileInline]

