"""Business logic for property management.

Views stay thin and call into these functions. Every function that writes money
runs inside a database transaction.

Ledger rules
------------
* charge net amount   = Charge.amount + sum(ChargeAdjustment.amount)
* charge paid         = sum(PaymentAllocation.amount) of non-reversed payments
* charge outstanding  = net - paid           (void charges count as 0)
* advance credit      = sum(payment.amount - allocated) of non-reversed payments
* net balance         = total outstanding - advance credit
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import DecimalField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (
    ZERO,
    Agreement,
    AgreementReminder,
    Charge,
    ChargeAdjustment,
    DepositTransaction,
    ElectricityAllocation,
    ElectricityBill,
    Meter,
    MeterAssignment,
    MeterReading,
    Payment,
    PaymentAllocation,
    RecurringCharge,
    ReminderSettings,
    RentRevision,
    Tenant,
    UnitSpace,
)

CENT = Decimal("0.01")
DEFAULT_EXPIRING_DAYS = 60
# Default order when a payment does not say which charges it settles.
CHARGE_TYPE_PRIORITY = {
    Charge.ChargeType.RENT: 0,
    Charge.ChargeType.FIXED: 1,
    Charge.ChargeType.ELECTRICITY: 2,
    Charge.ChargeType.EXPENSE: 3,
    Charge.ChargeType.ONE_TIME: 4,
}


class ServiceError(Exception):
    """A business-rule violation. `code` lets the client react (e.g. ask for confirmation)."""

    def __init__(self, message, code="invalid", extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}


# ---------------------------------------------------------------------------
# Date & money helpers
# ---------------------------------------------------------------------------


def money(value):
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def today():
    # Local (Asia/Kolkata) date, never the UTC date.
    return timezone.localdate()


def month_start(day):
    return day.replace(day=1)


def add_months(day, months):
    index = day.year * 12 + (day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def month_end(day):
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def months_between(start, end):
    return (end.year - start.year) * 12 + (end.month - start.month)


def parse_month(value, default=None):
    """Accepts 'YYYY-MM' or 'YYYY-MM-DD'; returns the first day of that month."""
    if not value:
        return default or month_start(today())
    try:
        year, month = [int(part) for part in str(value)[:7].split("-")]
        return date(year, month, 1)
    except (TypeError, ValueError):
        raise ServiceError("Month must be in YYYY-MM format.", code="invalid_month")


def due_date_for(period, due_day):
    last = calendar.monthrange(period.year, period.month)[1]
    return period.replace(day=min(int(due_day or 1), last))


def month_label(period):
    return period.strftime("%b %Y")


def distribute(total, weights):
    """Split `total` by `weights` into cents so the parts add up exactly to `total`.

    Uses the largest-remainder method: everyone gets the rounded-down share, and the
    leftover cents go to the largest fractional remainders (earlier lines win ties).
    """
    total = money(total)
    weights = [Decimal(w) for w in weights]
    weight_sum = sum(weights)
    if not weights or weight_sum <= 0:
        raise ServiceError("Cannot split an amount when the total weight is zero.", code="zero_weight")
    raw = [total * w / weight_sum for w in weights]
    floored = [r.quantize(CENT, rounding=ROUND_DOWN) for r in raw]
    leftover_cents = int(((total - sum(floored)) / CENT).to_integral_value())
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floored[i]), i))
    for i in order[:leftover_cents]:
        floored[i] += CENT
    return floored


# ---------------------------------------------------------------------------
# Charge / payment annotations
# ---------------------------------------------------------------------------

_DEC = DecimalField(max_digits=14, decimal_places=2)


def annotate_charges(queryset):
    """Adds net_amount, paid_amount and outstanding to a Charge queryset (subqueries avoid join fan-out)."""
    adjustments = (
        ChargeAdjustment.objects.filter(charge=OuterRef("pk"))
        .values("charge")
        .annotate(total=Sum("amount"))
        .values("total")
    )
    allocations = (
        PaymentAllocation.objects.filter(charge=OuterRef("pk"), payment__is_reversed=False)
        .values("charge")
        .annotate(total=Sum("amount"))
        .values("total")
    )
    return queryset.annotate(
        adjustment_total=Coalesce(Subquery(adjustments, output_field=_DEC), Value(ZERO), output_field=_DEC),
        paid_amount=Coalesce(Subquery(allocations, output_field=_DEC), Value(ZERO), output_field=_DEC),
    )


def charge_figures(charge):
    """Returns (net, paid, outstanding) for an annotated or plain charge."""
    if hasattr(charge, "paid_amount"):
        adjustment, paid = charge.adjustment_total, charge.paid_amount
    else:
        adjustment = charge.adjustments.aggregate(t=Sum("amount"))["t"] or ZERO
        paid = charge.allocations.filter(payment__is_reversed=False).aggregate(t=Sum("amount"))["t"] or ZERO
    if charge.is_void:
        return ZERO, money(paid), ZERO
    net = money(charge.amount + adjustment)
    return net, money(paid), money(net - paid)


def annotate_payments(queryset):
    allocated = (
        PaymentAllocation.objects.filter(payment=OuterRef("pk"))
        .values("payment")
        .annotate(total=Sum("amount"))
        .values("total")
    )
    return queryset.annotate(
        allocated_amount=Coalesce(Subquery(allocated, output_field=_DEC), Value(ZERO), output_field=_DEC)
    )


def payment_unapplied(payment):
    if payment.is_reversed:
        return ZERO
    allocated = getattr(payment, "allocated_amount", None)
    if allocated is None:
        allocated = payment.allocations.aggregate(t=Sum("amount"))["t"] or ZERO
    return money(payment.amount - allocated)


def charge_status(net, paid, outstanding, due_date, on_date=None):
    on_date = on_date or today()
    if net <= 0 or outstanding <= 0:
        return "paid"
    if due_date and due_date < on_date:
        return "overdue"
    if paid > 0:
        return "partially_paid"
    return "unpaid"


def outstanding_charges(agreement_ids):
    """Unpaid, non-void charges in default settlement order."""
    charges = annotate_charges(Charge.objects.filter(agreement_id__in=agreement_ids, is_void=False))
    result = []
    for charge in charges:
        net, paid, outstanding = charge_figures(charge)
        if outstanding > 0:
            result.append((charge, outstanding))
    result.sort(key=lambda item: (item[0].due_date, CHARGE_TYPE_PRIORITY.get(item[0].charge_type, 9), item[0].id))
    return result


# ---------------------------------------------------------------------------
# Rent and charge generation
# ---------------------------------------------------------------------------


def rent_for_period(agreement, period, revisions=None):
    """Rent applicable to a billing month. A revision applies from the month containing its effective date."""
    if revisions is None:
        revisions = list(agreement.rent_revisions.all())
    rent = agreement.monthly_rent
    for revision in sorted(revisions, key=lambda r: (r.effective_date, r.id)):
        if month_start(revision.effective_date) <= period:
            rent = revision.new_rent
    return money(rent)


def agreement_bills_period(agreement, period):
    if period < month_start(agreement.billing_start_date):
        return False
    if agreement.billing_end_date and period > month_start(agreement.billing_end_date):
        return False
    if agreement.status != Agreement.Status.ACTIVE and not agreement.billing_end_date:
        return False
    return True


def recurring_applies(rc, agreement, period):
    if not rc.is_enabled:
        return False
    start = month_start(rc.start_date)
    if period < start or (rc.end_date and period > month_start(rc.end_date)):
        return False
    if months_between(start, period) % rc.frequency:
        return False
    if rc.agreement_id:
        return rc.agreement_id == agreement.id
    if rc.unit_id:
        return rc.unit_id in agreement.all_unit_ids
    return rc.property_id == agreement.property_id


def generate_charges(owner, up_to=None, agreements=None):
    """Create any missing rent / fixed charges up to and including `up_to` (a month).

    Safe to call repeatedly: existing (agreement, period) rows are skipped and the
    unique constraints make concurrent calls harmless. Never generates beyond the
    current month. Returns the number of charges created.
    """
    current = month_start(today())
    up_to = min(month_start(up_to or current), current)
    if agreements is None:
        agreements = Agreement.objects.filter(owner=owner).exclude(billing_start_date__gt=month_end(up_to))
        agreements = agreements.filter(Q(status=Agreement.Status.ACTIVE) | Q(billing_end_date__isnull=False))
    agreements = list(agreements.select_related("tenant", "unit").prefetch_related("rent_revisions", "additional_units"))
    if not agreements:
        return 0

    ids = [a.id for a in agreements]
    existing_rent = set(
        Charge.objects.filter(agreement_id__in=ids, charge_type=Charge.ChargeType.RENT).values_list("agreement_id", "period")
    )
    existing_recurring = set(
        Charge.objects.filter(agreement_id__in=ids, recurring_charge__isnull=False).values_list(
            "agreement_id", "recurring_charge_id", "period"
        )
    )
    recurring = list(RecurringCharge.objects.filter(owner=owner, is_enabled=True))

    to_create = []
    for agreement in agreements:
        revisions = list(agreement.rent_revisions.all())
        period = month_start(agreement.billing_start_date)
        last = up_to
        if agreement.billing_end_date:
            last = min(last, month_start(agreement.billing_end_date))
        while period <= last:
            due = due_date_for(period, agreement.rent_due_day)
            if (agreement.id, period) not in existing_rent:
                rent = rent_for_period(agreement, period, revisions)
                if rent > 0:
                    to_create.append(
                        Charge(
                            owner_id=agreement.owner_id,
                            agreement=agreement,
                            tenant_id=agreement.tenant_id,
                            property_id=agreement.property_id,
                            charge_type=Charge.ChargeType.RENT,
                            period=period,
                            description=f"Rent {month_label(period)}",
                            amount=rent,
                            due_date=due,
                            included_in_monthly_due=True,
                        )
                    )
            for rc in recurring:
                if (agreement.id, rc.id, period) in existing_recurring or not recurring_applies(rc, agreement, period):
                    continue
                to_create.append(
                    Charge(
                        owner_id=agreement.owner_id,
                        agreement=agreement,
                        tenant_id=agreement.tenant_id,
                        property_id=agreement.property_id,
                        charge_type=Charge.ChargeType.FIXED,
                        period=period,
                        description=f"{rc.name} {month_label(period)}",
                        amount=rc.amount,
                        due_date=due,
                        included_in_monthly_due=rc.included_in_monthly_due,
                        recurring_charge=rc,
                    )
                )
            period = add_months(period, 1)

    if to_create:
        with transaction.atomic():
            Charge.objects.bulk_create(to_create, ignore_conflicts=True)
    return len(to_create)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


@dataclass
class PaymentResult:
    payment: Payment
    created: bool
    allocations: list = field(default_factory=list)
    unapplied: Decimal = ZERO


def _allocate(payment, targets, amount):
    """Allocate up to `amount` of `payment` across (charge, outstanding) pairs, in order."""
    remaining = money(amount)
    rows = []
    for charge, outstanding in targets:
        if remaining <= 0:
            break
        portion = min(outstanding, remaining)
        if portion <= 0:
            continue
        rows.append(PaymentAllocation(payment=payment, charge=charge, amount=portion))
        remaining -= portion
    PaymentAllocation.objects.bulk_create(rows)
    return rows, money(remaining)


def record_payment(
    *,
    owner,
    user,
    agreement,
    amount,
    payment_date=None,
    method=Payment.Method.CASH,
    reference="",
    notes="",
    charge_ids=None,
    idempotency_key=None,
    allow_excess=False,
):
    """Record a payment and settle charges.

    * charge_ids given  -> settle those charges in the given order.
    * charge_ids absent -> default rule: oldest due date first; rent, then fixed,
      electricity, expenses, one-time charges.
    * If the payment is larger than what it can settle, the caller must pass
      allow_excess=True; the excess is kept as advance credit on the agreement.
    * The same idempotency_key always returns the original payment instead of
      creating a second one.
    """
    amount = money(amount)
    if amount <= 0:
        raise ServiceError("Payment amount must be greater than zero.", code="invalid_amount")
    payment_date = payment_date or today()
    if payment_date > today() + timedelta(days=1):
        raise ServiceError("Payment date cannot be in the future.", code="invalid_date")

    if idempotency_key:
        existing = Payment.objects.filter(owner=owner, idempotency_key=idempotency_key).first()
        if existing:
            return _existing_payment_result(existing, agreement, amount)

    # Make sure this month's rent/fixed charges exist before deciding what the payment settles.
    generate_charges(owner, agreements=Agreement.objects.filter(pk=agreement.pk))

    try:
        with transaction.atomic():
            # Serialize payments per agreement so two requests cannot double-allocate one charge.
            locked = Agreement.objects.select_for_update().get(pk=agreement.pk)
            if charge_ids:
                ids = [int(i) for i in charge_ids]
                if len(set(ids)) != len(ids):
                    raise ServiceError("A charge was selected twice.", code="duplicate_charge")
                charges = annotate_charges(Charge.objects.filter(agreement=locked, id__in=ids))
                by_id = {c.id: c for c in charges}
                missing = [i for i in ids if i not in by_id]
                if missing:
                    raise ServiceError(f"Charges not found for this agreement: {missing}", code="invalid_charges")
                targets = []
                for i in ids:
                    _, _, outstanding = charge_figures(by_id[i])
                    if by_id[i].is_void or outstanding <= 0:
                        raise ServiceError(f"Charge '{by_id[i].description}' is already settled.", code="charge_settled")
                    targets.append((by_id[i], outstanding))
            else:
                targets = outstanding_charges([locked.id])

            settleable = money(sum(o for _, o in targets))
            excess = money(amount - settleable)
            if excess > 0 and not allow_excess:
                raise ServiceError(
                    f"Payment exceeds the amount due by ₹{excess}. Confirm to keep the excess as advance credit.",
                    code="overpayment_confirmation_required",
                    extra={"amount_due": str(settleable), "excess": str(excess)},
                )

            payment = Payment.objects.create(
                owner=owner,
                agreement=locked,
                tenant_id=locked.tenant_id,
                amount=amount,
                payment_date=payment_date,
                method=method,
                reference=reference or "",
                notes=notes or "",
                idempotency_key=idempotency_key or None,
                recorded_by=user,
            )
            rows, unapplied = _allocate(payment, targets, amount)
            return PaymentResult(payment=payment, created=True, allocations=rows, unapplied=unapplied)
    except IntegrityError:
        # Lost a race against an identical request with the same idempotency key.
        existing = Payment.objects.filter(owner=owner, idempotency_key=idempotency_key).first() if idempotency_key else None
        if existing:
            return _existing_payment_result(existing, agreement, amount)
        raise


def _existing_payment_result(existing, agreement, amount):
    if existing.agreement_id != agreement.id or existing.amount != amount:
        raise ServiceError(
            "This request key was already used for a different payment.", code="idempotency_conflict"
        )
    return PaymentResult(
        payment=existing,
        created=False,
        allocations=list(existing.allocations.all()),
        unapplied=payment_unapplied(existing),
    )


def reverse_payment(payment, reason):
    if not reason or not reason.strip():
        raise ServiceError("A reason is required to reverse a payment.", code="reason_required")
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.is_reversed:
            raise ServiceError("Payment is already reversed.", code="already_reversed")
        if payment.method == Payment.Method.DEPOSIT:
            raise ServiceError(
                "Payments made from the security deposit cannot be reversed here.", code="deposit_payment"
            )
        payment.is_reversed = True
        payment.reversed_at = timezone.now()
        payment.reversal_reason = reason.strip()
        payment.save(update_fields=["is_reversed", "reversed_at", "reversal_reason", "updated_at"])
    return payment


def apply_credit(agreement):
    """Apply unallocated payment amounts (advance credit) to outstanding charges using the default order."""
    with transaction.atomic():
        Agreement.objects.select_for_update().get(pk=agreement.pk)
        payments = [
            p
            for p in annotate_payments(
                Payment.objects.filter(agreement=agreement, is_reversed=False).order_by("payment_date", "id")
            )
            if payment_unapplied(p) > 0
        ]
        targets = outstanding_charges([agreement.id])
        created = []
        for payment in payments:
            if not targets:
                break
            rows, _ = _allocate(payment, targets, payment_unapplied(payment))
            created.extend(rows)
            settled = {}
            for row in rows:
                settled[row.charge_id] = settled.get(row.charge_id, ZERO) + row.amount
            targets = [(c, money(o - settled.get(c.id, ZERO))) for c, o in targets]
            targets = [(c, o) for c, o in targets if o > 0]
        return created


def adjust_charge(charge, amount, reason, owner):
    amount = money(amount)
    if amount == 0:
        raise ServiceError("Adjustment amount cannot be zero.", code="invalid_amount")
    if not reason or not reason.strip():
        raise ServiceError("A reason is required for an adjustment.", code="reason_required")
    with transaction.atomic():
        Agreement.objects.select_for_update().get(pk=charge.agreement_id)
        if charge.is_void:
            raise ServiceError("Cannot adjust a void charge.", code="charge_void")
        net, paid, _ = charge_figures(charge)
        if net + amount < paid:
            raise ServiceError(
                f"Adjustment would make the charge (₹{net + amount}) less than what is already paid (₹{paid}). "
                "Reverse the payment first.",
                code="below_paid",
            )
        if net + amount < 0:
            raise ServiceError("A charge cannot become negative.", code="negative_charge")
        return ChargeAdjustment.objects.create(owner=owner, charge=charge, amount=amount, reason=reason.strip())


def void_charge(charge, reason):
    if not reason or not reason.strip():
        raise ServiceError("A reason is required to void a charge.", code="reason_required")
    with transaction.atomic():
        charge = Charge.objects.select_for_update().get(pk=charge.pk)
        if charge.is_void:
            raise ServiceError("Charge is already void.", code="charge_void")
        if charge.allocations.filter(payment__is_reversed=False).exists():
            raise ServiceError("Charge has payments. Reverse them first or use an adjustment.", code="has_payments")
        charge.is_void = True
        charge.void_reason = reason.strip()
        charge.save(update_fields=["is_void", "void_reason", "updated_at"])
    return charge


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


def balance_summary(agreement_ids):
    """Outstanding per charge type, totals and advance credit for one or more agreements."""
    by_type = defaultdict(lambda: ZERO)
    total_charges = ZERO
    total_paid = ZERO
    overdue = ZERO
    on = today()
    charges = annotate_charges(Charge.objects.filter(agreement_id__in=agreement_ids))
    for charge in charges:
        net, paid, outstanding = charge_figures(charge)
        total_charges += net
        total_paid += min(paid, net) if not charge.is_void else ZERO
        if outstanding > 0:
            by_type[charge.charge_type] += outstanding
            if charge.due_date < on:
                overdue += outstanding
    payments = annotate_payments(Payment.objects.filter(agreement_id__in=agreement_ids, is_reversed=False))
    credit = money(sum((payment_unapplied(p) for p in payments), ZERO))
    total_outstanding = money(sum(by_type.values(), ZERO))
    return {
        "rent": money(by_type[Charge.ChargeType.RENT]),
        "fixed": money(by_type[Charge.ChargeType.FIXED]),
        "electricity": money(by_type[Charge.ChargeType.ELECTRICITY]),
        "expense": money(by_type[Charge.ChargeType.EXPENSE]),
        "one_time": money(by_type[Charge.ChargeType.ONE_TIME]),
        "total_charges": money(total_charges),
        "total_paid": money(total_paid),
        "total_outstanding": total_outstanding,
        "overdue": money(overdue),
        "advance_credit": credit,
        "net_balance": money(total_outstanding - credit),
    }


def deposit_balance(agreement_ids):
    plus = (DepositTransaction.TxnType.RECEIVED, DepositTransaction.TxnType.TRANSFERRED_IN)
    totals = defaultdict(lambda: ZERO)
    for txn_type, amount in DepositTransaction.objects.filter(agreement_id__in=agreement_ids).values_list(
        "txn_type", "amount"
    ):
        totals[txn_type] += amount
    held = sum((totals[t] for t in plus), ZERO) - sum(
        (v for k, v in totals.items() if k not in plus), ZERO
    )
    return {
        "received": money(totals[DepositTransaction.TxnType.RECEIVED]),
        "refunded": money(totals[DepositTransaction.TxnType.REFUNDED]),
        "applied_to_dues": money(totals[DepositTransaction.TxnType.APPLIED_TO_DUES]),
        "deducted": money(totals[DepositTransaction.TxnType.DEDUCTION]),
        "balance": money(held),
    }


def period_dues(agreement, period, charges=None):
    """Monthly amount due (rent + included fixed charges) for one agreement and month."""
    if charges is None:
        charges = annotate_charges(
            Charge.objects.filter(agreement=agreement, period=period, included_in_monthly_due=True, is_void=False)
        )
    net_total = paid_total = ZERO
    ids = []
    due_date = None
    rent = ZERO
    for charge in charges:
        net, paid, _ = charge_figures(charge)
        net_total += net
        paid_total += min(paid, net)
        if net - paid > 0:
            ids.append(charge.id)
        if charge.charge_type == Charge.ChargeType.RENT:
            rent = net
        due_date = min(due_date, charge.due_date) if due_date else charge.due_date
    outstanding = money(net_total - paid_total)
    return {
        "rent": money(rent),
        "amount_due": money(net_total),
        "paid": money(paid_total),
        "balance": outstanding,
        "due_date": due_date,
        "status": charge_status(net_total, paid_total, outstanding, due_date) if due_date else "no_charges",
        "open_charge_ids": ids,
    }


# ---------------------------------------------------------------------------
# Agreements
# ---------------------------------------------------------------------------


def conflicting_agreements(unit, exclude_ids=()):
    """Active agreements that would overlap the given unit's space.

    Overlap = same unit, or another unit occupying the same floor section, or either
    unit occupying the entire floor that the other also uses.
    """
    unit_ids = {unit.id}
    for space in UnitSpace.objects.filter(unit=unit):
        same_floor = UnitSpace.objects.filter(floor_id=space.floor_id).exclude(unit=unit)
        if space.section_id is None:
            unit_ids.update(same_floor.values_list("unit_id", flat=True))
        else:
            unit_ids.update(
                same_floor.filter(Q(section_id=space.section_id) | Q(section__isnull=True)).values_list("unit_id", flat=True)
            )
    return (
        Agreement.objects.filter(Q(unit_id__in=unit_ids) | Q(additional_units__in=unit_ids), status=Agreement.Status.ACTIVE)
        .exclude(id__in=exclude_ids)
        .distinct()
    )


def ensure_no_overlap(unit, exclude_ids=()):
    conflicts = list(
        conflicting_agreements(unit, exclude_ids).select_related("tenant", "unit").prefetch_related("additional_units")
    )
    if conflicts:
        names = ", ".join(f"{a.tenant.full_name} ({a.units_label})" for a in conflicts)
        raise ServiceError(
            f"This space is already occupied under an active agreement: {names}. End that agreement first.",
            code="occupancy_overlap",
        )


def default_billing_start(start_date):
    return max(month_start(start_date), month_start(today()))


def create_agreement(*, owner, validated_data):
    with transaction.atomic():
        additional_units = list(validated_data.pop("additional_units", []))
        for unit in [validated_data["unit"], *additional_units]:
            ensure_no_overlap(unit)
        validated_data.setdefault("billing_start_date", default_billing_start(validated_data["start_date"]))
        validated_data["billing_start_date"] = month_start(validated_data["billing_start_date"])
        agreement = Agreement.objects.create(owner=owner, status=Agreement.Status.ACTIVE, **validated_data)
        agreement.additional_units.set(additional_units)
        tenant = agreement.tenant
        if tenant.status != Tenant.Status.ACTIVE:
            tenant.status = Tenant.Status.ACTIVE
            tenant.save(update_fields=["status", "updated_at"])
        return agreement


def revise_rent(*, agreement, new_rent, effective_date, reason="", document=None):
    new_rent = money(new_rent)
    if new_rent < 0:
        raise ServiceError("Rent cannot be negative.", code="invalid_amount")
    if agreement.status != Agreement.Status.ACTIVE:
        raise ServiceError("Only active agreements can be revised.", code="not_active")
    if effective_date < agreement.start_date:
        raise ServiceError("Effective date cannot be before the agreement start date.", code="invalid_date")
    period = month_start(effective_date)
    with transaction.atomic():
        Agreement.objects.select_for_update().get(pk=agreement.pk)
        if any(month_start(r.effective_date) == period for r in agreement.rent_revisions.all()):
            raise ServiceError("A rent revision already exists for that month.", code="duplicate_revision")
        previous = rent_for_period(agreement, period)
        if previous == new_rent:
            raise ServiceError("New rent is the same as the current rent.", code="no_change")
        revision = RentRevision.objects.create(
            owner=agreement.owner,
            agreement=agreement,
            previous_rent=previous,
            new_rent=new_rent,
            effective_date=effective_date,
            reason=reason or "",
            document=document,
        )
    return revision


def _end_open_meter_assignments(agreement, end_date):
    MeterAssignment.objects.filter(agreement=agreement, end_date__isnull=True).update(end_date=end_date)


def end_agreement(*, agreement, move_out_date, status=Agreement.Status.ENDED, notes=""):
    if status not in (Agreement.Status.ENDED, Agreement.Status.TERMINATED):
        raise ServiceError("Status must be 'ended' or 'terminated'.", code="invalid_status")
    with transaction.atomic():
        agreement = Agreement.objects.select_for_update().get(pk=agreement.pk)
        if agreement.status != Agreement.Status.ACTIVE:
            raise ServiceError("Agreement is not active.", code="not_active")
        if move_out_date < agreement.start_date:
            raise ServiceError("Move-out date cannot be before the start date.", code="invalid_date")
        agreement.status = status
        agreement.move_out_date = move_out_date
        agreement.billing_end_date = month_start(move_out_date)
        if notes:
            agreement.notes = f"{agreement.notes}\n{notes}".strip()
        agreement.save()
        _end_open_meter_assignments(agreement, move_out_date)

        # Unpaid periodic charges already generated for months after move-out are voided.
        later = Charge.objects.filter(agreement=agreement, period__gt=agreement.billing_end_date, is_void=False)
        voided, kept = 0, 0
        for charge in later:
            if charge.allocations.filter(payment__is_reversed=False).exists():
                kept += 1
            else:
                charge.is_void = True
                charge.void_reason = "Agreement ended before this period"
                charge.save(update_fields=["is_void", "void_reason", "updated_at"])
                voided += 1

        tenant = agreement.tenant
        if not tenant.agreements.filter(status=Agreement.Status.ACTIVE).exists():
            tenant.status = Tenant.Status.FORMER
            tenant.save(update_fields=["status", "updated_at"])
    return agreement, {"voided_future_charges": voided, "paid_future_charges_kept": kept}


def renew_agreement(
    *, agreement, start_date, end_date=None, monthly_rent=None, rent_due_day=None, security_deposit_amount=None, notes=""
):
    """Create a new agreement continuing the old one. The old agreement is kept untouched except its status."""
    with transaction.atomic():
        old = Agreement.objects.select_for_update().get(pk=agreement.pk)
        if old.status != Agreement.Status.ACTIVE:
            raise ServiceError("Only active agreements can be renewed.", code="not_active")
        if start_date <= old.start_date:
            raise ServiceError("Renewal must start after the current agreement started.", code="invalid_date")
        if end_date and end_date < start_date:
            raise ServiceError("End date must be after the start date.", code="invalid_date")

        billing_start = month_start(start_date)
        if Charge.objects.filter(agreement=old, charge_type=Charge.ChargeType.RENT, period__gte=billing_start).exists():
            last_billed = Charge.objects.filter(agreement=old, charge_type=Charge.ChargeType.RENT).latest("period").period
            billing_start = add_months(last_billed, 1)

        current_rent = rent_for_period(old, billing_start)
        new = Agreement.objects.create(
            owner=old.owner,
            property=old.property,
            unit=old.unit,
            tenant=old.tenant,
            monthly_rent=money(monthly_rent) if monthly_rent is not None else current_rent,
            rent_due_day=rent_due_day or old.rent_due_day,
            start_date=start_date,
            end_date=end_date,
            billing_start_date=billing_start,
            security_deposit_amount=(
                money(security_deposit_amount) if security_deposit_amount is not None else old.security_deposit_amount
            ),
            status=Agreement.Status.ACTIVE,
            renewed_from=old,
            reminders_enabled=old.reminders_enabled,
            notes=notes or "",
        )
        new.additional_units.set(old.additional_units.all())
        old.status = Agreement.Status.RENEWED
        old.billing_end_date = add_months(billing_start, -1)
        old.save(update_fields=["status", "billing_end_date", "updated_at"])

        # Carry agreement-level recurring charges and meter links over to the new agreement.
        for rc in RecurringCharge.objects.filter(agreement=old, is_enabled=True):
            if rc.end_date and rc.end_date < start_date:
                continue
            RecurringCharge.objects.create(
                owner=rc.owner,
                name=rc.name,
                amount=rc.amount,
                frequency=rc.frequency,
                start_date=billing_start,
                end_date=rc.end_date,
                agreement=new,
                included_in_monthly_due=rc.included_in_monthly_due,
                notes=rc.notes,
            )
        for assignment in MeterAssignment.objects.filter(agreement=old, end_date__isnull=True):
            assignment.end_date = start_date - timedelta(days=1)
            assignment.save(update_fields=["end_date", "updated_at"])
            MeterAssignment.objects.create(
                owner=assignment.owner,
                meter=assignment.meter,
                agreement=new,
                start_date=start_date,
                share_percent=assignment.share_percent,
            )

        held = deposit_balance([old.id])["balance"]
        if held > 0:
            DepositTransaction.objects.create(
                owner=old.owner, agreement=old, txn_type=DepositTransaction.TxnType.TRANSFERRED_OUT,
                amount=held, txn_date=start_date, notes=f"Moved to renewed agreement #{new.id}",
            )
            DepositTransaction.objects.create(
                owner=old.owner, agreement=new, txn_type=DepositTransaction.TxnType.TRANSFERRED_IN,
                amount=held, txn_date=start_date, notes=f"Carried over from agreement #{old.id}",
            )
    return new


def expiry_state(agreement, on_date=None, window_days=DEFAULT_EXPIRING_DAYS):
    on_date = on_date or today()
    if agreement.status == Agreement.Status.RENEWED:
        return "renewed"
    if agreement.status != Agreement.Status.ACTIVE:
        return agreement.status
    if not agreement.end_date:
        return "active"
    if agreement.end_date < on_date:
        return "expired"
    if agreement.end_date <= on_date + timedelta(days=window_days):
        return "expiring_soon"
    return "active"


def reminder_window(owner):
    settings_obj = ReminderSettings.objects.filter(owner=owner).first()
    return settings_obj.days_before if settings_obj else DEFAULT_EXPIRING_DAYS


def generate_reminders(owner, on_date=None):
    """Create in-app renewal reminders that are due. Unique constraint prevents duplicates."""
    on_date = on_date or today()
    settings_obj = ReminderSettings.objects.filter(owner=owner, enabled=True).first()
    if not settings_obj:
        return 0
    days = settings_obj.days_before
    agreements = Agreement.objects.filter(
        owner=owner,
        status=Agreement.Status.ACTIVE,
        reminders_enabled=True,
        end_date__isnull=False,
        end_date__lte=on_date + timedelta(days=days),
    )
    rows = [
        AgreementReminder(
            owner=owner,
            agreement=a,
            expiry_date=a.end_date,
            days_before=days,
            remind_on=a.end_date - timedelta(days=days),
        )
        for a in agreements
    ]
    created = AgreementReminder.objects.bulk_create(rows, ignore_conflicts=True)
    return len(created)


# ---------------------------------------------------------------------------
# Recurring charges & expenses
# ---------------------------------------------------------------------------


def revise_recurring_charge(*, charge, new_amount, effective_date, notes=""):
    """End the current definition the month before `effective_date` and start a new one."""
    new_amount = money(new_amount)
    if new_amount <= 0:
        raise ServiceError("Amount must be greater than zero.", code="invalid_amount")
    effective = month_start(effective_date)
    if effective <= month_start(charge.start_date):
        raise ServiceError("Effective month must be after the charge's start month.", code="invalid_date")
    with transaction.atomic():
        charge = RecurringCharge.objects.select_for_update().get(pk=charge.pk)
        if hasattr(charge, "revised_to"):
            raise ServiceError("This charge was already revised; revise the newest version.", code="already_revised")
        old_end = charge.end_date
        charge.end_date = effective - timedelta(days=1)
        charge.save(update_fields=["end_date", "updated_at"])
        return RecurringCharge.objects.create(
            owner=charge.owner,
            name=charge.name,
            amount=new_amount,
            frequency=charge.frequency,
            start_date=effective,
            end_date=old_end if old_end and old_end >= effective else None,
            property=charge.property,
            unit=charge.unit,
            agreement=charge.agreement,
            is_enabled=charge.is_enabled,
            included_in_monthly_due=charge.included_in_monthly_due,
            revised_from=charge,
            notes=notes or charge.notes,
        )


def sync_expense_charge(expense):
    """Create or update the tenant charge for a recoverable expense.

    The owner's expense (expense.amount) and the tenant receivable (the charge) are
    separate figures, so reports never count the same money twice.
    """
    charge = Charge.objects.filter(expense=expense).first()
    wants_charge = expense.is_recoverable and expense.recoverable_amount > 0
    if wants_charge and not expense.agreement_id:
        raise ServiceError("Choose the tenant agreement to recover this expense from.", code="agreement_required")

    if charge is None:
        if wants_charge:
            agreement = expense.agreement
            Charge.objects.create(
                owner=expense.owner,
                agreement=agreement,
                tenant_id=agreement.tenant_id,
                property_id=agreement.property_id,
                charge_type=Charge.ChargeType.EXPENSE,
                period=month_start(expense.expense_date),
                description=f"Expense: {expense.title}",
                amount=expense.recoverable_amount,
                due_date=max(expense.expense_date, today()),
                expense=expense,
            )
        return

    net, paid, _ = charge_figures(charge)
    if charge.agreement_id != expense.agreement_id and paid > 0:
        raise ServiceError("Recovery already has payments; the tenant cannot be changed.", code="has_payments")
    target = expense.recoverable_amount if wants_charge else ZERO
    if target < paid:
        raise ServiceError(
            f"₹{paid} has already been recovered; the recoverable amount cannot go below that.", code="below_paid"
        )
    if paid == 0 and (charge.agreement_id != expense.agreement_id or target == 0):
        charge.is_void = True
        charge.void_reason = "Expense recovery changed"
        charge.save(update_fields=["is_void", "void_reason", "updated_at"])
        charge.expense = None
        charge.save(update_fields=["expense"])
        if wants_charge:
            sync_expense_charge(expense)
        return
    if charge.is_void and wants_charge:
        charge.is_void = False
        charge.void_reason = ""
        charge.save(update_fields=["is_void", "void_reason", "updated_at"])
        net, paid, _ = charge_figures(charge)
    delta = money(target - net)
    if delta:
        ChargeAdjustment.objects.create(
            owner=expense.owner, charge=charge, amount=delta, reason="Recoverable amount changed on expense"
        )


def expense_recovery(expense):
    if not expense.is_recoverable or expense.recoverable_amount <= 0:
        return {"status": "not_recoverable", "recovered": ZERO, "pending": ZERO}
    charge = getattr(expense, "charge", None)
    if charge is None:
        return {"status": "pending", "recovered": ZERO, "pending": money(expense.recoverable_amount)}
    net, paid, outstanding = charge_figures(charge)
    if outstanding <= 0:
        status = "recovered"
    elif paid > 0:
        status = "partially_recovered"
    else:
        status = "pending"
    return {"status": status, "recovered": money(min(paid, net)), "pending": outstanding}


# ---------------------------------------------------------------------------
# Electricity
# ---------------------------------------------------------------------------


def previous_reading_for(meter, reading_date):
    last = meter.readings.filter(reading_date__lt=reading_date).order_by("-reading_date").first()
    return last.current_reading if last else None


def units_from_readings(bill, agreements):
    """Units per agreement from saved readings in the bill month.

    A meter linked to one agreement gives all its units to that agreement. A shared
    meter needs a share_percent on every assignment; otherwise the agreement is
    reported in `needs_manual` and the user has to enter units.
    """
    start, end = bill.period, month_end(bill.period)
    units = {a.id: ZERO for a in agreements}
    detail = defaultdict(list)
    needs_manual = set()
    assignments = MeterAssignment.objects.filter(
        meter__property_id=bill.property_id, start_date__lte=end
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=start)).select_related("meter")
    if bill.meter_id:
        assignments = assignments.filter(meter_id=bill.meter_id)
    by_meter = defaultdict(list)
    for assignment in assignments:
        by_meter[assignment.meter_id].append(assignment)

    for meter_id, rows in by_meter.items():
        relevant = [r for r in rows if r.agreement_id in units]
        if not relevant:
            continue
        consumed = MeterReading.objects.filter(meter_id=meter_id, reading_date__range=(start, end)).aggregate(
            t=Sum("units_consumed")
        )["t"] or ZERO
        shared = len({r.agreement_id for r in rows}) > 1
        for row in relevant:
            if shared:
                if row.share_percent is None:
                    needs_manual.add(row.agreement_id)
                    continue
                portion = (consumed * row.share_percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)
            else:
                portion = consumed
            units[row.agreement_id] += portion
            detail[row.agreement_id].append({"meter": row.meter.name, "meter_units": str(consumed), "units": str(portion)})
    return units, detail, needs_manual


def calculate_allocation(bill, method, agreements, lines=None, use_readings=False):
    """Preview an allocation. Returns {'lines': [...], 'warnings': [...], 'difference': Decimal}.

    `lines` maps agreement id -> {'units': ..} (meter) or {'amount': ..} (manual).
    """
    lines = {int(k): v for k, v in (lines or {}).items()}
    agreements = list(agreements)
    if not agreements:
        raise ServiceError("Select at least one agreement.", code="no_agreements")
    if len({a.id for a in agreements}) != len(agreements):
        raise ServiceError("An agreement was selected twice.", code="duplicate_agreement")
    amount = money(bill.amount)
    warnings = []
    result = []
    difference = ZERO

    if method == ElectricityBill.Method.EQUAL:
        shares = distribute(amount, [1] * len(agreements))
        result = [{"agreement": a, "units": None, "amount": s} for a, s in zip(agreements, shares)]
        if len(set(shares)) > 1:
            warnings.append({"code": "rounding", "message": "Rounding cents were added to the first line(s) so the total matches."})

    elif method == ElectricityBill.Method.METER:
        units = {}
        detail = {}
        if use_readings:
            units, detail, needs_manual = units_from_readings(bill, agreements)
            needs_manual -= {aid for aid in needs_manual if aid in lines and lines[aid].get("units") not in (None, "")}
            if needs_manual:
                raise ServiceError(
                    "Some agreements share a meter without a share percentage. Enter their units manually.",
                    code="units_required",
                    extra={"agreement_ids": sorted(needs_manual)},
                )
        for a in agreements:
            entered = lines.get(a.id, {}).get("units")
            if entered not in (None, ""):
                units[a.id] = Decimal(str(entered))
            if a.id not in units:
                raise ServiceError(f"Units missing for {a.tenant.full_name}.", code="units_required")
            if units[a.id] < 0:
                raise ServiceError("Units cannot be negative.", code="invalid_units")
        total_units = sum(units[a.id] for a in agreements)
        if total_units <= 0:
            raise ServiceError(
                "Total units are zero, so the bill cannot be split by usage. Use equal split or manual allocation.",
                code="zero_units",
            )
        if bill.total_units is not None and money(total_units) != money(bill.total_units):
            warnings.append(
                {
                    "code": "units_mismatch",
                    "message": f"Allocated units ({money(total_units)}) differ from the bill's total units ({money(bill.total_units)}). "
                    "The full bill amount is still split by the entered units.",
                    "requires_confirmation": True,
                }
            )
        shares = distribute(amount, [units[a.id] for a in agreements])
        rate = (amount / total_units).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        result = [
            {"agreement": a, "units": money(units[a.id]), "amount": s, "readings": detail.get(a.id, [])}
            for a, s in zip(agreements, shares)
        ]
        warnings.append({"code": "rate", "message": f"Effective rate ₹{rate} per unit over {money(total_units)} units."})

    elif method == ElectricityBill.Method.MANUAL:
        for a in agreements:
            entered = lines.get(a.id, {}).get("amount")
            if entered in (None, ""):
                raise ServiceError(f"Amount missing for {a.tenant.full_name}.", code="amount_required")
            value = money(entered)
            if value < 0:
                raise ServiceError("Amounts cannot be negative.", code="invalid_amount")
            result.append({"agreement": a, "units": None, "amount": value})
        allocated = money(sum(r["amount"] for r in result))
        difference = money(amount - allocated)
        if difference < 0:
            raise ServiceError(
                f"Allocations exceed the bill by ₹{-difference}. Reduce the amounts.",
                code="over_allocated",
                extra={"difference": str(difference)},
            )
        if difference > 0:
            warnings.append(
                {
                    "code": "under_allocated",
                    "message": f"₹{difference} of the bill is not allocated. Confirm to record it as an owner-paid amount, or correct the amounts.",
                    "requires_confirmation": True,
                }
            )
    else:
        raise ServiceError("Unknown allocation method.", code="invalid_method")

    return {"lines": result, "warnings": warnings, "difference": difference}


def allocate_bill(*, bill, method, agreements, lines=None, use_readings=False, confirm=False):
    with transaction.atomic():
        bill = ElectricityBill.objects.select_for_update().get(pk=bill.pk)
        if bill.status != ElectricityBill.Status.DRAFT:
            raise ServiceError("This bill is already allocated. Use an allocation correction instead.", code="already_allocated")
        for a in agreements:
            if a.property_id != bill.property_id:
                raise ServiceError(f"{a} does not belong to this bill's property.", code="wrong_property")
        preview = calculate_allocation(bill, method, agreements, lines, use_readings)
        if any(w.get("requires_confirmation") for w in preview["warnings"]) and not confirm:
            raise ServiceError(
                "The allocation needs your confirmation.", code="confirmation_required", extra={"preview": preview}
            )
        _record_allocations(bill, method, preview["lines"], preview["difference"])
    return bill, preview


def _record_allocations(bill, method, lines, unallocated):
    """Charge each tenant their share. Electricity is part of the month's amount due, shown with the rent."""
    due = bill.due_date or bill.bill_date
    for line in lines:
        if line["amount"] <= 0:
            continue
        agreement = line["agreement"]
        allocation = ElectricityAllocation.objects.create(
            owner=bill.owner, bill=bill, agreement=agreement, units=line["units"], amount=line["amount"]
        )
        Charge.objects.create(
            owner=bill.owner,
            agreement=agreement,
            tenant_id=agreement.tenant_id,
            property_id=bill.property_id,
            charge_type=Charge.ChargeType.ELECTRICITY,
            period=bill.period,
            description=f"Electricity {month_label(bill.period)}" + (f" ({bill.meter.name})" if bill.meter_id else ""),
            amount=line["amount"],
            due_date=due,
            included_in_monthly_due=True,
            electricity_allocation=allocation,
        )
    bill.allocation_method = method
    bill.status = ElectricityBill.Status.ALLOCATED
    bill.unallocated_amount = unallocated
    bill.save(update_fields=["allocation_method", "status", "unallocated_amount", "updated_at"])


