"""Read-only aggregations for the dashboard, rent collection screen and monthly report."""

from collections import defaultdict
from datetime import timedelta

from django.db.models import Q, Sum

from .models import ZERO, Agreement, Charge, ElectricityBill, Expense, Payment, Property, RentalUnit
from .services import (
    annotate_charges,
    annotate_payments,
    charge_figures,
    charge_status,
    expiry_state,
    generate_charges,
    generate_reminders,
    month_end,
    money,
    payment_unapplied,
    reminder_window,
    today,
)

STATUS_ORDER = {"overdue": 0, "unpaid": 1, "partially_paid": 2, "paid": 3, "no_charges": 4}


def _scoped(queryset, property_id, field="property_id"):
    return queryset.filter(**{field: property_id}) if property_id else queryset


def outstanding_by_agreement(owner, property_id=None):
    """{agreement_id: {"outstanding": .., "overdue": .., "credit": ..}} over all periods."""
    data = defaultdict(lambda: {"outstanding": ZERO, "overdue": ZERO, "credit": ZERO})
    on = today()
    charges = annotate_charges(_scoped(Charge.objects.filter(owner=owner, is_void=False), property_id))
    for charge in charges:
        _, _, outstanding = charge_figures(charge)
        if outstanding > 0:
            data[charge.agreement_id]["outstanding"] += outstanding
            if charge.due_date < on:
                data[charge.agreement_id]["overdue"] += outstanding
    payments = annotate_payments(
        _scoped(Payment.objects.filter(owner=owner, is_reversed=False), property_id, "agreement__property_id")
    )
    for payment in payments:
        unapplied = payment_unapplied(payment)
        if unapplied > 0:
            data[payment.agreement_id]["credit"] += unapplied
    return data


def rent_collection(owner, period, property_id=None, search=""):
    """One row per agreement billed in `period`: what is due this month, paid, balance, status."""
    generate_charges(owner, period)
    period_charges = annotate_charges(
        _scoped(
            Charge.objects.filter(owner=owner, period=period, included_in_monthly_due=True, is_void=False),
            property_id,
        )
    )
    per_agreement = defaultdict(list)
    for charge in period_charges:
        per_agreement[charge.agreement_id].append(charge)

    agreements = _scoped(
        Agreement.objects.filter(owner=owner).filter(Q(id__in=per_agreement.keys()) | Q(status=Agreement.Status.ACTIVE)),
        property_id,
    ).select_related("tenant", "unit", "property").prefetch_related("additional_units")
    if search:
        agreements = agreements.filter(
            Q(tenant__full_name__icontains=search) | Q(unit__name__icontains=search) | Q(tenant__mobile__icontains=search)
        )
    balances = outstanding_by_agreement(owner, property_id)

    rows = []
    totals = defaultdict(lambda: ZERO)
    counts = defaultdict(int)
    for agreement in agreements:
        charges = per_agreement.get(agreement.id, [])
        if not charges and agreement.status != Agreement.Status.ACTIVE:
            continue
        net_total = paid_total = rent = ZERO
        open_ids, lines, due_date = [], [], None
        for charge in sorted(charges, key=lambda c: (c.charge_type != Charge.ChargeType.RENT, c.id)):
            net, paid, outstanding = charge_figures(charge)
            net_total += net
            paid_total += min(paid, net)
            if charge.charge_type == Charge.ChargeType.RENT:
                rent = net
            if outstanding > 0:
                open_ids.append(charge.id)
            due_date = min(due_date, charge.due_date) if due_date else charge.due_date
            lines.append(
                {"id": charge.id, "type": charge.charge_type, "description": charge.description,
                 "amount": net, "paid": paid, "balance": outstanding}
            )
        balance = money(net_total - paid_total)
        status = charge_status(net_total, paid_total, balance, due_date) if charges else "no_charges"
        counts[status] += 1
        totals["expected"] += net_total
        totals["collected"] += paid_total
        totals["remaining"] += balance
        totals["rent_expected"] += rent
        b = balances.get(agreement.id, {"outstanding": ZERO, "overdue": ZERO, "credit": ZERO})
        rows.append(
            {
                "agreement_id": agreement.id,
                "tenant_id": agreement.tenant_id,
                "tenant_name": agreement.tenant.full_name,
                "tenant_mobile": agreement.tenant.mobile,
                "property_id": agreement.property_id,
                "property_name": agreement.property.name,
                "unit_name": agreement.units_label,
                "monthly_rent": rent,
                "amount_due": money(net_total),
                "paid": money(paid_total),
                "balance": balance,
                "status": status,
                "due_date": due_date,
                "open_charge_ids": open_ids,
                "lines": lines,
                "total_outstanding": money(b["outstanding"]),
                "overdue_outstanding": money(b["overdue"]),
                "advance_credit": money(b["credit"]),
            }
        )
    rows.sort(key=lambda r: (STATUS_ORDER.get(r["status"], 9), r["tenant_name"].lower()))
    return {
        "month": period.strftime("%Y-%m"),
        "summary": {
            "expected": money(totals["expected"]),
            "rent_expected": money(totals["rent_expected"]),
            "collected": money(totals["collected"]),
            "remaining": money(totals["remaining"]),
            "paid": counts["paid"],
            "partially_paid": counts["partially_paid"],
            "unpaid": counts["unpaid"],
            "overdue": counts["overdue"],
        },
        "rows": rows,
    }


