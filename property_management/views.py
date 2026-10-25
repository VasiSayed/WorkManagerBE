from datetime import timedelta

import mimetypes

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, ProtectedError, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import decorators, mixins, permissions, response, status, viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.views import APIView

from . import reports, services
from .models import (
    Agreement,
    AgreementReminder,
    Charge,
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
    Property,
    RecurringCharge,
    ReminderSettings,
    RentalUnit,
    RentRevision,
    Tenant,
)
from .serializers import (
    AdjustmentInputSerializer,
    AgreementReminderSerializer,
    AgreementSerializer,
    AllocateBillSerializer,
    ChargeAdjustmentSerializer,
    ChargeSerializer,
    CorrectAllocationSerializer,
    DepositTransactionSerializer,
    DocumentSerializer,
    ElectricityAllocationSerializer,
    ElectricityBillSerializer,
    EndAgreementSerializer,
    ExpenseSerializer,
    FloorSectionSerializer,
    FloorSerializer,
    MeterAssignmentSerializer,
    MeterReadingSerializer,
    MeterSerializer,
    PaymentSerializer,
    PropertySerializer,
    ReasonSerializer,
    RecordPaymentSerializer,
    RecurringChargeSerializer,
    ReminderSettingsSerializer,
    RenewAgreementSerializer,
    RentalUnitSerializer,
    RentRevisionSerializer,
    ReviseRecurringSerializer,
    SettleDepositSerializer,
    TenantSerializer,
)


class PMPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 500


class ServiceErrorMixin:
    """Turns business-rule errors into 400 responses with a machine-readable code."""

    def handle_exception(self, exc):
        if isinstance(exc, services.ServiceError):
            return response.Response({"detail": exc.message, "code": exc.code, **exc.extra}, status=400)
        if isinstance(exc, DjangoValidationError):
            return response.Response({"detail": "; ".join(exc.messages)}, status=400)
        if isinstance(exc, ProtectedError):
            return response.Response(
                {"detail": "This record has financial or historical records linked to it and cannot be deleted. "
                           "Mark it inactive or end it instead.", "code": "protected"},
                status=400,
            )
        return super().handle_exception(exc)


def param_int(request, name):
    value = request.query_params.get(name)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        raise services.ServiceError(f"'{name}' must be a number.", code="invalid_param")


