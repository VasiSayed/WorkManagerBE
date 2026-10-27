from decimal import Decimal

from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from rest_framework import serializers

from . import services
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


class OwnedPKField(serializers.PrimaryKeyRelatedField):
    """PK field that only accepts objects owned by the requesting user."""

    def __init__(self, model, **kwargs):
        self.model = model
        kwargs.setdefault("queryset", model.objects.all())
        super().__init__(**kwargs)

    def get_queryset(self):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return self.model.objects.none()
        return self.model.objects.filter(owner=request.user)


FILE_LINK_SALT = "property_management.file"
FILE_LINK_MAX_AGE = 600  # seconds


def signed_file_url(request, kind, obj):
    """Short-lived link that opens a private file without an Authorization header (e.g. in the phone's browser)."""
    token = signing.dumps({"k": kind, "id": obj.pk, "u": obj.owner_id}, salt=FILE_LINK_SALT, compress=True)
    url = reverse("pm-signed-file", args=[token])
    return request.build_absolute_uri(url) if request else url


def _service_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except services.ServiceError as exc:
        raise serializers.ValidationError({"detail": exc.message, "code": exc.code, **exc.extra})


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class PropertySerializer(serializers.ModelSerializer):
    unit_count = serializers.IntegerField(read_only=True)
    occupied_count = serializers.IntegerField(read_only=True)
    floor_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Property
        fields = [
            "id", "name", "address", "property_type", "description", "number_of_floors", "status", "notes",
            "unit_count", "occupied_count", "floor_count", "created_at", "updated_at",
        ]

    def validate_name(self, value):
        request = self.context["request"]
        qs = Property.objects.filter(owner=request.user, name__iexact=value.strip())
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("You already have a property with this name.")
        return value.strip()


