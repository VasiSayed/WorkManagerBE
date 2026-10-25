from django.contrib import admin

from .models import (
    Agreement,
    AgreementReminder,
    Charge,
    ChargeAdjustment,
    DepositTransaction,
    Document,
    ElectricityAllocation,
    ElectricityBill,
    Expense,
    Floor,
    FloorSection,
    Meter,
    MeterAssignment,
    MeterReading,
    Payment,
    PaymentAllocation,
    Property,
    RecurringCharge,
    ReminderSettings,
    RentalUnit,
    RentRevision,
    Tenant,
    UnitSpace,
)


class ReadOnlyFinancialAdmin(admin.ModelAdmin):
    """Financial history is corrected through the API workflows, never edited in admin."""

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False


class UnitSpaceInline(admin.TabularInline):
    model = UnitSpace
    extra = 0


class FloorSectionInline(admin.TabularInline):
    model = FloorSection
    extra = 0
    exclude = ["owner"]


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ["name", "owner", "property_type", "status"]
    list_filter = ["property_type", "status"]
    search_fields = ["name", "address"]


@admin.register(Floor)
class FloorAdmin(admin.ModelAdmin):
    list_display = ["name", "property", "level"]
    list_filter = ["property"]
    inlines = [FloorSectionInline]


@admin.register(RentalUnit)
class RentalUnitAdmin(admin.ModelAdmin):
    list_display = ["name", "property", "status"]
    list_filter = ["property", "status"]
    inlines = [UnitSpaceInline]


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ["full_name", "mobile", "status", "owner"]
    list_filter = ["status"]
    search_fields = ["full_name", "mobile"]


@admin.register(Agreement)
class AgreementAdmin(admin.ModelAdmin):
    list_display = ["tenant", "unit", "monthly_rent", "start_date", "end_date", "status"]
    list_filter = ["status", "property"]
    search_fields = ["tenant__full_name", "unit__name"]
    readonly_fields = ["renewed_from", "billing_end_date", "move_out_date"]
    filter_horizontal = ["additional_units"]


@admin.register(RecurringCharge)
class RecurringChargeAdmin(admin.ModelAdmin):
    list_display = ["name", "amount", "frequency", "start_date", "end_date", "is_enabled"]
    list_filter = ["is_enabled", "frequency"]


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ["title", "property", "amount", "expense_date", "is_recoverable", "recoverable_amount"]
    list_filter = ["category", "is_recoverable", "property"]
    search_fields = ["title"]


@admin.register(Meter)
class MeterAdmin(admin.ModelAdmin):
    list_display = ["name", "meter_number", "property", "meter_type", "is_active"]
    list_filter = ["meter_type", "is_active"]


@admin.register(MeterAssignment)
class MeterAssignmentAdmin(admin.ModelAdmin):
    list_display = ["meter", "agreement", "start_date", "end_date", "share_percent"]


@admin.register(MeterReading)
class MeterReadingAdmin(ReadOnlyFinancialAdmin):
    list_display = ["meter", "reading_date", "previous_reading", "current_reading", "units_consumed", "is_reset"]


@admin.register(ElectricityBill)
class ElectricityBillAdmin(ReadOnlyFinancialAdmin):
    list_display = ["property", "period", "amount", "allocation_method", "status"]
    list_filter = ["status", "allocation_method"]


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ["title", "doc_type", "property", "tenant", "agreement", "created_at"]
    list_filter = ["doc_type"]


@admin.register(ReminderSettings)
class ReminderSettingsAdmin(admin.ModelAdmin):
    list_display = ["owner", "enabled", "days_before"]


for model, columns in [
    (Charge, ["description", "tenant", "charge_type", "period", "amount", "due_date", "is_void"]),
    (ChargeAdjustment, ["charge", "amount", "reason", "created_at"]),
    (Payment, ["tenant", "amount", "payment_date", "method", "is_reversed"]),
    (PaymentAllocation, ["payment", "charge", "amount"]),
    (ElectricityAllocation, ["bill", "agreement", "units", "amount"]),
    (DepositTransaction, ["agreement", "txn_type", "amount", "txn_date"]),
    (RentRevision, ["agreement", "previous_rent", "new_rent", "effective_date"]),
    (AgreementReminder, ["agreement", "expiry_date", "days_before", "remind_on", "dismissed"]),
]:
    admin.site.register(model, type(f"{model.__name__}Admin", (ReadOnlyFinancialAdmin,), {"list_display": columns}))