class OwnedViewSet(ServiceErrorMixin, viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = PMPagination
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    # query param -> ORM lookup
    filter_map = {}
    search_fields = []

    def get_queryset(self):
        queryset = self.queryset.filter(owner=self.request.user)
        for param, lookup in self.filter_map.items():
            value = self.request.query_params.get(param)
            if value not in (None, ""):
                if value in ("true", "false"):
                    value = value == "true"
                queryset = queryset.filter(**{lookup: value})
        return queryset

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


def _sensitive_file_response(field_file):
    if not field_file:
        raise Http404
    try:
        handle = field_file.open("rb")
    except FileNotFoundError:
        raise Http404
    content_type = mimetypes.guess_type(field_file.name)[0] or "application/octet-stream"
    resp = FileResponse(handle, content_type=content_type)
    resp["Content-Disposition"] = f'inline; filename="{field_file.name.rsplit("/", 1)[-1]}"'
    resp["Cache-Control"] = "private, no-store"
    return resp


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class PropertyViewSet(OwnedViewSet):
    queryset = Property.objects.all()
    serializer_class = PropertySerializer
    filter_map = {"status": "status", "property_type": "property_type"}
    search_fields = ["name", "address"]

    def get_queryset(self):
        return super().get_queryset().annotate(
            unit_count=Count("units", distinct=True),
            floor_count=Count("floors", distinct=True),
            occupied_count=Count(
                "units",
                filter=Q(units__agreements__status=Agreement.Status.ACTIVE)
                | Q(units__additional_agreements__status=Agreement.Status.ACTIVE),
                distinct=True,
            ),
        ).order_by("name")  # GROUP BY drops the model's default ordering

    @decorators.action(detail=True, methods=["get"])
    def overview(self, request, pk=None):
        """Everything the property screen needs in one call."""
        prop = self.get_object()
        period = services.parse_month(request.query_params.get("month"))
        floors = FloorSerializer(prop.floors.prefetch_related("sections"), many=True, context={"request": request}).data
        units = RentalUnitSerializer(
            prop.units.prefetch_related("spaces__floor", "spaces__section", "agreements__tenant", "additional_agreements__tenant"),
            many=True,
            context={"request": request},
        ).data
        collection = reports.rent_collection(request.user, period, prop.id)
        return response.Response(
            {
                "property": self.get_serializer(prop).data,
                "floors": floors,
                "units": units,
                "collection": collection,
                "balances": services.balance_summary(
                    list(prop.agreements.values_list("id", flat=True))
                ),
            }
        )


class FloorViewSet(OwnedViewSet):
    queryset = Floor.objects.prefetch_related("sections")
    serializer_class = FloorSerializer
    filter_map = {"property": "property_id"}


class FloorSectionViewSet(OwnedViewSet):
    queryset = FloorSection.objects.all()
    serializer_class = FloorSectionSerializer
    filter_map = {"floor": "floor_id", "property": "floor__property_id"}


class RentalUnitViewSet(OwnedViewSet):
    queryset = RentalUnit.objects.select_related("property").prefetch_related(
        "spaces__floor", "spaces__section", "agreements__tenant", "additional_agreements__tenant"
    )
    serializer_class = RentalUnitSerializer
    filter_map = {"property": "property_id", "status": "status"}
    search_fields = ["name"]

    def get_queryset(self):
        qs = super().get_queryset()
        occupancy = self.request.query_params.get("occupancy")
        occupied = Q(agreements__status=Agreement.Status.ACTIVE) | Q(additional_agreements__status=Agreement.Status.ACTIVE)
        if occupancy == "vacant":
            qs = qs.exclude(id__in=RentalUnit.objects.filter(occupied).values("id"))
        elif occupancy == "occupied":
            qs = qs.filter(occupied).distinct()
        return qs


# ---------------------------------------------------------------------------
# Tenants & agreements
# ---------------------------------------------------------------------------


class TenantViewSet(OwnedViewSet):
    queryset = Tenant.objects.prefetch_related("agreements__unit", "agreements__property")
    serializer_class = TenantSerializer
    filter_map = {"status": "status"}

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(full_name__icontains=search) | Q(mobile__icontains=search))
        prop = self.request.query_params.get("property")
        if prop:
            qs = qs.filter(agreements__property_id=prop).distinct()
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["show_identity"] = self.action in ("retrieve", "update", "partial_update", "create")
        return ctx

    @decorators.action(detail=True, methods=["get"])
    def summary(self, request, pk=None):
        """Tenant details: agreements, unified balance, charges, payments, deposit, documents."""
        tenant = self.get_object()
        services.generate_charges(request.user, agreements=tenant.agreements.all())
        agreements = list(
            tenant.agreements.select_related("unit", "property").prefetch_related("rent_revisions", "additional_units")
        )
        ids = [a.id for a in agreements]
        ctx = {"request": request, "expiry_window": services.reminder_window(request.user)}
        current = [a for a in agreements if a.status == Agreement.Status.ACTIVE]
        charges = services.annotate_charges(
            Charge.objects.filter(agreement_id__in=ids).select_related("tenant").prefetch_related("adjustments")
        ).order_by("-due_date", "-id")
        payments = services.annotate_payments(
            Payment.objects.filter(agreement_id__in=ids).select_related("tenant", "agreement__unit").prefetch_related("allocations__charge")
        )
        return response.Response(
            {
                "tenant": TenantSerializer(tenant, context={**ctx, "show_identity": True}).data,
                "current_agreements": AgreementSerializer(current, many=True, context=ctx).data,
                "previous_agreements": AgreementSerializer(
                    [a for a in agreements if a.status != Agreement.Status.ACTIVE], many=True, context=ctx
                ).data,
                "balance": services.balance_summary(ids),
                "deposit": services.deposit_balance(ids),
                "current_month": [
                    {"agreement_id": a.id, **services.period_dues(a, services.month_start(services.today()))}
                    for a in current
                ],
                "rent_revisions": RentRevisionSerializer(
                    RentRevision.objects.filter(agreement_id__in=ids), many=True
                ).data,
                "recurring_charges": RecurringChargeSerializer(
                    RecurringCharge.objects.filter(
                        Q(agreement_id__in=ids)
                        | Q(unit_id__in=[uid for a in current for uid in a.all_unit_ids])
                        | Q(property_id__in=[a.property_id for a in current])
                    ).select_related("agreement__tenant", "agreement__unit", "unit", "property"),
                    many=True,
                    context=ctx,
                ).data,
                "charges": ChargeSerializer(charges, many=True, context=ctx).data,
                "payments": PaymentSerializer(payments, many=True, context=ctx).data,
                "deposit_transactions": DepositTransactionSerializer(
                    DepositTransaction.objects.filter(agreement_id__in=ids), many=True, context=ctx
                ).data,
                "expenses": ExpenseSerializer(
                    Expense.objects.filter(agreement_id__in=ids).select_related("property", "unit", "agreement__tenant"),
                    many=True,
                    context=ctx,
                ).data,
                "documents": DocumentSerializer(
                    Document.objects.filter(Q(tenant=tenant) | Q(agreement_id__in=ids)), many=True, context=ctx
                ).data,
            }
        )