class FloorSectionSerializer(serializers.ModelSerializer):
    floor = OwnedPKField(Floor)

    class Meta:
        model = FloorSection
        fields = ["id", "floor", "name", "description"]
        validators = []

    def validate(self, attrs):
        floor = attrs.get("floor", getattr(self.instance, "floor", None))
        name = attrs.get("name", getattr(self.instance, "name", ""))
        qs = FloorSection.objects.filter(floor=floor, name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({"name": "This floor already has a section with this name."})
        return attrs


class FloorSerializer(serializers.ModelSerializer):
    property = OwnedPKField(Property)
    sections = FloorSectionSerializer(many=True, read_only=True)

    class Meta:
        model = Floor
        fields = ["id", "property", "name", "level", "notes", "sections"]
        validators = []

    def validate(self, attrs):
        prop = attrs.get("property", getattr(self.instance, "property", None))
        name = attrs.get("name", getattr(self.instance, "name", ""))
        qs = Floor.objects.filter(property=prop, name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({"name": "This property already has a floor with this name."})
        return attrs


class UnitSpaceSerializer(serializers.ModelSerializer):
    floor = OwnedPKField(Floor)
    section = OwnedPKField(FloorSection, required=False, allow_null=True)
    floor_name = serializers.CharField(source="floor.name", read_only=True)
    section_name = serializers.CharField(source="section.name", read_only=True, default=None)

    class Meta:
        model = UnitSpace
        fields = ["id", "floor", "section", "floor_name", "section_name"]


class RentalUnitSerializer(serializers.ModelSerializer):
    property = OwnedPKField(Property)
    property_name = serializers.CharField(source="property.name", read_only=True)
    spaces = UnitSpaceSerializer(many=True, required=False)
    current_agreement = serializers.SerializerMethodField()

    class Meta:
        model = RentalUnit
        fields = [
            "id", "property", "property_name", "name", "description", "status", "notes", "spaces",
            "current_agreement", "created_at",
        ]
        validators = []

    def get_current_agreement(self, obj):
        candidates = [*obj.agreements.all(), *obj.additional_agreements.all()]
        active = [a for a in candidates if a.status == Agreement.Status.ACTIVE]
        if not active:
            return None
        a = active[0]
        return {"id": a.id, "tenant_id": a.tenant_id, "tenant_name": a.tenant.full_name}

    def validate(self, attrs):
        prop = attrs.get("property", getattr(self.instance, "property", None))
        name = attrs.get("name", getattr(self.instance, "name", ""))
        qs = RentalUnit.objects.filter(property=prop, name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({"name": "This property already has a unit with this name."})
        if self.instance and "property" in attrs and attrs["property"] != self.instance.property:
            if self.instance.agreements.exists():
                raise serializers.ValidationError({"property": "Unit has agreements; it cannot move to another property."})
        seen = set()
        for space in attrs.get("spaces", []):
            if space["floor"].property_id != prop.id:
                raise serializers.ValidationError({"spaces": "Floors must belong to the unit's property."})
            section = space.get("section")
            if section and section.floor_id != space["floor"].id:
                raise serializers.ValidationError({"spaces": "Section does not belong to the selected floor."})
            key = (space["floor"].id, section.id if section else None)
            if key in seen:
                raise serializers.ValidationError({"spaces": "The same space was added twice."})
            seen.add(key)
        return attrs

    def _save_spaces(self, unit, spaces):
        if spaces is None:
            return
        unit.spaces.all().delete()
        UnitSpace.objects.bulk_create(
            [UnitSpace(unit=unit, floor=s["floor"], section=s.get("section")) for s in spaces]
        )
        # Changing occupied space must not create an overlap with another active tenant.
        active = list(
            Agreement.objects.filter(Q(unit=unit) | Q(additional_units=unit), status=Agreement.Status.ACTIVE)
            .values_list("id", flat=True)
            .distinct()
        )
        if active:
            _service_call(services.ensure_no_overlap, unit, exclude_ids=active)

    @transaction.atomic
    def create(self, validated_data):
        spaces = validated_data.pop("spaces", [])
        unit = super().create(validated_data)
        self._save_spaces(unit, spaces)
        return unit

    @transaction.atomic
    def update(self, instance, validated_data):
        spaces = validated_data.pop("spaces", None)
        unit = super().update(instance, validated_data)
        self._save_spaces(unit, spaces)
        return unit


# ---------------------------------------------------------------------------
# Tenants & agreements
# ---------------------------------------------------------------------------


def mask(value):
    value = value or ""
    return ("•" * max(len(value) - 4, 0)) + value[-4:] if value else ""


class TenantSerializer(serializers.ModelSerializer):
    current_agreement = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            "id", "full_name", "mobile", "alternate_mobile", "email", "address", "id_type", "id_number",
            "emergency_contact_name", "emergency_contact_phone", "status", "notes", "current_agreement",
            "created_at", "updated_at",
        ]
        read_only_fields = ["status"]

    def get_current_agreement(self, obj):
        active = [a for a in obj.agreements.all() if a.status == Agreement.Status.ACTIVE]
        if not active:
            return None
        a = active[0]
        return {"id": a.id, "unit_name": a.units_label, "property_name": a.property.name, "property_id": a.property_id}

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Identity numbers are shown in full only on the single-tenant endpoint.
        if not self.context.get("show_identity"):
            data["id_number"] = mask(data.get("id_number"))
        return data

    def validate_mobile(self, value):
        digits = "".join(ch for ch in value if ch.isdigit())
        if len(digits) < 7:
            raise serializers.ValidationError("Enter a valid mobile number.")
        return value.strip()


class RentRevisionSerializer(serializers.ModelSerializer):
    document = OwnedPKField(Document, required=False, allow_null=True)

    class Meta:
        model = RentRevision
        fields = ["id", "agreement", "previous_rent", "new_rent", "effective_date", "reason", "document", "created_at"]
        read_only_fields = ["agreement", "previous_rent"]


class AgreementSerializer(serializers.ModelSerializer):
    property = OwnedPKField(Property)
    unit = OwnedPKField(RentalUnit)
    additional_units = OwnedPKField(RentalUnit, many=True, required=False)
    units = serializers.SerializerMethodField()
    tenant = OwnedPKField(Tenant)
    tenant_name = serializers.CharField(source="tenant.full_name", read_only=True)
    tenant_mobile = serializers.CharField(source="tenant.mobile", read_only=True)
    unit_name = serializers.CharField(source="units_label", read_only=True)
    property_name = serializers.CharField(source="property.name", read_only=True)
    current_rent = serializers.SerializerMethodField()
    expiry_state = serializers.SerializerMethodField()
    renewed_to = serializers.PrimaryKeyRelatedField(read_only=True)
    billing_start_date = serializers.DateField(required=False)

    class Meta:
        model = Agreement
        fields = [
            "id", "property", "unit", "additional_units", "units", "tenant", "tenant_name", "tenant_mobile", "unit_name",
            "property_name",
            "monthly_rent", "current_rent", "rent_due_day", "start_date", "end_date", "billing_start_date",
            "billing_end_date", "security_deposit_amount", "status", "expiry_state", "move_out_date",
            "renewed_from", "renewed_to", "reminders_enabled", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "move_out_date", "renewed_from", "billing_end_date"]

    def get_units(self, obj):
        return [{"id": u.id, "name": u.name} for u in obj.all_units]

    def get_current_rent(self, obj):
        return str(services.rent_for_period(obj, services.month_start(services.today())))

    def get_expiry_state(self, obj):
        return services.expiry_state(obj, window_days=self.context.get("expiry_window", services.DEFAULT_EXPIRING_DAYS))

    def validate(self, attrs):
        if self.instance:
            locked = {"property", "unit", "tenant", "start_date", "billing_start_date", "monthly_rent"}
            changed = [f for f in locked if f in attrs and attrs[f] != getattr(self.instance, f)]
            if changed and self.instance.charges.exists():
                raise serializers.ValidationError(
                    {"detail": f"{', '.join(sorted(changed))} cannot change after charges exist. "
                               "Use a rent revision or renew the agreement instead."}
                )
        prop = attrs.get("property", getattr(self.instance, "property", None))
        unit = attrs.get("unit", getattr(self.instance, "unit", None))
        if unit and prop and unit.property_id != prop.id:
            raise serializers.ValidationError({"unit": "Unit does not belong to the selected property."})
        if "additional_units" in attrs:
            extra = attrs["additional_units"]
            if len({u.id for u in extra}) != len(extra) or (unit and unit.id in {u.id for u in extra}):
                raise serializers.ValidationError({"additional_units": "Each unit can be selected only once."})
            if prop and any(u.property_id != prop.id for u in extra):
                raise serializers.ValidationError({"additional_units": "All units must belong to the selected property."})
            if self.instance:
                if self.instance.status != Agreement.Status.ACTIVE:
                    raise serializers.ValidationError({"additional_units": "Units can only be changed on an active agreement."})
                current = {u.id for u in self.instance.additional_units.all()}
                for u in extra:
                    if u.id not in current:
                        _service_call(services.ensure_no_overlap, u, exclude_ids=[self.instance.id])
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": "Expiry date must be after the start date."})
        billing = attrs.get("billing_start_date")
        if billing and start and services.month_start(billing) < services.month_start(start):
            raise serializers.ValidationError({"billing_start_date": "Billing cannot start before the agreement."})
        return attrs

    def create(self, validated_data):
        return _service_call(services.create_agreement, owner=self.context["request"].user, validated_data=validated_data)


class RenewAgreementSerializer(serializers.Serializer):
    start_date = serializers.DateField()
    end_date = serializers.DateField(required=False, allow_null=True)
    monthly_rent = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=0)
    rent_due_day = serializers.IntegerField(required=False, min_value=1, max_value=31)
    security_deposit_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=0)
    notes = serializers.CharField(required=False, allow_blank=True)


