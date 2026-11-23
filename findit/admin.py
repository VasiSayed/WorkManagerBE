from django.contrib import admin

from .models import Asset, AssetHistory, AssetImage, Household, HouseholdMember, Purpose, ReferenceSequence


class MemberInline(admin.TabularInline):
    model = HouseholdMember
    extra = 0


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "created_at")
    inlines = [MemberInline]


@admin.register(Purpose)
class PurposeAdmin(admin.ModelAdmin):
    list_display = ("name", "household", "is_active")
    search_fields = ("name",)


class ImageInline(admin.TabularInline):
    model = AssetImage
    extra = 0
    readonly_fields = ("original_name", "width", "height", "uploaded_by", "created_at")


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("reference", "name", "household", "purpose", "status", "updated_at")
    list_filter = ("status", "household")
    search_fields = ("reference", "name", "description", "location")
    readonly_fields = ("uuid", "number", "reference", "created_at", "updated_at")
    inlines = [ImageInline]

    def has_delete_permission(self, request, obj=None):
        return False  # archive or reset instead; numbers and labels are permanent


@admin.register(AssetHistory)
class AssetHistoryAdmin(admin.ModelAdmin):
    list_display = ("asset", "action", "field", "user", "created_at")
    list_filter = ("action",)

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ReferenceSequence)
class ReferenceSequenceAdmin(admin.ModelAdmin):
    list_display = ("key", "last_value")

    def has_change_permission(self, request, obj=None):
        return False