class AgreementViewSet(OwnedViewSet):
    queryset = Agreement.objects.select_related("tenant", "unit", "property", "renewed_to").prefetch_related(
        "rent_revisions", "additional_units"
    )
    serializer_class = AgreementSerializer
    filter_map = {"property": "property_id", "tenant": "tenant_id", "status": "status"}

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request.user.is_authenticated:
            ctx["expiry_window"] = services.reminder_window(self.request.user)
        return ctx

    def get_queryset(self):
        qs = super().get_queryset()
        expiry = self.request.query_params.get("expiry")
        if expiry:
            window = services.reminder_window(self.request.user)
            on = services.today()
            active = qs.filter(status=Agreement.Status.ACTIVE)
            if expiry == "expired":
                qs = active.filter(end_date__lt=on)
            elif expiry == "expiring_soon":
                qs = active.filter(end_date__gte=on, end_date__lte=on + timedelta(days=window))
            elif expiry == "active":
                qs = active
            elif expiry == "renewed":
                qs = qs.filter(status=Agreement.Status.RENEWED)
        unit = self.request.query_params.get("unit")
        if unit:
            qs = qs.filter(Q(unit_id=unit) | Q(additional_units=unit)).distinct()
        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(tenant__full_name__icontains=search) | Q(unit__name__icontains=search) | Q(additional_units__name__icontains=search)
            ).distinct()
        return qs

    def perform_create(self, serializer):
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        agreement = self.get_object()
        if agreement.charges.exists() or agreement.payments.exists() or agreement.deposit_transactions.exists():
            raise services.ServiceError(
                "Agreement has financial history and cannot be deleted. End or terminate it instead.", code="protected"
            )
        return super().destroy(request, *args, **kwargs)

    @decorators.action(detail=True, methods=["post"])
    def renew(self, request, pk=None):
        agreement = self.get_object()
        data = RenewAgreementSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        new = services.renew_agreement(agreement=agreement, **data.validated_data)
        return response.Response(self.get_serializer(new).data, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=["post"])
    def end(self, request, pk=None):
        agreement = self.get_object()
        data = EndAgreementSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        agreement, info = services.end_agreement(agreement=agreement, **data.validated_data)
        return response.Response({**self.get_serializer(agreement).data, **info})

    @decorators.action(detail=True, methods=["get", "post"], url_path="rent-revisions")
    def rent_revisions(self, request, pk=None):
        agreement = self.get_object()
        if request.method == "GET":
            return response.Response(RentRevisionSerializer(agreement.rent_revisions.all(), many=True).data)
        data = RentRevisionSerializer(data=request.data, context={"request": request})
        data.is_valid(raise_exception=True)
        revision = services.revise_rent(
            agreement=agreement,
            new_rent=data.validated_data["new_rent"],
            effective_date=data.validated_data["effective_date"],
            reason=data.validated_data.get("reason", ""),
            document=data.validated_data.get("document"),
        )
        return response.Response(RentRevisionSerializer(revision).data, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=["get", "post"], parser_classes=[MultiPartParser, FormParser, JSONParser])
    def documents(self, request, pk=None):
        agreement = self.get_object()
        if request.method == "GET":
            docs = agreement.documents.all()
            return response.Response(DocumentSerializer(docs, many=True, context={"request": request}).data)
        data = request.data.copy()
        data["agreement"] = agreement.id
        serializer = DocumentSerializer(data=data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save(owner=request.user)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=["get"])
    def balance(self, request, pk=None):
        agreement = self.get_object()
        services.generate_charges(request.user, agreements=Agreement.objects.filter(pk=agreement.pk))
        open_charges = services.outstanding_charges([agreement.id])
        return response.Response(
            {
                "balance": services.balance_summary([agreement.id]),
                "deposit": services.deposit_balance([agreement.id]),
                "open_charges": [
                    {"id": c.id, "type": c.charge_type, "description": c.description, "due_date": c.due_date,
                     "outstanding": str(o)}
                    for c, o in open_charges
                ],
            }
        )

    @decorators.action(detail=True, methods=["post"], url_path="apply-credit")
    def apply_credit(self, request, pk=None):
        rows = services.apply_credit(self.get_object())
        return response.Response({"allocated": str(sum((r.amount for r in rows), services.ZERO)), "count": len(rows)})