class EndAgreementSerializer(serializers.Serializer):
    move_out_date = serializers.DateField()
    status = serializers.ChoiceField(choices=[Agreement.Status.ENDED, Agreement.Status.TERMINATED], default=Agreement.Status.ENDED)
    notes = serializers.CharField(required=False, allow_blank=True)


class ReminderSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReminderSettings
        fields = ["enabled", "days_before"]


class AgreementReminderSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source="agreement.tenant.full_name", read_only=True)
    unit_name = serializers.CharField(source="agreement.units_label", read_only=True)
    agreement_status = serializers.CharField(source="agreement.status", read_only=True)

    class Meta:
        model = AgreementReminder
        fields = ["id", "agreement", "tenant_name", "unit_name", "agreement_status", "expiry_date", "days_before", "remind_on", "dismissed"]
        read_only_fields = ["agreement", "expiry_date", "days_before", "remind_on"]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class ChargeAdjustmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChargeAdjustment
        fields = ["id", "amount", "reason", "created_at"]


class ChargeSerializer(serializers.ModelSerializer):
    agreement = OwnedPKField(Agreement)
    net_amount = serializers.SerializerMethodField()
    paid = serializers.SerializerMethodField()
    outstanding = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    tenant_name = serializers.CharField(source="tenant.full_name", read_only=True)
    adjustments = ChargeAdjustmentSerializer(many=True, read_only=True)

    class Meta:
        model = Charge
        fields = [
            "id", "agreement", "tenant", "tenant_name", "property", "charge_type", "period", "description",
            "amount", "net_amount", "paid", "outstanding", "status", "due_date", "included_in_monthly_due",
            "recurring_charge", "electricity_allocation", "expense", "is_void", "void_reason", "notes",
            "adjustments", "created_at",
        ]
        read_only_fields = [
            "tenant", "property", "charge_type", "recurring_charge", "electricity_allocation", "expense",
            "is_void", "void_reason", "included_in_monthly_due",
        ]

    def _figures(self, obj):
        cache = getattr(obj, "_figures", None)
        if cache is None:
            cache = obj._figures = services.charge_figures(obj)
        return cache

    def get_net_amount(self, obj):
        return str(self._figures(obj)[0])

    def get_paid(self, obj):
        return str(self._figures(obj)[1])

    def get_outstanding(self, obj):
        return str(self._figures(obj)[2])

    def get_status(self, obj):
        if obj.is_void:
            return "void"
        net, paid, outstanding = self._figures(obj)
        return services.charge_status(net, paid, outstanding, obj.due_date)

    def create(self, validated_data):
        # The API only creates one-time charges; other types come from their own workflows.
        agreement = validated_data["agreement"]
        return Charge.objects.create(
            owner=self.context["request"].user,
            tenant_id=agreement.tenant_id,
            property_id=agreement.property_id,
            charge_type=Charge.ChargeType.ONE_TIME,
            **validated_data,
        )