def _period_charge_totals(owner, period, property_id):
    totals = defaultdict(lambda: {"amount": ZERO, "paid": ZERO, "outstanding": ZERO})
    charges = annotate_charges(_scoped(Charge.objects.filter(owner=owner, period=period, is_void=False), property_id))
    for charge in charges:
        net, paid, outstanding = charge_figures(charge)
        bucket = totals[charge.charge_type]
        bucket["amount"] += net
        bucket["paid"] += min(paid, net)
        bucket["outstanding"] += outstanding
    return {k: {kk: money(vv) for kk, vv in v.items()} for k, v in totals.items()}


def _all_outstanding_by_type(owner, property_id):
    totals = defaultdict(lambda: ZERO)
    for charge in annotate_charges(_scoped(Charge.objects.filter(owner=owner, is_void=False), property_id)):
        _, _, outstanding = charge_figures(charge)
        if outstanding > 0:
            totals[charge.charge_type] += outstanding
    result = {t: money(totals[t]) for t in Charge.ChargeType.values}
    result["total"] = money(sum(totals.values(), ZERO))
    return result


def agreements_needing_attention(owner, property_id=None):
    window = reminder_window(owner)
    on = today()
    qs = _scoped(
        Agreement.objects.filter(
            owner=owner, status=Agreement.Status.ACTIVE, end_date__isnull=False, end_date__lte=on + timedelta(days=window)
        ),
        property_id,
    ).select_related("tenant", "unit", "property").prefetch_related("additional_units").order_by("end_date")
    return [
        {
            "agreement_id": a.id,
            "tenant_name": a.tenant.full_name,
            "unit_name": a.units_label,
            "property_name": a.property.name,
            "end_date": a.end_date,
            "days_left": (a.end_date - on).days,
            "state": expiry_state(a, on, window),
        }
        for a in qs
    ]


def dashboard(owner, period, property_id=None):
    generate_charges(owner, period)
    generate_reminders(owner)
    properties = Property.objects.filter(owner=owner, status=Property.Status.ACTIVE)
    if property_id:
        properties = properties.filter(id=property_id)
    units = _scoped(RentalUnit.objects.filter(owner=owner).exclude(status=RentalUnit.Status.INACTIVE), property_id)
    active = _scoped(Agreement.objects.filter(owner=owner, status=Agreement.Status.ACTIVE), property_id)
    occupied_ids = set(active.values_list("unit_id", flat=True)) | set(
        active.filter(additional_units__isnull=False).values_list("additional_units", flat=True)
    )
    unit_count = units.count()
    occupied = units.filter(id__in=occupied_ids).count()

    period_totals = _period_charge_totals(owner, period, property_id)
    rent = period_totals.get("rent", {"amount": ZERO, "paid": ZERO, "outstanding": ZERO})
    fixed = period_totals.get("fixed", {"amount": ZERO, "paid": ZERO, "outstanding": ZERO})
    outstanding = _all_outstanding_by_type(owner, property_id)

    bills = _scoped(ElectricityBill.objects.filter(owner=owner, period=period), property_id).exclude(
        status=ElectricityBill.Status.CANCELLED
    )
    expenses = _scoped(
        Expense.objects.filter(owner=owner, expense_date__range=(period, month_end(period))), property_id
    )
    received = _scoped(
        Payment.objects.filter(owner=owner, is_reversed=False, payment_date__range=(period, month_end(period))),
        property_id,
        "agreement__property_id",
    ).exclude(method=Payment.Method.DEPOSIT)
    collection = rent_collection(owner, period, property_id)

    return {
        "month": period.strftime("%Y-%m"),
        "properties": properties.count(),
        "units": unit_count,
        "occupied_units": occupied,
        "vacant_units": unit_count - occupied,
        "expected_rent": rent["amount"],
        "rent_collected": rent["paid"],
        "rent_remaining": rent["outstanding"],
        "monthly_fixed_charges": fixed["amount"],
        "fixed_charges_outstanding": fixed["outstanding"],
        "electricity_billed": money(bills.aggregate(t=Sum("amount"))["t"]),
        "electricity_bills_pending_allocation": bills.filter(status=ElectricityBill.Status.DRAFT).count(),
        "property_expenses": money(expenses.aggregate(t=Sum("amount"))["t"]),
        "recoverable_expenses": money(expenses.filter(is_recoverable=True).aggregate(t=Sum("recoverable_amount"))["t"]),
        "recoverable_expenses_outstanding": outstanding["expense"],
        "payments_received": money(received.aggregate(t=Sum("amount"))["t"]),
        "outstanding": outstanding,
        "tenant_status_counts": {k: collection["summary"][k] for k in ("paid", "partially_paid", "unpaid", "overdue")},
        "attention": agreements_needing_attention(owner, property_id),
    }