class ReminderSettingsView(ServiceErrorMixin, APIView):
    def get(self, request):
        obj, _ = ReminderSettings.objects.get_or_create(owner=request.user)
        return response.Response(ReminderSettingsSerializer(obj).data)

    def put(self, request):
        obj, _ = ReminderSettings.objects.get_or_create(owner=request.user)
        serializer = ReminderSettingsSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        services.generate_reminders(request.user)
        return response.Response(serializer.data)

    patch = put


class AgreementReminderViewSet(
    ServiceErrorMixin, mixins.ListModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet
):
    queryset = AgreementReminder.objects.select_related("agreement__tenant", "agreement__unit")
    serializer_class = AgreementReminderSerializer
    pagination_class = PMPagination

    def get_queryset(self):
        qs = self.queryset.filter(owner=self.request.user)
        if self.request.query_params.get("include_dismissed") != "true":
            qs = qs.filter(dismissed=False, agreement__status=Agreement.Status.ACTIVE)
        return qs

    def list(self, request, *args, **kwargs):
        services.generate_reminders(request.user)
        return super().list(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class ChargeViewSet(OwnedViewSet):
    """Charges are generated by workflows. Only one-time charges can be created here; none can be edited or deleted."""

    queryset = Charge.objects.select_related("tenant").prefetch_related("adjustments")
    serializer_class = ChargeSerializer
    http_method_names = ["get", "post", "head", "options"]
    filter_map = {
        "agreement": "agreement_id", "tenant": "tenant_id", "property": "property_id",
        "charge_type": "charge_type", "is_void": "is_void",
    }

    def get_queryset(self):
        qs = services.annotate_charges(super().get_queryset())
        month = self.request.query_params.get("month")
        if month:
            qs = qs.filter(period=services.parse_month(month))
        return qs

    def perform_create(self, serializer):
        serializer.save()

    @decorators.action(detail=False, methods=["post"])
    def generate(self, request):
        """Generate rent + fixed charges for a month (idempotent)."""
        period = services.parse_month(request.data.get("month"))
        created = services.generate_charges(request.user, period)
        return response.Response({"month": period.strftime("%Y-%m"), "created": created})

    @decorators.action(detail=True, methods=["post"])
    def adjust(self, request, pk=None):
        charge = self.get_object()
        data = AdjustmentInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        adjustment = services.adjust_charge(charge, data.validated_data["amount"], data.validated_data["reason"], request.user)
        return response.Response(ChargeAdjustmentSerializer(adjustment).data, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        charge = self.get_object()
        data = ReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.void_charge(charge, data.validated_data["reason"])
        return response.Response(self.get_serializer(self.get_queryset().get(pk=charge.pk)).data)


class PaymentViewSet(OwnedViewSet):
    """Payments are immutable. Create, list, and reverse with a reason."""

    queryset = Payment.objects.select_related("tenant", "agreement__unit").prefetch_related("allocations__charge")
    serializer_class = PaymentSerializer
    http_method_names = ["get", "post", "head", "options"]
    filter_map = {"agreement": "agreement_id", "tenant": "tenant_id", "property": "agreement__property_id",
                  "method": "method", "is_reversed": "is_reversed"}

    def get_queryset(self):
        qs = services.annotate_payments(super().get_queryset())
        month = self.request.query_params.get("month")
        if month:
            start = services.parse_month(month)
            qs = qs.filter(payment_date__range=(start, services.month_end(start)))
        return qs

    def create(self, request, *args, **kwargs):
        data = RecordPaymentSerializer(data=request.data, context={"request": request})
        data.is_valid(raise_exception=True)
        v = data.validated_data
        result = services.record_payment(
            owner=request.user,
            user=request.user,
            agreement=v["agreement"],
            amount=v["amount"],
            payment_date=v.get("payment_date"),
            method=v["method"],
            reference=v.get("reference", ""),
            notes=v.get("notes", ""),
            charge_ids=v.get("charge_ids") or None,
            idempotency_key=v.get("idempotency_key") or None,
            allow_excess=v.get("excess_handling") == "advance_credit",
        )
        payment = self.get_queryset().get(pk=result.payment.pk)
        body = {
            **PaymentSerializer(payment, context={"request": request}).data,
            "duplicate": not result.created,
            "agreement_balance": services.balance_summary([payment.agreement_id]),
        }
        return response.Response(body, status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK)

    @decorators.action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        payment = self.get_object()
        data = ReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.reverse_payment(payment, data.validated_data["reason"])
        return response.Response(PaymentSerializer(self.get_queryset().get(pk=payment.pk), context={"request": request}).data)


class RecurringChargeViewSet(OwnedViewSet):
    queryset = RecurringCharge.objects.select_related("agreement__tenant", "agreement__unit", "unit", "property")
    serializer_class = RecurringChargeSerializer
    filter_map = {"agreement": "agreement_id", "unit": "unit_id", "is_enabled": "is_enabled"}

    def get_queryset(self):
        qs = super().get_queryset()
        prop = self.request.query_params.get("property")
        if prop:
            qs = qs.filter(Q(property_id=prop) | Q(unit__property_id=prop) | Q(agreement__property_id=prop))
        return qs

    @decorators.action(detail=True, methods=["post"])
    def revise(self, request, pk=None):
        data = ReviseRecurringSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        new = services.revise_recurring_charge(
            charge=self.get_object(),
            new_amount=data.validated_data["amount"],
            effective_date=data.validated_data["effective_date"],
            notes=data.validated_data.get("notes", ""),
        )
        return response.Response(self.get_serializer(new).data, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=["post"])
    def discontinue(self, request, pk=None):
        charge = self.get_object()
        end = request.data.get("end_date")
        end = services.month_end(services.parse_month(end)) if end else services.month_end(services.today())
        if end < charge.start_date:
            raise services.ServiceError("End date is before the charge started.", code="invalid_date")
        charge.end_date = end
        charge.save(update_fields=["end_date", "updated_at"])
        return response.Response(self.get_serializer(charge).data)


class ExpenseViewSet(OwnedViewSet):
    queryset = Expense.objects.select_related("property", "unit", "agreement__tenant", "charge")
    serializer_class = ExpenseSerializer
    filter_map = {"property": "property_id", "unit": "unit_id", "agreement": "agreement_id",
                  "category": "category", "is_recoverable": "is_recoverable"}

    def get_queryset(self):
        qs = super().get_queryset()
        month = self.request.query_params.get("month")
        if month:
            start = services.parse_month(month)
            qs = qs.filter(expense_date__range=(start, services.month_end(start)))
        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(description__icontains=search))
        return qs

    def destroy(self, request, *args, **kwargs):
        expense = self.get_object()
        charge = getattr(expense, "charge", None)
        if charge and charge.allocations.filter(payment__is_reversed=False).exists():
            raise services.ServiceError("Recovery payments exist for this expense; it cannot be deleted.", code="protected")
        if charge:
            charge.is_void = True
            charge.void_reason = "Expense deleted"
            charge.expense = None
            charge.save(update_fields=["is_void", "void_reason", "expense", "updated_at"])
        return super().destroy(request, *args, **kwargs)

    @decorators.action(detail=True, methods=["get"], url_path="receipt", url_name="receipt")
    def receipt(self, request, pk=None):
        return _sensitive_file_response(self.get_object().receipt)


# ---------------------------------------------------------------------------
# Electricity
# ---------------------------------------------------------------------------


class MeterViewSet(OwnedViewSet):
    queryset = Meter.objects.select_related("property").prefetch_related("assignments__agreement__tenant", "assignments__agreement__unit")
    serializer_class = MeterSerializer
    filter_map = {"property": "property_id", "is_active": "is_active", "meter_type": "meter_type"}


class MeterAssignmentViewSet(OwnedViewSet):
    queryset = MeterAssignment.objects.select_related("agreement__tenant", "agreement__unit")
    serializer_class = MeterAssignmentSerializer
    filter_map = {"meter": "meter_id", "agreement": "agreement_id"}


class MeterReadingViewSet(OwnedViewSet):
    queryset = MeterReading.objects.select_related("meter")
    serializer_class = MeterReadingSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]
    filter_map = {"meter": "meter_id", "property": "meter__property_id"}

    def get_queryset(self):
        qs = super().get_queryset()
        month = self.request.query_params.get("month")
        if month:
            start = services.parse_month(month)
            qs = qs.filter(reading_date__range=(start, services.month_end(start)))
        return qs

    def destroy(self, request, *args, **kwargs):
        reading = self.get_object()
        if reading.meter.readings.filter(reading_date__gt=reading.reading_date).exists():
            raise services.ServiceError("Only the latest reading of a meter can be deleted.", code="not_latest")
        return super().destroy(request, *args, **kwargs)


