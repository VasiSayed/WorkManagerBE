"""Property management models.

Money rules
-----------
* All money uses DecimalField(max_digits=12, decimal_places=2).
* Balances are never stored. A tenant's outstanding amount is always derived from
  Charge (+ ChargeAdjustment) minus PaymentAllocation of non-reversed payments.
* Financial rows (charges, payments, allocations, adjustments, deposit transactions)
  are never edited in place through the API; corrections create new rows.
"""

import builtins
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

MONEY = {"max_digits": 12, "decimal_places": 2}
ZERO = Decimal("0.00")

DOCUMENT_EXTENSIONS = ["pdf", "jpg", "jpeg", "png", "webp", "heic", "doc", "docx"]
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def validate_upload_size(file):
    from django.core.exceptions import ValidationError

    if file and file.size > MAX_UPLOAD_BYTES:
        raise ValidationError("File is too large. Maximum size is 10 MB.")


def private_upload_path(instance, filename):
    # Random names keep tenant documents from being guessable.
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"property_management/{instance.owner_id or 'unknown'}/{uuid.uuid4().hex}.{ext}"


def file_field(**kwargs):
    return models.FileField(
        upload_to=private_upload_path,
        validators=[FileExtensionValidator(DOCUMENT_EXTENSIONS), validate_upload_size],
        **kwargs,
    )