class PaymentAllocationSerializer(serializers.ModelSerializer):
    charge_description = serializers.CharField(source="charge.description", read_only=True)
    charge_type = serializers.CharField(source="charge.charge_type", read_only=True)

    class Meta:
        model = PaymentAllocation
        fields = ["id", "charge", "charge_description", "charge_type", "amount"]


class PaymentSerializer(serializers.ModelSerializer):
    allocations = PaymentAllocationSerializer(many=True, read_only=True)
    unapplied_amount = serializers.SerializerMethodField()
    tenant_name = serializers.CharField(source="tenant.full_name", read_only=True)
    unit_name = serializers.CharField(source="agreement.units_label", read_only=True)

    class Meta:
        model = Payment
        fields = [
            "id", "agreement", "tenant", "tenant_name", "unit_name", "amount", "payment_date", "method", "reference",
            "notes", "allocations", "unapplied_amount", "is_reversed", "reversed_at", "reversal_reason", "created_at",
        ]

    def get_unapplied_amount(self, obj):
        return str(services.payment_unapplied(obj))


class RecordPaymentSerializer(serializers.Serializer):
    agreement = OwnedPKField(Agreement)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    payment_date = serializers.DateField(required=False)
    method = serializers.ChoiceField(
        choices=[c for c in Payment.Method.choices if c[0] != Payment.Method.DEPOSIT], default=Payment.Method.CASH
    )
    reference = serializers.CharField(required=False, allow_blank=True, max_length=100)
    notes = serializers.CharField(required=False, allow_blank=True)
    charge_ids = serializers.ListField(child=serializers.IntegerField(), required=False, allow_empty=True)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=64)
    excess_handling = serializers.ChoiceField(choices=["advance_credit"], required=False, allow_null=True)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class AdjustmentInputSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    reason = serializers.CharField(max_length=255)