class ElectricityBillViewSet(OwnedViewSet):
    queryset = ElectricityBill.objects.select_related("property").prefetch_related(
        "allocations__agreement__tenant", "allocations__agreement__unit", "allocations__charge"
    )
    serializer_class = ElectricityBillSerializer
    filter_map = {"property": "property_id", "status": "status"}

    def get_queryset(self):
        qs = super().get_queryset()
        month = self.request.query_params.get("month")
        if month:
            qs = qs.filter(period=services.parse_month(month))
        return qs

    def destroy(self, request, *args, **kwargs):
        bill = self.get_object()
        if bill.status != ElectricityBill.Status.DRAFT:
            raise services.ServiceError("Allocated bills cannot be deleted.", code="protected")
        return super().destroy(request, *args, **kwargs)

    @decorators.action(detail=True, methods=["get"], url_path="candidates")
    def candidates(self, request, pk=None):
        """Agreements that could share this bill (active in the property during the bill month), with meter links."""
        bill = self.get_object()
        start, end = bill.period, services.month_end(bill.period)
        agreements = Agreement.objects.filter(owner=request.user, property=bill.property, start_date__lte=end).filter(
            Q(status=Agreement.Status.ACTIVE) | Q(move_out_date__gte=start) | Q(billing_end_date__gte=start)
        ).select_related("tenant", "unit").prefetch_related("additional_units")
        units, detail, needs_manual = services.units_from_readings(bill, agreements)
        return response.Response(
            [
                {"agreement_id": a.id, "tenant_name": a.tenant.full_name, "unit_name": a.units_label, "status": a.status,
                 "reading_units": str(units.get(a.id, 0)), "meters": detail.get(a.id, []),
                 "shared_meter_needs_units": a.id in needs_manual}
                for a in agreements
            ]
        )

    @decorators.action(detail=True, methods=["post"])
    def allocate(self, request, pk=None):
        bill = self.get_object()
        data = AllocateBillSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        agreements = list(
            Agreement.objects.filter(owner=request.user, id__in=v["agreement_ids"])
            .select_related("tenant", "unit")
            .prefetch_related("additional_units")
        )
        if len(agreements) != len(set(v["agreement_ids"])):
            raise services.ServiceError("One or more agreements were not found.", code="invalid_agreements")
        order = {aid: i for i, aid in enumerate(v["agreement_ids"])}
        agreements.sort(key=lambda a: order[a.id])
        if v["preview"]:
            result = services.calculate_allocation(bill, v["method"], agreements, v.get("lines"), v["use_readings"])
            return response.Response(_preview_json(result))
        try:
            bill, result = services.allocate_bill(
                bill=bill, method=v["method"], agreements=agreements, lines=v.get("lines"),
                use_readings=v["use_readings"], confirm=v["confirm"],
            )
        except services.ServiceError as exc:
            if "preview" in exc.extra:
                exc.extra["preview"] = _preview_json(exc.extra["preview"])
            raise
        bill = self.get_queryset().get(pk=bill.pk)
        return response.Response({"bill": self.get_serializer(bill).data, "preview": _preview_json(result)})

    @decorators.action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        bill = self.get_object()
        if bill.status != ElectricityBill.Status.DRAFT:
            raise services.ServiceError("Only draft bills can be cancelled.", code="not_draft")
        bill.status = ElectricityBill.Status.CANCELLED
        bill.save(update_fields=["status", "updated_at"])
        return response.Response(self.get_serializer(bill).data)

    @decorators.action(detail=True, methods=["get"], url_path="attachment", url_name="attachment")
    def attachment(self, request, pk=None):
        return _sensitive_file_response(self.get_object().attachment)