class PMOwnedModel(models.Model):
    """Every row carries its owner so authorization is a single filter."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pm_%(class)s_items")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Properties, floors, sections, units
# ---------------------------------------------------------------------------


class Property(PMOwnedModel):
    class PropertyType(models.TextChoices):
        RESIDENTIAL = "residential", "Residential"
        COMMERCIAL = "commercial", "Commercial"
        MIXED = "mixed", "Mixed use"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=150)
    address = models.TextField(blank=True)
    property_type = models.CharField(max_length=20, choices=PropertyType.choices, default=PropertyType.RESIDENTIAL)
    description = models.TextField(blank=True)
    number_of_floors = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "properties"
        constraints = [models.UniqueConstraint(fields=["owner", "name"], name="pm_property_owner_name_uniq")]

    def __str__(self):
        return self.name


class Floor(PMOwnedModel):
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="floors")
    name = models.CharField(max_length=100)
    level = models.IntegerField(default=0, help_text="Sort order: 0 = ground, -1 = basement, 1 = first ...")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["property_id", "level", "name"]
        constraints = [models.UniqueConstraint(fields=["property", "name"], name="pm_floor_property_name_uniq")]

    def __str__(self):
        return f"{self.property} / {self.name}"


class FloorSection(PMOwnedModel):
    floor = models.ForeignKey(Floor, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["floor_id", "name"]
        constraints = [models.UniqueConstraint(fields=["floor", "name"], name="pm_section_floor_name_uniq")]

    def __str__(self):
        return f"{self.floor} / {self.name}"


class RentalUnit(PMOwnedModel):
    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        MAINTENANCE = "maintenance", "Under maintenance"
        INACTIVE = "inactive", "Inactive"

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="units")
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["property_id", "name"]
        constraints = [models.UniqueConstraint(fields=["property", "name"], name="pm_unit_property_name_uniq")]

    def __str__(self):
        return f"{self.property} / {self.name}"


class UnitSpace(models.Model):
    """A piece of a floor that a unit occupies. section=None means the entire floor.

    A unit may hold several spaces across several floors (e.g. a ground-floor shop
    section plus half of the first floor).
    """

    unit = models.ForeignKey(RentalUnit, on_delete=models.CASCADE, related_name="spaces")
    floor = models.ForeignKey(Floor, on_delete=models.PROTECT, related_name="unit_spaces")
    section = models.ForeignKey(FloorSection, on_delete=models.PROTECT, null=True, blank=True, related_name="unit_spaces")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["unit", "floor", "section"], name="pm_unitspace_uniq"),
            models.UniqueConstraint(
                fields=["unit", "floor"], condition=Q(section__isnull=True), name="pm_unitspace_whole_floor_uniq"
            ),
        ]

    def __str__(self):
        return f"{self.unit.name}: {self.section or self.floor.name + ' (entire floor)'}"


# ---------------------------------------------------------------------------
# Tenants and agreements
# ---------------------------------------------------------------------------


class Tenant(PMOwnedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        FORMER = "former", "Former"
        PROSPECTIVE = "prospective", "Prospective"

    full_name = models.CharField(max_length=150)
    mobile = models.CharField(max_length=20)
    alternate_mobile = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    id_type = models.CharField(max_length=50, blank=True, help_text="e.g. Aadhaar, PAN, Passport")
    id_number = models.CharField(max_length=50, blank=True)
    emergency_contact_name = models.CharField(max_length=150, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["full_name"]
        indexes = [models.Index(fields=["owner", "status"], name="pm_tenant_owner_status_idx")]

    def __str__(self):
        return self.full_name


class Agreement(PMOwnedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        RENEWED = "renewed", "Renewed"
        ENDED = "ended", "Ended"
        TERMINATED = "terminated", "Terminated"

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="agreements")
    # Main unit. A tenant renting several units together under one rent lists the others in additional_units.
    unit = models.ForeignKey(RentalUnit, on_delete=models.PROTECT, related_name="agreements")
    additional_units = models.ManyToManyField(RentalUnit, blank=True, related_name="additional_agreements")
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="agreements")
    # Rent at the start of the agreement. Later changes live in RentRevision.
    monthly_rent = models.DecimalField(**MONEY, validators=[MinValueValidator(ZERO)])
    rent_due_day = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(31)])
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Agreement expiry date")
    billing_start_date = models.DateField(
        help_text="Monthly charges are generated from this month onward (lets you skip months settled before using the app)."
    )
    billing_end_date = models.DateField(
        null=True, blank=True, help_text="Last billing month (set automatically when the agreement ends or is renewed)."
    )
    security_deposit_amount = models.DecimalField(**MONEY, default=ZERO, validators=[MinValueValidator(ZERO)])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    move_out_date = models.DateField(null=True, blank=True)
    renewed_from = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="renewed_to"
    )
    reminders_enabled = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-start_date", "-id"]
        indexes = [models.Index(fields=["owner", "status"], name="pm_agr_owner_status_idx")]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")),
                name="pm_agreement_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.tenant} @ {self.units_label}"

    @builtins.property  # plain @property is shadowed by the "property" field above
    def all_units(self):
        """Main unit first, then any additional units (uses prefetched additional_units when available)."""
        return [self.unit, *sorted(self.additional_units.all(), key=lambda u: u.name)]

    @builtins.property
    def all_unit_ids(self):
        return [u.id for u in self.all_units]

    @builtins.property
    def units_label(self):
        return " + ".join(u.name for u in self.all_units)


class RentRevision(PMOwnedModel):
    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT, related_name="rent_revisions")
    previous_rent = models.DecimalField(**MONEY)
    new_rent = models.DecimalField(**MONEY, validators=[MinValueValidator(ZERO)])
    effective_date = models.DateField(help_text="Applies from the billing month containing this date.")
    reason = models.CharField(max_length=255, blank=True)
    document = models.ForeignKey("Document", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["agreement_id", "effective_date", "id"]


class ReminderSettings(PMOwnedModel):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pm_reminder_settings")
    enabled = models.BooleanField(default=False)
    days_before = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1), MaxValueValidator(365)])

    class Meta:
        verbose_name_plural = "reminder settings"


class AgreementReminder(PMOwnedModel):
    """In-app renewal reminder. One per agreement + expiry date + reminder period."""

    agreement = models.ForeignKey(Agreement, on_delete=models.CASCADE, related_name="reminders")
    expiry_date = models.DateField()
    days_before = models.PositiveSmallIntegerField()
    remind_on = models.DateField()
    dismissed = models.BooleanField(default=False)

    class Meta:
        ordering = ["remind_on"]
        constraints = [
            models.UniqueConstraint(
                fields=["agreement", "expiry_date", "days_before"], name="pm_reminder_no_duplicates"
            )
        ]


# ---------------------------------------------------------------------------
# Recurring charges, ledger (charges, payments, allocations, adjustments)
# ---------------------------------------------------------------------------


class RecurringCharge(PMOwnedModel):
    class Frequency(models.IntegerChoices):
        MONTHLY = 1, "Monthly"
        QUARTERLY = 3, "Quarterly"
        HALF_YEARLY = 6, "Half-yearly"
        YEARLY = 12, "Yearly"

    name = models.CharField(max_length=100)
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    frequency = models.PositiveSmallIntegerField(choices=Frequency.choices, default=Frequency.MONTHLY)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    # Exactly one scope is set; property/unit scopes apply to every agreement under them.
    property = models.ForeignKey(Property, on_delete=models.PROTECT, null=True, blank=True, related_name="recurring_charges")
    unit = models.ForeignKey(RentalUnit, on_delete=models.PROTECT, null=True, blank=True, related_name="recurring_charges")
    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT, null=True, blank=True, related_name="recurring_charges")
    is_enabled = models.BooleanField(default=True)
    included_in_monthly_due = models.BooleanField(default=True)
    revised_from = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True, related_name="revised_to")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name", "start_date"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(property__isnull=False, unit__isnull=True, agreement__isnull=True)
                    | Q(property__isnull=True, unit__isnull=False, agreement__isnull=True)
                    | Q(property__isnull=True, unit__isnull=True, agreement__isnull=False)
                ),
                name="pm_recurring_exactly_one_scope",
            ),
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")),
                name="pm_recurring_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.amount})"


class Charge(PMOwnedModel):
    """Anything a tenant owes. The single source of receivables."""

    class ChargeType(models.TextChoices):
        RENT = "rent", "Rent"
        FIXED = "fixed", "Fixed monthly charge"
        ELECTRICITY = "electricity", "Electricity"
        EXPENSE = "expense", "Recoverable expense"
        ONE_TIME = "one_time", "One-time charge"

    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT, related_name="charges")
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="charges")
    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="charges")
    charge_type = models.CharField(max_length=20, choices=ChargeType.choices)
    period = models.DateField(null=True, blank=True, help_text="First day of the billing month, if periodic.")
    description = models.CharField(max_length=255)
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    due_date = models.DateField()
    included_in_monthly_due = models.BooleanField(default=False)
    recurring_charge = models.ForeignKey(RecurringCharge, on_delete=models.PROTECT, null=True, blank=True, related_name="charges")
    electricity_allocation = models.OneToOneField(
        "ElectricityAllocation", on_delete=models.PROTECT, null=True, blank=True, related_name="charge"
    )
    expense = models.OneToOneField("Expense", on_delete=models.PROTECT, null=True, blank=True, related_name="charge")
    is_void = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["due_date", "id"]
        indexes = [
            models.Index(fields=["owner", "period", "charge_type"], name="pm_charge_owner_period_idx"),
            models.Index(fields=["agreement", "due_date"], name="pm_charge_agr_due_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["agreement", "period"], condition=Q(charge_type="rent"), name="pm_one_rent_charge_per_period"
            ),
            models.UniqueConstraint(
                fields=["agreement", "recurring_charge", "period"],
                condition=Q(recurring_charge__isnull=False),
                name="pm_one_recurring_charge_per_period",
            ),
        ]

    def __str__(self):
        return f"{self.description} – {self.amount}"


class ChargeAdjustment(PMOwnedModel):
    """Controlled change to what is owed on a charge. Negative = waiver/discount."""

    charge = models.ForeignKey(Charge, on_delete=models.PROTECT, related_name="adjustments")
    amount = models.DecimalField(**MONEY)
    reason = models.CharField(max_length=255)

    class Meta:
        ordering = ["created_at"]
        constraints = [models.CheckConstraint(condition=~Q(amount=0), name="pm_adjustment_nonzero")]


class Payment(PMOwnedModel):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        DEPOSIT = "security_deposit", "Security deposit"
        OTHER = "other", "Other"

    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT, related_name="payments")
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    payment_date = models.DateField()
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.CASH)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    is_reversed = models.BooleanField(default=False)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-payment_date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="pm_payment_idempotency_uniq",
            ),
        ]


class PaymentAllocation(models.Model):
    """Which part of a payment settled which charge. Unallocated remainder = advance credit."""

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="allocations")
    charge = models.ForeignKey(Charge, on_delete=models.PROTECT, related_name="allocations")
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


class Expense(PMOwnedModel):
    class Category(models.TextChoices):
        PLUMBING = "plumbing", "Plumbing"
        ELECTRICAL = "electrical", "Electrical"
        CARPENTRY = "carpentry", "Carpentry / doors"
        LOCKS = "locks", "Locks & keys"
        PAINTING = "painting", "Painting"
        MAINTENANCE = "maintenance", "General maintenance"
        TAX = "tax", "Tax / fees"
        OTHER = "other", "Other"

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="expenses")
    unit = models.ForeignKey(RentalUnit, on_delete=models.PROTECT, null=True, blank=True, related_name="expenses")
    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT, null=True, blank=True, related_name="expenses")
    title = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    expense_date = models.DateField()
    is_recoverable = models.BooleanField(default=False)
    recoverable_amount = models.DecimalField(**MONEY, default=ZERO, validators=[MinValueValidator(ZERO)])
    receipt = file_field(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-expense_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(recoverable_amount__lte=models.F("amount")), name="pm_expense_recoverable_lte_amount"
            ),
        ]

    def __str__(self):
        return self.title


# ---------------------------------------------------------------------------
# Electricity
# ---------------------------------------------------------------------------


class Meter(PMOwnedModel):
    class MeterType(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        SHARED = "shared", "Shared"
        COMMON = "common_area", "Common area"
        MAIN = "main", "Main / provider"

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="meters")
    meter_number = models.CharField(max_length=60)
    name = models.CharField(max_length=100)
    meter_type = models.CharField(max_length=20, choices=MeterType.choices, default=MeterType.INDIVIDUAL)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["property_id", "name"]
        constraints = [models.UniqueConstraint(fields=["property", "meter_number"], name="pm_meter_number_uniq")]

    def __str__(self):
        return f"{self.name} ({self.meter_number})"


class MeterAssignment(PMOwnedModel):
    """Dated link between a meter and an agreement. Shared meters have several open rows."""

    meter = models.ForeignKey(Meter, on_delete=models.PROTECT, related_name="assignments")
    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT, related_name="meter_assignments")
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    share_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(Decimal("100"))],
        help_text="For shared meters: share of this meter's units attributed to the agreement.",
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["meter_id", "start_date"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")),
                name="pm_meterassign_end_after_start",
            ),
        ]


class MeterReading(PMOwnedModel):
    meter = models.ForeignKey(Meter, on_delete=models.PROTECT, related_name="readings")
    reading_date = models.DateField()
    previous_reading = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(ZERO)])
    current_reading = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(ZERO)])
    units_consumed = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(ZERO)])
    is_reset = models.BooleanField(
        default=False, help_text="Meter replaced or reset; previous_reading is the new meter's starting value."
    )
    reset_reason = models.CharField(max_length=255, blank=True)
    image = models.ImageField(upload_to=private_upload_path, null=True, blank=True, validators=[validate_upload_size])
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["meter_id", "-reading_date"]
        constraints = [
            models.UniqueConstraint(fields=["meter", "reading_date"], name="pm_reading_meter_date_uniq"),
            models.CheckConstraint(
                condition=Q(current_reading__gte=models.F("previous_reading")), name="pm_reading_not_decreasing"
            ),
        ]


class ElectricityBill(PMOwnedModel):
    class Method(models.TextChoices):
        EQUAL = "equal", "Equal split"
        METER = "meter", "Meter-based split"
        MANUAL = "manual", "Manual allocation"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ALLOCATED = "allocated", "Allocated"
        CANCELLED = "cancelled", "Cancelled"

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="electricity_bills")
    meter = models.ForeignKey(Meter, on_delete=models.PROTECT, null=True, blank=True, related_name="bills")
    period = models.DateField(help_text="First day of the billed month.")
    bill_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    total_units = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(ZERO)])
    attachment = file_field(null=True, blank=True)
    allocation_method = models.CharField(max_length=10, choices=Method.choices, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    unallocated_amount = models.DecimalField(
        **MONEY, default=ZERO, help_text="Portion the owner chose to absorb when allocations were less than the bill."
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-period", "-id"]


class ElectricityAllocation(PMOwnedModel):
    bill = models.ForeignKey(ElectricityBill, on_delete=models.PROTECT, related_name="allocations")
    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT, related_name="electricity_allocations")
    units = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(ZERO)])
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["bill_id", "id"]
        constraints = [models.UniqueConstraint(fields=["bill", "agreement"], name="pm_one_allocation_per_bill_agreement")]


# ---------------------------------------------------------------------------
# Security deposits & documents
# ---------------------------------------------------------------------------


class DepositTransaction(PMOwnedModel):
    class TxnType(models.TextChoices):
        RECEIVED = "received", "Received"
        REFUNDED = "refunded", "Refunded"
        APPLIED_TO_DUES = "applied_to_dues", "Applied to outstanding dues"
        DEDUCTION = "deduction", "Deduction (damages etc.)"
        TRANSFERRED_OUT = "transferred_out", "Transferred to renewed agreement"
        TRANSFERRED_IN = "transferred_in", "Transferred from previous agreement"

    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT, related_name="deposit_transactions")
    txn_type = models.CharField(max_length=20, choices=TxnType.choices)
    amount = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0.01"))])
    txn_date = models.DateField()
    method = models.CharField(max_length=20, choices=Payment.Method.choices, blank=True)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["txn_date", "id"]


class Document(PMOwnedModel):
    class DocType(models.TextChoices):
        AGREEMENT = "agreement", "Signed agreement"
        IDENTITY = "identity", "Identity document"
        DEPOSIT_RECEIPT = "deposit_receipt", "Deposit receipt"
        PROPERTY_IMAGE = "property_image", "Property image"
        PROPERTY_DOC = "property_document", "Property document"
        OTHER = "other", "Other"

    property = models.ForeignKey(Property, on_delete=models.CASCADE, null=True, blank=True, related_name="documents")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name="documents")
    agreement = models.ForeignKey(Agreement, on_delete=models.CASCADE, null=True, blank=True, related_name="documents")
    doc_type = models.CharField(max_length=30, choices=DocType.choices, default=DocType.OTHER)
    title = models.CharField(max_length=150)
    file = file_field()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(property__isnull=False) | Q(tenant__isnull=False) | Q(agreement__isnull=False),
                name="pm_document_has_parent",
            )
        ]

    def __str__(self):
        return self.title