def agreement_for_unit(unit, period):
    """The agreement that has this unit (as main or additional unit) during the given month, if any."""
    start, end = period, month_end(period)
    return (
        Agreement.objects.filter(Q(unit=unit) | Q(additional_units=unit), start_date__lte=end)
        .filter(
            Q(status=Agreement.Status.ACTIVE, billing_end_date__isnull=True)
            | Q(billing_end_date__gte=start)
            | Q(move_out_date__gte=start)
        )
        .select_related("tenant", "unit")
        .prefetch_related("additional_units")
        .order_by("-start_date", "-id")
        .distinct()
        .first()
    )


def _non_negative(value, label):
    try:
        result = Decimal(str(value))
    except Exception:
        raise ServiceError(f"Enter a valid number for {label}.", code="invalid_number")
    if not result.is_finite() or result < 0:
        raise ServiceError(f"{label.capitalize()} cannot be negative.", code="invalid_number")
    return result


def meter_bill_split(meter, period, amount, method, unit_lines=None):
    """Split one month's bill for a meter between the units it supplies.

    method: "equal" (occupied units share equally), "meter" (by units used per unit; a vacant unit's
    share is paid by the owner) or "manual" (amount per occupied unit; anything left is paid by the owner).
    unit_lines maps unit id -> {"units": ..} or {"amount": ..}.
    """
    amount = money(amount)
    if amount <= 0:
        raise ServiceError("Bill amount must be more than zero.", code="invalid_amount")
    unit_lines = {int(k): (v or {}) for k, v in (unit_lines or {}).items()}
    units = sorted(meter.units.all(), key=lambda u: u.name)
    if not units:
        raise ServiceError("Link this meter to the units it supplies first.", code="no_units")
    rows = [{"unit": u, "agreement": agreement_for_unit(u, period), "amount": ZERO} for u in units]
    occupied = [r for r in rows if r["agreement"]]
    if not occupied:
        raise ServiceError("None of this meter's units has a tenant in this month.", code="no_tenants")

    if method == ElectricityBill.Method.EQUAL:
        for r, share in zip(occupied, distribute(amount, [1] * len(occupied))):
            r["amount"] = share
    elif method == ElectricityBill.Method.METER:
        for r in rows:
            entered = unit_lines.get(r["unit"].id, {}).get("units")
            if entered in (None, ""):
                raise ServiceError(f"Enter units used for {r['unit'].name} (0 if none).", code="units_required")
            r["units_used"] = _non_negative(entered, f"units for {r['unit'].name}")
        weights = [r["units_used"] for r in rows]
        if sum(weights) <= 0:
            raise ServiceError("Total units used is zero. Use equal split or manual amounts.", code="zero_units")
        for r, share in zip(rows, distribute(amount, weights)):
            r["amount"] = share
    elif method == ElectricityBill.Method.MANUAL:
        for r in occupied:
            entered = unit_lines.get(r["unit"].id, {}).get("amount")
            if entered in (None, ""):
                raise ServiceError(f"Enter an amount for {r['unit'].name}.", code="amount_required")
            r["amount"] = money(_non_negative(entered, f"amount for {r['unit'].name}"))
        allocated = sum((r["amount"] for r in occupied), ZERO)
        if allocated > amount:
            raise ServiceError(f"Amounts add up to ₹{money(allocated)}, more than the bill (₹{amount}).", code="over_allocated")
    else:
        raise ServiceError("Unknown split method.", code="invalid_method")

    by_agreement = {}
    for r in occupied:
        line = by_agreement.setdefault(
            r["agreement"].id, {"agreement": r["agreement"], "units": None, "amount": ZERO, "unit_names": []}
        )
        line["amount"] += r["amount"]
        line["unit_names"].append(r["unit"].name)
        if "units_used" in r:
            line["units"] = (line["units"] or ZERO) + r["units_used"]
    lines = list(by_agreement.values())
    owner_pays = money(amount - sum((l["amount"] for l in lines), ZERO))
    return {"units": rows, "lines": lines, "total": amount, "owner_pays": owner_pays}