def _preview_json(result):
    return {
        "lines": [
            {
                "agreement_id": line["agreement"].id,
                "tenant_name": line["agreement"].tenant.full_name,
                "unit_name": line["agreement"].units_label,
                "units": str(line["units"]) if line["units"] is not None else None,
                "amount": str(line["amount"]),
                "readings": line.get("readings", []),
            }
            for line in result["lines"]
        ],
        "total": str(sum((line["amount"] for line in result["lines"]), services.ZERO)),
        "difference": str(result["difference"]),
        "warnings": result["warnings"],
    }


class ElectricityAllocationViewSet(ServiceErrorMixin, viewsets.ReadOnlyModelViewSet):
    queryset = ElectricityAllocation.objects.select_related("agreement__tenant", "agreement__unit", "charge")
    serializer_class = ElectricityAllocationSerializer
    pagination_class = PMPagination

    def get_queryset(self):
        qs = self.queryset.filter(owner=self.request.user)
        for param, lookup in {"bill": "bill_id", "agreement": "agreement_id"}.items():
            if self.request.query_params.get(param):
                qs = qs.filter(**{lookup: self.request.query_params[param]})
        return qs

    @decorators.action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        allocation = self.get_object()
        data = CorrectAllocationSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.correct_allocation(allocation=allocation, **{"new_amount": data.validated_data["amount"], "reason": data.validated_data["reason"]})
        bill = allocation.bill
        return response.Response(
            {
                "allocation": self.get_serializer(self.get_queryset().get(pk=allocation.pk)).data,
                "bill_amount": str(bill.amount),
                "bill_allocated_total": str(services.bill_allocated_total(bill)),
            }
        )