def monthly_report(owner, period, property_id=None):
    collection = rent_collection(owner, period, property_id)
    period_totals = _period_charge_totals(owner, period, property_id)
    empty = {"amount": ZERO, "paid": ZERO, "outstanding": ZERO}

    bills = _scoped(ElectricityBill.objects.filter(owner=owner, period=period), property_id).exclude(
        status=ElectricityBill.Status.CANCELLED
    ).select_related("property")
    expenses = list(
        _scoped(Expense.objects.filter(owner=owner, expense_date__range=(period, month_end(period))), property_id)
        .select_related("property", "agreement__tenant")
    )
    by_category = defaultdict(lambda: ZERO)
    for e in expenses:
        by_category[e.get_category_display()] += e.amount
    recovery_charges = annotate_charges(
        Charge.objects.filter(expense__in=[e.id for e in expenses], is_void=False)
    )
    recovered = pending = ZERO
    for charge in recovery_charges:
        net, paid, outstanding = charge_figures(charge)
        recovered += min(paid, net)
        pending += outstanding

    payments = _scoped(
        Payment.objects.filter(owner=owner, is_reversed=False, payment_date__range=(period, month_end(period))),
        property_id,
        "agreement__property_id",
    )
    by_method = defaultdict(lambda: ZERO)
    for p in payments:
        by_method[p.get_method_display()] += p.amount

    def group(status):
        return [
            {k: r[k] for k in ("agreement_id", "tenant_name", "unit_name", "amount_due", "paid", "balance", "due_date")}
            for r in collection["rows"]
            if r["status"] == status
        ]

    return {
        "month": period.strftime("%Y-%m"),
        "rent": {
            **{k: period_totals.get("rent", empty)[k] for k in ("amount", "paid", "outstanding")},
            "paid_tenants": group("paid"),
            "partially_paid_tenants": group("partially_paid"),
            "unpaid_tenants": group("unpaid"),
            "overdue_tenants": group("overdue"),
        },
        "monthly_dues": collection["summary"],
        "fixed_charges": period_totals.get("fixed", empty),
        "electricity": {
            **period_totals.get("electricity", empty),
            "billed": money(sum((b.amount for b in bills), ZERO)),
            "owner_absorbed": money(sum((b.unallocated_amount for b in bills), ZERO)),
            "bills": [
                {"id": b.id, "property_name": b.property.name, "amount": b.amount, "status": b.status,
                 "method": b.allocation_method, "unallocated_amount": b.unallocated_amount}
                for b in bills
            ],
        },
        "one_time_charges": period_totals.get("one_time", empty),
        "expenses": {
            "total": money(sum((e.amount for e in expenses), ZERO)),
            "by_category": {k: money(v) for k, v in by_category.items()},
            "recoverable": money(sum((e.recoverable_amount for e in expenses if e.is_recoverable), ZERO)),
            "recovered": money(recovered),
            "recovery_pending": money(pending),
            "items": [
                {"id": e.id, "title": e.title, "amount": e.amount, "date": e.expense_date,
                 "property_name": e.property.name, "recoverable_amount": e.recoverable_amount if e.is_recoverable else ZERO,
                 "tenant_name": e.agreement.tenant.full_name if e.agreement_id else None}
                for e in expenses
            ],
        },
        "payments_received": {
            "total": money(sum(by_method.values(), ZERO)),
            "by_method": {k: money(v) for k, v in by_method.items()},
        },
        "outstanding_all_periods": _all_outstanding_by_type(owner, property_id),
    }