def add_meter_bill(*, meter, period, amount, method, unit_lines=None, bill_date=None, due_date=None,
                   total_units=None, notes="", attachment=None):
    """Record a meter's monthly bill and add each tenant's share to their dues in one step."""
    period = month_start(period)
    with transaction.atomic():
        meter = Meter.objects.select_for_update().get(pk=meter.pk)
        duplicate = ElectricityBill.objects.filter(meter=meter, period=period).exclude(status=ElectricityBill.Status.CANCELLED)
        if duplicate.exists():
            raise ServiceError(
                f"{meter.name} already has a bill for {month_label(period)}. Open it to correct amounts, or cancel it first.",
                code="duplicate_bill",
            )
        split = meter_bill_split(meter, period, amount, method, unit_lines)
        bill = ElectricityBill.objects.create(
            owner=meter.owner,
            property_id=meter.property_id,
            meter=meter,
            period=period,
            bill_date=bill_date or today(),
            due_date=due_date,
            amount=split["total"],
            total_units=total_units,
            notes=notes,
            attachment=attachment,
        )
        _record_allocations(
            bill,
            method,
            [
                {"agreement": l["agreement"], "units": money(l["units"]) if l["units"] is not None else None, "amount": money(l["amount"])}
                for l in split["lines"]
            ],
            split["owner_pays"],
        )
    return bill, split