class RecurringChargeSerializer(serializers.ModelSerializer):
    property = OwnedPKField(Property, required=False, allow_null=True)
    unit = OwnedPKField(RentalUnit, required=False, allow_null=True)
    agreement = OwnedPKField(Agreement, required=False, allow_null=True)
    scope_label = serializers.SerializerMethodField()

    class Meta:
        model = RecurringCharge
        fields = [
            "id", "name", "amount", "frequency", "start_date", "end_date", "property", "unit", "agreement",
            "scope_label", "is_enabled", "included_in_monthly_due", "revised_from", "notes", "created_at",
        ]
        read_only_fields = ["revised_from"]

    def get_scope_label(self, obj):
        if obj.agreement_id:
            return f"Tenant: {obj.agreement.tenant.full_name} ({obj.agreement.units_label})"
        if obj.unit_id:
            return f"Unit: {obj.unit.name}"
        return f"Property: {obj.property.name}" if obj.property_id else ""

    def validate(self, attrs):
        scope = {k: attrs.get(k, getattr(self.instance, k, None)) for k in ("property", "unit", "agreement")}
        if sum(1 for v in scope.values() if v) != 1:
            raise serializers.ValidationError({"detail": "Choose exactly one of property, unit or agreement."})
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": "End date must be after the start date."})
        if self.instance and self.instance.charges.exists():
            locked = [f for f in ("amount", "frequency", "start_date", "property", "unit", "agreement")
                      if f in attrs and attrs[f] != getattr(self.instance, f)]
            if locked:
                raise serializers.ValidationError(
                    {"detail": f"{', '.join(locked)} cannot change after charges were generated. Use 'revise' instead."}
                )
        return attrs


class ReviseRecurringSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    effective_date = serializers.DateField()
    notes = serializers.CharField(required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


class ExpenseSerializer(serializers.ModelSerializer):
    property = OwnedPKField(Property)
    unit = OwnedPKField(RentalUnit, required=False, allow_null=True)
    agreement = OwnedPKField(Agreement, required=False, allow_null=True)
    property_name = serializers.CharField(source="property.name", read_only=True)
    unit_name = serializers.CharField(source="unit.name", read_only=True, default=None)
    tenant_name = serializers.CharField(source="agreement.tenant.full_name", read_only=True, default=None)
    recovery = serializers.SerializerMethodField()
    receipt_url = serializers.SerializerMethodField()
    receipt_open_url = serializers.SerializerMethodField()

    class Meta:
        model = Expense
        fields = [
            "id", "property", "property_name", "unit", "unit_name", "agreement", "tenant_name", "title",
            "description", "category", "amount", "expense_date", "is_recoverable", "recoverable_amount",
            "receipt", "receipt_url", "receipt_open_url", "recovery", "notes", "created_at",
        ]
        extra_kwargs = {"receipt": {"write_only": True, "required": False}}

    def get_recovery(self, obj):
        info = services.expense_recovery(obj)
        data = {k: str(v) if not isinstance(v, str) else v for k, v in info.items()}
        charge = getattr(obj, "charge", None)
        data["charge_id"] = charge.id if charge else None
        return data

    def get_receipt_url(self, obj):
        if not obj.receipt:
            return None
        request = self.context.get("request")
        url = reverse("pm-expense-receipt", args=[obj.pk])
        return request.build_absolute_uri(url) if request else url

    def get_receipt_open_url(self, obj):
        return signed_file_url(self.context.get("request"), "expense", obj) if obj.receipt else None

    def validate(self, attrs):
        get = lambda k: attrs.get(k, getattr(self.instance, k, None))  # noqa: E731
        prop, unit, agreement = get("property"), get("unit"), get("agreement")
        if unit and unit.property_id != prop.id:
            raise serializers.ValidationError({"unit": "Unit does not belong to the selected property."})
        if agreement and agreement.property_id != prop.id:
            raise serializers.ValidationError({"agreement": "Agreement does not belong to the selected property."})
        if agreement and not unit:
            attrs["unit"] = agreement.unit
        amount = get("amount")
        is_recoverable = get("is_recoverable")
        recoverable = attrs.get("recoverable_amount")
        if is_recoverable:
            if recoverable is None and (self.instance is None or not self.instance.is_recoverable):
                recoverable = amount
                attrs["recoverable_amount"] = amount
            recoverable = recoverable if recoverable is not None else self.instance.recoverable_amount
            if recoverable > amount:
                raise serializers.ValidationError({"recoverable_amount": "Cannot recover more than the expense amount."})
            if not agreement:
                raise serializers.ValidationError({"agreement": "Choose the tenant agreement to recover this expense from."})
        else:
            attrs["recoverable_amount"] = 0
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        expense = super().create(validated_data)
        _service_call(services.sync_expense_charge, expense)
        return expense

    @transaction.atomic
    def update(self, instance, validated_data):
        expense = super().update(instance, validated_data)
        _service_call(services.sync_expense_charge, expense)
        return expense


# ---------------------------------------------------------------------------
# Electricity
# ---------------------------------------------------------------------------


class MeterAssignmentSerializer(serializers.ModelSerializer):
    meter = OwnedPKField(Meter)
    agreement = OwnedPKField(Agreement)
    tenant_name = serializers.CharField(source="agreement.tenant.full_name", read_only=True)
    unit_name = serializers.CharField(source="agreement.units_label", read_only=True)

    class Meta:
        model = MeterAssignment
        fields = ["id", "meter", "agreement", "tenant_name", "unit_name", "start_date", "end_date", "share_percent", "notes"]

    def validate(self, attrs):
        meter = attrs.get("meter", getattr(self.instance, "meter", None))
        agreement = attrs.get("agreement", getattr(self.instance, "agreement", None))
        if meter.property_id != agreement.property_id:
            raise serializers.ValidationError({"agreement": "Agreement must be in the meter's property."})
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if end and end < start:
            raise serializers.ValidationError({"end_date": "End date must be after the start date."})
        overlapping = MeterAssignment.objects.filter(meter=meter, agreement=agreement, start_date__lte=end or start.max)
        overlapping = overlapping.exclude(end_date__lt=start)
        if self.instance:
            overlapping = overlapping.exclude(pk=self.instance.pk)
        if overlapping.exists():
            raise serializers.ValidationError({"detail": "This agreement is already linked to the meter for these dates."})
        return attrs


class MeterSerializer(serializers.ModelSerializer):
    property = OwnedPKField(Property)
    property_name = serializers.CharField(source="property.name", read_only=True)
    units = OwnedPKField(RentalUnit, many=True, required=False)
    unit_details = serializers.SerializerMethodField()
    assignments = MeterAssignmentSerializer(many=True, read_only=True)
    last_reading = serializers.SerializerMethodField()
    last_bill = serializers.SerializerMethodField()

    class Meta:
        model = Meter
        fields = [
            "id", "property", "property_name", "meter_number", "name", "meter_type", "units", "unit_details",
            "default_split", "is_active", "notes", "assignments", "last_reading", "last_bill",
        ]
        validators = []

    def get_unit_details(self, obj):
        period = services.month_start(services.today())
        result = []
        for unit in sorted(obj.units.all(), key=lambda u: u.name):
            agreement = services.agreement_for_unit(unit, period)
            result.append({
                "id": unit.id,
                "name": unit.name,
                "agreement_id": agreement.id if agreement else None,
                "tenant_name": agreement.tenant.full_name if agreement else None,
            })
        return result

    def get_last_bill(self, obj):
        bill = obj.bills.exclude(status=ElectricityBill.Status.CANCELLED).order_by("-period", "-id").first()
        if not bill:
            return None
        return {"id": bill.id, "period": bill.period, "amount": str(bill.amount), "status": bill.status}

    def get_last_reading(self, obj):
        reading = obj.readings.order_by("-reading_date").first()
        return {"date": reading.reading_date, "value": str(reading.current_reading)} if reading else None

    def validate(self, attrs):
        prop = attrs.get("property", getattr(self.instance, "property", None))
        number = attrs.get("meter_number", getattr(self.instance, "meter_number", ""))
        qs = Meter.objects.filter(property=prop, meter_number__iexact=number)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({"meter_number": "A meter with this number already exists here."})
        units = attrs.get("units")
        if units is not None and prop and any(u.property_id != prop.id for u in units):
            raise serializers.ValidationError({"units": "Units must belong to the meter's property."})
        return attrs


class MeterBillSerializer(serializers.Serializer):
    """One month's bill for a meter. `lines` maps unit id -> {"units": ..} or {"amount": ..}."""

    month = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    method = serializers.ChoiceField(choices=ElectricityBill.Method.choices)
    lines = serializers.JSONField(required=False, default=dict)
    bill_date = serializers.DateField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    total_units = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True, min_value=0)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    attachment = serializers.FileField(required=False, allow_null=True)
    preview = serializers.BooleanField(required=False, default=False)

    def validate_month(self, value):
        return _service_call(services.parse_month, value)

    def validate_lines(self, value):
        if isinstance(value, str):  # multipart forms send it as a JSON string
            import json

            try:
                value = json.loads(value or "{}")
            except ValueError:
                raise serializers.ValidationError("Invalid lines.")
        if not isinstance(value, dict):
            raise serializers.ValidationError("Lines must map unit id to values.")
        return value

    def validate_attachment(self, value):
        if value:
            # Same file type/size rules as a bill uploaded the normal way.
            for validator in ElectricityBill._meta.get_field("attachment").validators:
                try:
                    validator(value)
                except DjangoValidationError as exc:
                    raise serializers.ValidationError(exc.messages)
        return value