# ---------------------------------------------------------------------------
# Deposits, documents, dashboard, reports
# ---------------------------------------------------------------------------


class DepositTransactionViewSet(OwnedViewSet):
    queryset = DepositTransaction.objects.all()
    serializer_class = DepositTransactionSerializer
    http_method_names = ["get", "post", "head", "options"]
    filter_map = {"agreement": "agreement_id", "txn_type": "txn_type"}

    @decorators.action(detail=False, methods=["post"])
    def settle(self, request):
        data = SettleDepositSerializer(data=request.data, context={"request": request})
        data.is_valid(raise_exception=True)
        v = data.validated_data
        agreement = v.pop("agreement")
        v["settle_date"] = v.get("settle_date") or services.today()
        txns = services.settle_deposit(owner=request.user, user=request.user, agreement=agreement, **v)
        return response.Response(
            {
                "transactions": DepositTransactionSerializer(txns, many=True).data,
                "deposit": services.deposit_balance([agreement.id]),
                "balance": services.balance_summary([agreement.id]),
            },
            status=status.HTTP_201_CREATED,
        )

    @decorators.action(detail=False, methods=["get"])
    def balance(self, request):
        agreement = get_object_or_404(Agreement, pk=param_int(request, "agreement"), owner=request.user)
        return response.Response(services.deposit_balance([agreement.id]))