def correct_allocation(*, allocation, new_amount, reason):
    """Change what a tenant owes for a bill via an adjustment; the original allocation row is preserved."""
    new_amount = money(new_amount)
    if new_amount < 0:
        raise ServiceError("Amount cannot be negative.", code="invalid_amount")
    charge = Charge.objects.get(electricity_allocation=allocation)
    net, _, _ = charge_figures(charge)
    delta = money(new_amount - net)
    if delta == 0:
        raise ServiceError("Amount is unchanged.", code="no_change")
    return adjust_charge(charge, delta, f"Electricity correction: {reason}", allocation.owner)


def bill_allocated_total(bill):
    total = ZERO
    for charge in annotate_charges(Charge.objects.filter(electricity_allocation__bill=bill)):
        total += charge_figures(charge)[0]
    return money(total)


# ---------------------------------------------------------------------------
# Security deposit
# ---------------------------------------------------------------------------


def settle_deposit(*, owner, user, agreement, settle_date, apply_to_dues=ZERO, deduction=ZERO, deduction_reason="", refund=ZERO, refund_method=Payment.Method.CASH):
    """Settle a deposit: use part for outstanding dues, keep part for damages, refund the rest."""
    apply_to_dues, deduction, refund = money(apply_to_dues), money(deduction), money(refund)
    if min(apply_to_dues, deduction, refund) < 0:
        raise ServiceError("Amounts cannot be negative.", code="invalid_amount")
    if apply_to_dues + deduction + refund <= 0:
        raise ServiceError("Enter at least one amount.", code="invalid_amount")
    if deduction > 0 and not deduction_reason.strip():
        raise ServiceError("A reason is required for a deduction.", code="reason_required")
    with transaction.atomic():
        Agreement.objects.select_for_update().get(pk=agreement.pk)
        held = deposit_balance([agreement.id])["balance"]
        if apply_to_dues + deduction + refund > held:
            raise ServiceError(f"Only ₹{held} of deposit is held.", code="insufficient_deposit")
        txns = []
        if apply_to_dues > 0:
            outstanding = sum((o for _, o in outstanding_charges([agreement.id])), ZERO)
            if apply_to_dues > outstanding:
                raise ServiceError(f"Outstanding dues are only ₹{money(outstanding)}.", code="exceeds_dues")
            result = record_payment(
                owner=owner, user=user, agreement=agreement, amount=apply_to_dues, payment_date=settle_date,
                method=Payment.Method.DEPOSIT, notes="Adjusted from security deposit",
            )
            txns.append(DepositTransaction.objects.create(
                owner=owner, agreement=agreement, txn_type=DepositTransaction.TxnType.APPLIED_TO_DUES,
                amount=apply_to_dues, txn_date=settle_date, payment=result.payment, notes="Applied to outstanding dues",
            ))
        if deduction > 0:
            txns.append(DepositTransaction.objects.create(
                owner=owner, agreement=agreement, txn_type=DepositTransaction.TxnType.DEDUCTION,
                amount=deduction, txn_date=settle_date, notes=deduction_reason.strip(),
            ))
        if refund > 0:
            txns.append(DepositTransaction.objects.create(
                owner=owner, agreement=agreement, txn_type=DepositTransaction.TxnType.REFUNDED,
                amount=refund, txn_date=settle_date, method=refund_method, notes="Refunded to tenant",
            ))
    return txns