def split_to_json(split):
    return {
        "units": [
            {
                "unit_id": r["unit"].id,
                "unit_name": r["unit"].name,
                "agreement_id": r["agreement"].id if r["agreement"] else None,
                "tenant_name": r["agreement"].tenant.full_name if r["agreement"] else None,
                "units_used": str(r["units_used"]) if "units_used" in r else None,
                "amount": str(r["amount"]),
            }
            for r in split["units"]
        ],
        "lines": [
            {
                "agreement_id": l["agreement"].id,
                "tenant_name": l["agreement"].tenant.full_name,
                "unit_names": l["unit_names"],
                "units": str(l["units"]) if l["units"] is not None else None,
                "amount": str(services.money(l["amount"])),
            }
            for l in split["lines"]
        ],
        "total": str(split["total"]),
        "owner_pays": str(split["owner_pays"]),
    }


class MeterReadingSerializer(serializers.ModelSerializer):
    meter = OwnedPKField(Meter)
    meter_name = serializers.CharField(source="meter.name", read_only=True)
    previous_reading = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=0)

    class Meta:
        model = MeterReading
        fields = [
            "id", "meter", "meter_name", "reading_date", "previous_reading", "current_reading", "units_consumed",
            "is_reset", "reset_reason", "image", "notes", "created_at",
        ]
        read_only_fields = ["units_consumed"]
        extra_kwargs = {"image": {"write_only": True, "required": False}}
        validators = []

    def validate(self, attrs):
        if self.instance:
            raise serializers.ValidationError({"detail": "Readings cannot be edited. Delete and re-enter, or record a reset."})
        meter, day = attrs["meter"], attrs["reading_date"]
        if MeterReading.objects.filter(meter=meter, reading_date=day).exists():
            raise serializers.ValidationError({"reading_date": "A reading already exists for this meter and date."})
        if meter.readings.filter(reading_date__gt=day).exists():
            raise serializers.ValidationError({"reading_date": "A later reading exists. Readings must be entered in order."})
        last = services.previous_reading_for(meter, day)
        if attrs.get("is_reset"):
            if not attrs.get("reset_reason", "").strip():
                raise serializers.ValidationError({"reset_reason": "Explain the meter replacement or reset."})
            attrs.setdefault("previous_reading", 0)
        else:
            if last is not None:
                if "previous_reading" in attrs and attrs["previous_reading"] != last:
                    raise serializers.ValidationError(
                        {"previous_reading": f"Previous reading must be the last recorded value ({last}). Use a reset if the meter was replaced."}
                    )
                attrs["previous_reading"] = last
            elif "previous_reading" not in attrs:
                raise serializers.ValidationError({"previous_reading": "Enter the previous reading for the first entry."})
        if attrs["current_reading"] < attrs["previous_reading"]:
            raise serializers.ValidationError(
                {"current_reading": "Current reading cannot be lower than the previous reading. Use a meter reset if it was replaced."}
            )
        attrs["units_consumed"] = attrs["current_reading"] - attrs["previous_reading"]
        return attrs


class ElectricityAllocationSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source="agreement.tenant.full_name", read_only=True)
    unit_name = serializers.CharField(source="agreement.units_label", read_only=True)
    charge = serializers.SerializerMethodField()

    class Meta:
        model = ElectricityAllocation
        fields = ["id", "bill", "agreement", "tenant_name", "unit_name", "units", "amount", "charge", "notes"]

    def get_charge(self, obj):
        charge = getattr(obj, "charge", None)
        if not charge:
            return None
        net, paid, outstanding = services.charge_figures(charge)
        return {"id": charge.id, "net_amount": str(net), "paid": str(paid), "outstanding": str(outstanding)}