class DocumentViewSet(OwnedViewSet):
    queryset = Document.objects.all()
    serializer_class = DocumentSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filter_map = {"property": "property_id", "tenant": "tenant_id", "agreement": "agreement_id", "doc_type": "doc_type"}

    @decorators.action(detail=True, methods=["get"], url_path="download", url_name="download")
    def download(self, request, pk=None):
        return _sensitive_file_response(self.get_object().file)


class SignedFileView(APIView):
    """Serves a private file from a short-lived signed link created by the serializers."""

    authentication_classes = []
    permission_classes = []

    def get(self, request, token):
        from django.core import signing

        from .serializers import FILE_LINK_MAX_AGE, FILE_LINK_SALT

        try:
            data = signing.loads(token, salt=FILE_LINK_SALT, max_age=FILE_LINK_MAX_AGE)
        except signing.BadSignature:
            return response.Response({"detail": "Link expired or invalid. Open the file again from the app."}, status=403)
        sources = {"document": (Document, "file"), "expense": (Expense, "receipt"), "bill": (ElectricityBill, "attachment")}
        if data.get("k") not in sources:
            raise Http404
        model, field = sources[data["k"]]
        obj = get_object_or_404(model, pk=data.get("id"), owner_id=data.get("u"))
        return _sensitive_file_response(getattr(obj, field))


class DashboardView(ServiceErrorMixin, APIView):
    def get(self, request):
        period = services.parse_month(request.query_params.get("month"))
        prop = param_int(request, "property")
        return response.Response(reports.dashboard(request.user, period, prop))


class RentCollectionView(ServiceErrorMixin, APIView):
    def get(self, request):
        period = services.parse_month(request.query_params.get("month"))
        prop = param_int(request, "property")
        return response.Response(
            reports.rent_collection(request.user, period, prop, request.query_params.get("search", "").strip())
        )


class MonthlyReportView(ServiceErrorMixin, APIView):
    def get(self, request):
        period = services.parse_month(request.query_params.get("month"))
        prop = param_int(request, "property")
        return response.Response(reports.monthly_report(request.user, period, prop))


class OptionsView(APIView):
    """Choice lists for the mobile app's pickers."""

    def get(self, request):
        def choices(enum):
            return [{"value": v, "label": label} for v, label in enum.choices]

        return response.Response(
            {
                "property_types": choices(Property.PropertyType),
                "property_statuses": choices(Property.Status),
                "unit_statuses": choices(RentalUnit.Status),
                "tenant_statuses": choices(Tenant.Status),
                "agreement_statuses": choices(Agreement.Status),
                "payment_methods": [c for c in choices(Payment.Method) if c["value"] != Payment.Method.DEPOSIT],
                "charge_types": choices(Charge.ChargeType),
                "recurring_frequencies": choices(RecurringCharge.Frequency),
                "expense_categories": choices(Expense.Category),
                "meter_types": choices(Meter.MeterType),
                "allocation_methods": choices(ElectricityBill.Method),
                "document_types": choices(Document.DocType),
                "deposit_txn_types": choices(DepositTransaction.TxnType),
            }
        )