class ElectricityBillSerializer(serializers.ModelSerializer):
    property = OwnedPKField(Property)
    meter = OwnedPKField(Meter, required=False, allow_null=True)
    property_name = serializers.CharField(source="property.name", read_only=True)
    allocations = ElectricityAllocationSerializer(many=True, read_only=True)
    attachment_url = serializers.SerializerMethodField()
    attachment_open_url = serializers.SerializerMethodField()
    period = serializers.DateField(help_text="Any date in the billed month")

    class Meta:
        model = ElectricityBill
        fields = [
            "id", "property", "property_name", "meter", "period", "bill_date", "due_date", "amount", "total_units",
            "attachment", "attachment_url", "attachment_open_url", "allocation_method", "status", "unallocated_amount", "notes",
            "allocations", "created_at",
        ]
        read_only_fields = ["allocation_method", "status", "unallocated_amount"]
        extra_kwargs = {"attachment": {"write_only": True, "required": False}}

    def get_attachment_url(self, obj):
        if not obj.attachment:
            return None
        request = self.context.get("request")
        url = reverse("pm-bill-attachment", args=[obj.pk])
        return request.build_absolute_uri(url) if request else url

    def get_attachment_open_url(self, obj):
        return signed_file_url(self.context.get("request"), "bill", obj) if obj.attachment else None

    def validate(self, attrs):
        if "period" in attrs:
            attrs["period"] = services.month_start(attrs["period"])
        if self.instance and self.instance.status != ElectricityBill.Status.DRAFT:
            changed = [f for f in ("amount", "period", "property", "total_units") if f in attrs and attrs[f] != getattr(self.instance, f)]
            if changed:
                raise serializers.ValidationError({"detail": "An allocated bill's amounts cannot be edited. Correct allocations instead."})
        meter = attrs.get("meter", getattr(self.instance, "meter", None))
        prop = attrs.get("property", getattr(self.instance, "property", None))
        if meter and meter.property_id != prop.id:
            raise serializers.ValidationError({"meter": "Meter belongs to another property."})
        return attrs


class AllocateBillSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=ElectricityBill.Method.choices)
    agreement_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)
    lines = serializers.DictField(child=serializers.DictField(), required=False)
    use_readings = serializers.BooleanField(default=False)
    confirm = serializers.BooleanField(default=False)
    preview = serializers.BooleanField(default=False)


class CorrectAllocationSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    reason = serializers.CharField(max_length=200)


# ---------------------------------------------------------------------------
# Deposits & documents
# ---------------------------------------------------------------------------


class DepositTransactionSerializer(serializers.ModelSerializer):
    agreement = OwnedPKField(Agreement)

    class Meta:
        model = DepositTransaction
        fields = ["id", "agreement", "txn_type", "amount", "txn_date", "method", "payment", "notes", "created_at"]
        read_only_fields = ["payment"]

    def validate_txn_type(self, value):
        if value != DepositTransaction.TxnType.RECEIVED:
            raise serializers.ValidationError("Use the settle action for refunds, deductions and applying to dues.")
        return value


class SettleDepositSerializer(serializers.Serializer):
    agreement = OwnedPKField(Agreement)
    settle_date = serializers.DateField(required=False)
    apply_to_dues = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    deduction = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    deduction_reason = serializers.CharField(required=False, allow_blank=True, default="")
    refund = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    refund_method = serializers.ChoiceField(choices=Payment.Method.choices, default=Payment.Method.CASH)


class DocumentSerializer(serializers.ModelSerializer):
    property = OwnedPKField(Property, required=False, allow_null=True)
    tenant = OwnedPKField(Tenant, required=False, allow_null=True)
    agreement = OwnedPKField(Agreement, required=False, allow_null=True)
    download_url = serializers.SerializerMethodField()
    open_url = serializers.SerializerMethodField()
    file_name = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id", "property", "tenant", "agreement", "doc_type", "title", "file", "file_name", "download_url",
            "open_url", "created_at",
        ]
        extra_kwargs = {"file": {"write_only": True}}

    def get_download_url(self, obj):
        request = self.context.get("request")
        url = reverse("pm-document-download", args=[obj.pk])
        return request.build_absolute_uri(url) if request else url

    def get_open_url(self, obj):
        return signed_file_url(self.context.get("request"), "document", obj)

    def get_file_name(self, obj):
        return obj.file.name.rsplit("/", 1)[-1] if obj.file else None

    def validate(self, attrs):
        if not any(attrs.get(k) for k in ("property", "tenant", "agreement")) and not self.instance:
            raise serializers.ValidationError({"detail": "Attach the document to a property, tenant or agreement."})
        return attrs
