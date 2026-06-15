import calendar
import smtplib
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.mail import EmailMessage
from django.core.mail.backends.smtp import EmailBackend
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.views import APIView

from .email_utils import render_template_text
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
    Project,
    SMTPConfig,
    SalaryChangeHistory,
    SalaryRecord,
    SalaryRule,
    Task,
    WorkMember,
)
from .serializers import (
    AttachmentSerializer,
    DesignationHistorySerializer,
    EmailLogSerializer,
    EmailTemplateSerializer,
    EmailVariableSerializer,
    EmailTypeSerializer,
    FreelanceSourceSerializer,
    JobSerializer,
    LeaveRecordSerializer,
    MeetingSerializer,
    ProjectSerializer,
    SMTPConfigSerializer,
    SalaryChangeHistorySerializer,
    SalaryRecordSerializer,
    SalaryRuleSerializer,
    TaskSerializer,
    WorkMemberSerializer,
    normalize_action_items,
)


def money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def month_bounds(month_value):
    if isinstance(month_value, str):
        year, month = [int(part) for part in month_value[:7].split("-")]
    else:
        year, month = month_value.year, month_value.month
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end






def add_months(day, months=1):
    year = day.year + ((day.month - 1 + months) // 12)
    month = ((day.month - 1 + months) % 12) + 1
    return date(year, month, 1)


def clamp_day(year, month, day):
    return min(int(day or 1), calendar.monthrange(year, month)[1])


def due_date_for_period(month_start, due_day):
    """Postpaid due date: salary/payment for a month is due in the next month."""
    next_month = add_months(month_start, 1)
    return next_month.replace(day=clamp_day(next_month.year, next_month.month, due_day or 1))


def iter_month_starts(start_date, stop_month_start):
    if not start_date:
        return
    cursor = start_date.replace(day=1)
    while cursor <= stop_month_start:
        yield cursor
        cursor = add_months(cursor, 1)


def safe_working_days(value):
    if not value:
        return [0, 1, 2, 3, 4]
    days = []
    for day in value:
        try:
            day = int(day)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and day not in days:
            days.append(day)
    return days or [0, 1, 2, 3, 4]


def count_working_days(start_date, end_date, weekdays):
    weekdays = set(safe_working_days(weekdays))
    if not start_date or not end_date or start_date > end_date:
        return 0
    total = 0
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() in weekdays:
            total += 1
        cursor += timedelta(days=1)
    return total


def job_cycle_bounds(job, month_value):
    month_start, month_end = month_bounds(month_value)
    start_day = min(int(job.salary_cycle_start_day or 1), calendar.monthrange(month_start.year, month_start.month)[1])
    end_day = min(int(job.salary_cycle_end_day or month_end.day), calendar.monthrange(month_start.year, month_start.month)[1])
    period_start = month_start.replace(day=start_day)
    period_end = month_start.replace(day=end_day)
    if period_end < period_start:
        # Overnight cycle support: e.g. 26 to 25. Current month starts at cycle_start; end is next month cycle_end.
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end_day = min(int(job.salary_cycle_end_day or 25), calendar.monthrange(next_month.year, next_month.month)[1])
        period_end = next_month.replace(day=end_day)
    return period_start, period_end

def build_job_salary_calculation(job, month_value):
    """Pro-rata salary using the job's cycle and working-day pattern.

    Working days are Python weekday numbers: Monday=0 ... Sunday=6.
    Default is Monday-Friday. This fixes the earlier calendar-day-only logic.
    """
    period_start, period_end = job_cycle_bounds(job, month_value)
    active_start = max(period_start, job.joining_date)
    if active_start > period_end:
        return Decimal("0.00"), []

    try:
        weekdays = job.salary_rule.working_days or job.working_days
    except SalaryRule.DoesNotExist:
        weekdays = job.working_days
    weekdays = safe_working_days(weekdays)

    total_working_days = Decimal(str(count_working_days(period_start, period_end, weekdays)))
    if total_working_days <= 0:
        return Decimal("0.00"), []

    changes = list(
        SalaryChangeHistory.objects.filter(owner=job.owner, job=job, effective_date__lte=period_end)
        .order_by("effective_date", "id")
    )

    salary = Decimal(job.current_salary or 0)
    for change in changes:
        if change.effective_date <= active_start:
            salary = Decimal(change.new_amount)
            continue
        salary = Decimal(change.old_amount or salary)
        break

    month_changes = [c for c in changes if active_start < c.effective_date <= period_end]
    cursor = active_start
    lines = []

    for change in month_changes:
        to_date = change.effective_date - timedelta(days=1)
        days_count = count_working_days(cursor, to_date, weekdays)
        if cursor <= to_date and days_count > 0:
            days = Decimal(str(days_count))
            daily_rate = money(salary / total_working_days)
            amount = money((salary * days) / total_working_days)
            lines.append({
                "from_date": cursor.isoformat(),
                "to_date": to_date.isoformat(),
                "daily_rate": str(daily_rate),
                "days": str(days),
                "amount": str(amount),
                "notes": f"Salary {salary} pro-rata · working days {weekdays}",
            })
        salary = Decimal(change.new_amount)
        cursor = change.effective_date

    if cursor <= period_end:
        days_count = count_working_days(cursor, period_end, weekdays)
        if days_count > 0:
            days = Decimal(str(days_count))
            daily_rate = money(salary / total_working_days)
            amount = money((salary * days) / total_working_days)
            lines.append({
                "from_date": cursor.isoformat(),
                "to_date": period_end.isoformat(),
                "daily_rate": str(daily_rate),
                "days": str(days),
                "amount": str(amount),
                "notes": f"Salary {salary} pro-rata · working days {weekdays}",
            })

    expected = money(sum(Decimal(line["amount"]) for line in lines))
    return expected, lines

def daily_monthly_amount(amount, day):
    """Daily accrual for a monthly freelance retainer using actual days in each month."""
    amount = Decimal(amount or 0)
    days_in_month = Decimal(str(calendar.monthrange(day.year, day.month)[1]))
    if days_in_month <= 0:
        return Decimal("0.00")
    return amount / days_in_month


def daterange(start_date, end_date):
    cursor = start_date
    while cursor <= end_date:
        yield cursor
        cursor += timedelta(days=1)


def accrued_monthly_amount(amount, start_date, end_date):
    """Accrue a monthly amount over an arbitrary date range.

    Option B from discussion: use the actual number of days in each calendar
    month. Example: March uses /31, April uses /30.
    """
    if not start_date or not end_date or start_date > end_date:
        return Decimal("0.00"), []
    amount = Decimal(amount or 0)
    lines = []
    cursor = start_date
    total = Decimal("0.00")
    while cursor <= end_date:
        month_end = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
        line_end = min(month_end, end_date)
        days = Decimal(str((line_end - cursor).days + 1))
        daily_rate_raw = daily_monthly_amount(amount, cursor)
        line_amount = money(daily_rate_raw * days)
        total += line_amount
        lines.append({
            "from_date": cursor.isoformat(),
            "to_date": line_end.isoformat(),
            "daily_rate": str(money(daily_rate_raw)),
            "days": str(days),
            "amount": str(line_amount),
            "notes": f"Monthly retainer pro-rata using actual {calendar.monthrange(cursor.year, cursor.month)[1]} days in {cursor:%B %Y}",
        })
        cursor = line_end + timedelta(days=1)
    return money(total), lines


def salary_record_period_end(record):
    if record.period_end:
        return record.period_end
    if record.received_date:
        # Legacy rows often used received date as the period marker. Payment on
        # 23 Apr means work earned through 22 Apr, next period starts 23 Apr.
        return record.received_date - timedelta(days=1)
    if record.month:
        return month_bounds(record.month)[1]
    return None


def freelance_scope_filter(queryset, project=None, freelance_source=None):
    if project:
        return queryset.filter(project=project)
    if freelance_source:
        return queryset.filter(project__isnull=True, freelance_source=freelance_source)
    return queryset.none()


def last_freelance_period_end(owner, project=None, freelance_source=None, exclude_record_id=None):
    qs = SalaryRecord.objects.filter(owner=owner, salary_type=SalaryRecord.SalaryType.FREELANCING)
    qs = freelance_scope_filter(qs, project=project, freelance_source=freelance_source)
    if exclude_record_id:
        qs = qs.exclude(id=exclude_record_id)
    latest = None
    for record in qs.order_by("-period_end", "-received_date", "-month", "-id"):
        end = salary_record_period_end(record)
        if end and (latest is None or end > latest):
            latest = end
    return latest


def default_freelance_period_start(owner, project=None, freelance_source=None, exclude_record_id=None):
    last_end = last_freelance_period_end(owner, project=project, freelance_source=freelance_source, exclude_record_id=exclude_record_id)
    if last_end:
        return last_end + timedelta(days=1)
    if project and project.start_date:
        return project.start_date
    if project:
        return project.created_at.date()
    if freelance_source:
        return freelance_source.created_at.date()
    return timezone.localdate()


def build_freelance_payment_calculation(owner, project=None, freelance_source=None, month_value=None, received_date=None, period_start=None, period_end=None, exclude_record_id=None):
    """Return freelance expected amount.

    Monthly project/source payments use a running ledger style calculation with
    actual month-day pro-rata (Option B). The default period is:
      previous paid period end + 1  →  received_date - 1
    This supports payments arriving late, e.g. Mar 1 to Apr 22 paid on Apr 23.
    """
    amount = Decimal("0.00")
    notes = "Manual freelance payment"

    if project:
        if project.billing_type == Project.BillingType.FIXED_PROJECT:
            amount = Decimal(project.billing_amount or project.budget or 0)
            notes = "Fixed project amount"
            start, end = month_bounds(month_value or date.today())
            lines = [{
                "from_date": start.isoformat(),
                "to_date": end.isoformat(),
                "daily_rate": str(money(amount)),
                "days": "1.00",
                "amount": str(money(amount)),
                "notes": notes,
            }] if amount > 0 else []
            return money(amount), lines, start, end
        elif project.billing_type == Project.BillingType.MONTHLY:
            amount = Decimal(project.billing_amount or 0)
            notes = "Monthly project retainer pro-rata"
        else:
            amount = Decimal(project.billing_amount or project.budget or 0)
            notes = "Manual project amount suggestion"
    elif freelance_source and freelance_source.default_billing_type == FreelanceSource.DefaultBillingType.MONTHLY:
        amount = Decimal(freelance_source.default_monthly_amount or 0)
        notes = "Monthly source retainer pro-rata"
        if amount <= 0:
            project_amounts = Project.objects.filter(
                owner=owner,
                freelance_source=freelance_source,
                source_type=Project.SourceType.FREELANCING,
                status__in=[Project.Status.ACTIVE, Project.Status.IN_PROGRESS],
                billing_type=Project.BillingType.MONTHLY,
            ).aggregate(total=Sum("billing_amount"))["total"] or Decimal("0.00")
            amount = Decimal(project_amounts or 0)
            notes = "Auto total from active monthly freelance projects pro-rata"
    else:
        month_start, month_end = month_bounds(month_value or date.today())
        amount = money(amount)
        return amount, [], month_start, month_end

    if project and project.billing_type != Project.BillingType.MONTHLY:
        month_start, month_end = month_bounds(month_value or date.today())
        amount = money(amount)
        lines = [{
            "from_date": month_start.isoformat(),
            "to_date": month_end.isoformat(),
            "daily_rate": str(amount),
            "days": "1.00",
            "amount": str(amount),
            "notes": notes,
        }] if amount > 0 else []
        return amount, lines, month_start, month_end

    if isinstance(received_date, str) and received_date:
        received_date = date.fromisoformat(received_date[:10])
    if isinstance(period_start, str) and period_start:
        period_start = date.fromisoformat(period_start[:10])
    if isinstance(period_end, str) and period_end:
        period_end = date.fromisoformat(period_end[:10])

    start = period_start or default_freelance_period_start(owner, project=project, freelance_source=freelance_source, exclude_record_id=exclude_record_id)
    if period_end:
        end = period_end
    elif received_date:
        end = received_date - timedelta(days=1)
    else:
        _month_start, end = month_bounds(month_value or date.today())

    if end < start:
        end = start

    expected, lines = accrued_monthly_amount(amount, start, end)
    if lines:
        for line in lines:
            line["notes"] = f"{notes} · {line['notes']}"
    return expected, lines, start, end


def freelance_due_cutoff(project, today):
    """Return last work date that is due as of today for a monthly project.

    Due is postpaid: May work becomes due on the configured due day in June.
    """
    due_day = project.payment_due_day or (project.freelance_source.payment_due_day if project.freelance_source_id else 1)
    current_due = today.replace(day=clamp_day(today.year, today.month, due_day))
    if today < current_due:
        due_month = add_months(today.replace(day=1), -2)
    else:
        due_month = add_months(today.replace(day=1), -1)
    return month_bounds(due_month)[1]

def normalize_time_value(value):
    """Accept FE `HH:MM` values and convert blank strings to None."""
    if value in (None, ""):
        return None
    return value


def list_from_value(value):
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def find_or_create_member_from_text(owner, text):
    """The current FE task grid has a free text `assigned` column.

    To keep that UX working without a heavy dropdown, we resolve by name/email.
    If no existing member is found, a lightweight member record is created for
    this owner only, so other users still cannot see or reuse it.
    """
    name = (text or "").strip()
    if not name:
        return None
    member = WorkMember.objects.filter(owner=owner).filter(Q(name__iexact=name) | Q(email__iexact=name)).first()
    if member:
        return member
    return WorkMember.objects.create(owner=owner, name=name, member_type=WorkMember.MemberType.EMPLOYEE)


def apply_common_source_fields(item, common, object_type_key):
    """Map the FE wizard shape into serializer fields.

    FE currently keeps source/project in wizard state as:
    - type/task_type/meeting_type
    - source
    - project

    DRF models use job/freelance_source/projects. This helper accepts both.
    """
    data = dict(item)
    item_type = data.get(object_type_key) or data.get("type") or common.get(object_type_key) or common.get("type")
    if item_type:
        data[object_type_key] = item_type

    source = data.pop("source", None) or data.pop("source_id", None) or common.get("source") or common.get("source_id")
    if item_type == "job":
        data.setdefault("job", data.get("job") or common.get("job") or source)
    elif item_type == "freelancing":
        data.setdefault("freelance_source", data.get("freelance_source") or common.get("freelance_source") or source)
    elif item_type in ("manual", "general"):
        manual_source = (
            data.get("manual_source_name")
            or data.pop("source_name", None)
            or common.get("manual_source_name")
            or common.get("source_name")
        )
        if manual_source:
            data["manual_source_name"] = manual_source

    project = data.pop("project", None) or data.pop("project_id", None) or common.get("project") or common.get("project_id")
    projects = data.get("projects") or common.get("projects") or list_from_value(project)
    if projects:
        data["projects"] = projects
    return data


def build_task_bulk_payload(request, item, common):
    data = apply_common_source_fields(item, common, "task_type")
    # FE inline table uses `start` / `end`.
    if "start" in data and "start_time" not in data:
        data["start_time"] = normalize_time_value(data.pop("start"))
    if "end" in data and "end_time" not in data:
        data["end_time"] = normalize_time_value(data.pop("end"))

    assigned_text = data.pop("assigned", None) or data.pop("assigned_name", None)
    if assigned_text and not data.get("assigned_by"):
        member = find_or_create_member_from_text(request.user, assigned_text)
        if member:
            data["assigned_by"] = member.id

    # FE may send one `member` or selected member ids.
    if "member" in data and "members" not in data:
        data["members"] = list_from_value(data.pop("member"))
    return data


def build_meeting_bulk_payload(item, common):
    data = apply_common_source_fields(item, common, "meeting_type")
    if "start" in data and "start_time" not in data:
        data["start_time"] = normalize_time_value(data.pop("start"))
    if "end" in data and "end_time" not in data:
        data["end_time"] = normalize_time_value(data.pop("end"))

    # FE currently stores selected members as [id, id]. Convert to role rows.
    if "members" in data and "member_roles" not in data:
        data["member_roles"] = [{"member": member_id, "role": "attendee", "attended": False} for member_id in list_from_value(data.pop("members"))]
    return data


def bulk_created_response(serializer_class, created_objects, request):
    return response.Response(
        {
            "count": len(created_objects),
            "ids": [obj.id for obj in created_objects],
            "results": serializer_class(created_objects, many=True, context={"request": request}).data,
        },
        status=status.HTTP_201_CREATED,
    )


class OwnedModelViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = []

    def get_queryset(self):
        queryset = self.queryset.filter(owner=self.request.user)
        return self.apply_common_filters(queryset)

    def apply_common_filters(self, queryset):
        params = self.request.query_params
        for key in ["status", "task_type", "meeting_type", "salary_type", "leave_type", "source_type", "member_type", "module"]:
            value = params.get(key)
            if value and hasattr(queryset.model, key):
                queryset = queryset.filter(**{key: value})
        return queryset

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class WorkMemberViewSet(OwnedModelViewSet):
    queryset = WorkMember.objects.all()
    serializer_class = WorkMemberSerializer
    search_fields = ["name", "email", "company", "designation"]
    ordering_fields = ["name", "member_type", "created_at"]


class JobViewSet(OwnedModelViewSet):
    queryset = Job.objects.all()
    serializer_class = JobSerializer
    search_fields = ["company_name", "job_title", "current_designation"]
    ordering_fields = ["company_name", "joining_date", "current_salary", "created_at"]

    @decorators.action(detail=True, methods=["get"], url_path="full-detail")
    def full_detail(self, request, pk=None):
        job = self.get_object()
        return response.Response({
            "job": JobSerializer(job, context={"request": request}).data,
            "projects": ProjectSerializer(job.projects.all(), many=True, context={"request": request}).data,
            "designation_history": DesignationHistorySerializer(job.designation_history.all(), many=True, context={"request": request}).data,
            "salary_changes": SalaryChangeHistorySerializer(job.salary_changes.all(), many=True, context={"request": request}).data,
        })


class FreelanceSourceViewSet(OwnedModelViewSet):
    queryset = FreelanceSource.objects.all()
    serializer_class = FreelanceSourceSerializer
    search_fields = ["name", "primary_contact_name", "email"]
    ordering_fields = ["name", "platform", "created_at"]


class ProjectViewSet(OwnedModelViewSet):
    queryset = Project.objects.select_related("job", "freelance_source")
    serializer_class = ProjectSerializer
    search_fields = ["name", "manual_source_name", "job__company_name", "freelance_source__name"]
    ordering_fields = ["name", "start_date", "budget", "created_at"]

    def apply_common_filters(self, queryset):
        queryset = super().apply_common_filters(queryset)
        params = self.request.query_params
        if params.get("job"):
            queryset = queryset.filter(job_id=params["job"])
        if params.get("freelance_source"):
            queryset = queryset.filter(freelance_source_id=params["freelance_source"])
        return queryset


class DesignationHistoryViewSet(OwnedModelViewSet):
    queryset = DesignationHistory.objects.select_related("job")
    serializer_class = DesignationHistorySerializer
    search_fields = ["job__company_name", "old_designation", "new_designation", "reason"]
    ordering_fields = ["effective_date", "created_at"]

    def perform_create(self, serializer):
        record = serializer.save(owner=self.request.user)
        if record.job.owner_id == self.request.user.id:
            record.job.current_designation = record.new_designation
            record.job.save(update_fields=["current_designation", "updated_at"])

    def perform_update(self, serializer):
        record = serializer.save()
        if record.job.owner_id == self.request.user.id:
            record.job.current_designation = record.new_designation
            record.job.save(update_fields=["current_designation", "updated_at"])


class SalaryChangeHistoryViewSet(OwnedModelViewSet):
    queryset = SalaryChangeHistory.objects.select_related("job")
    serializer_class = SalaryChangeHistorySerializer
    search_fields = ["job__company_name", "reason"]
    ordering_fields = ["effective_date", "old_amount", "new_amount", "created_at"]

    def perform_create(self, serializer):
        record = serializer.save(owner=self.request.user)
        if not record.old_amount:
            record.old_amount = record.job.current_salary
            record.save(update_fields=["old_amount", "updated_at"])
        if record.job.owner_id == self.request.user.id:
            record.job.current_salary = record.new_amount
            record.job.save(update_fields=["current_salary", "updated_at"])

    def perform_update(self, serializer):
        record = serializer.save()
        if record.job.owner_id == self.request.user.id:
            record.job.current_salary = record.new_amount
            record.job.save(update_fields=["current_salary", "updated_at"])


class SalaryRuleViewSet(OwnedModelViewSet):
    queryset = SalaryRule.objects.select_related("job")
    serializer_class = SalaryRuleSerializer
    search_fields = ["job__company_name"]
    ordering_fields = ["created_at"]


class EmailTypeViewSet(OwnedModelViewSet):
    queryset = EmailType.objects.all()
    serializer_class = EmailTypeSerializer
    search_fields = ["name", "code", "description"]
    ordering_fields = ["name", "code", "created_at"]




class EmailVariableViewSet(OwnedModelViewSet):
    queryset = EmailVariable.objects.all()
    serializer_class = EmailVariableSerializer
    search_fields = ["key", "label", "description", "category"]
    ordering_fields = ["category", "label", "key", "created_at"]

    def apply_common_filters(self, queryset):
        queryset = super().apply_common_filters(queryset)
        params = self.request.query_params
        if params.get("category"):
            queryset = queryset.filter(category=params["category"])
        if params.get("active") in ("1", "true", "True"):
            queryset = queryset.filter(is_active=True)
        return queryset


class EmailTemplateViewSet(OwnedModelViewSet):
    queryset = EmailTemplate.objects.select_related("email_type")
    serializer_class = EmailTemplateSerializer
    search_fields = ["name", "subject", "body", "email_type__name"]
    ordering_fields = ["name", "created_at"]

    def apply_common_filters(self, queryset):
        queryset = super().apply_common_filters(queryset)
        if self.request.query_params.get("email_type"):
            queryset = queryset.filter(email_type_id=self.request.query_params["email_type"])
        return queryset


class SMTPConfigViewSet(OwnedModelViewSet):
    queryset = SMTPConfig.objects.all()
    serializer_class = SMTPConfigSerializer
    search_fields = ["host", "username", "from_email"]
    ordering_fields = ["created_at"]

    @decorators.action(detail=True, methods=["post"], url_path="test")
    def test_connection(self, request, pk=None):
        config = self.get_object()
        try:
            with smtplib.SMTP_SSL(config.host, config.port, timeout=8) if config.use_ssl else smtplib.SMTP(config.host, config.port, timeout=8) as server:
                if config.use_tls and not config.use_ssl:
                    server.starttls()
                server.login(config.username, config.password)
            config.last_test_status = "success"
            config.last_test_message = "SMTP connection successful."
            http_status = status.HTTP_200_OK
        except Exception as exc:
            config.last_test_status = "failed"
            config.last_test_message = str(exc)
            http_status = status.HTTP_400_BAD_REQUEST
        config.last_tested_at = timezone.now()
        config.save(update_fields=["last_test_status", "last_test_message", "last_tested_at"])
        return response.Response(SMTPConfigSerializer(config, context={"request": request}).data, status=http_status)


class TaskViewSet(OwnedModelViewSet):
    queryset = Task.objects.select_related("job", "freelance_source", "assigned_by").prefetch_related("projects", "members")
    serializer_class = TaskSerializer
    search_fields = ["name", "description", "manual_source_name", "job__company_name", "freelance_source__name", "projects__name"]
    ordering_fields = ["date", "priority", "status", "created_at"]

    def apply_common_filters(self, queryset):
        queryset = super().apply_common_filters(queryset)
        params = self.request.query_params
        today = timezone.localdate()
        if params.get("today") == "1":
            queryset = queryset.filter(date=today)
        if params.get("this_week") == "1":
            queryset = queryset.filter(date__gte=today, date__lte=today + timedelta(days=7))
        if params.get("project"):
            queryset = queryset.filter(projects__id=params["project"])
        if params.get("date_from"):
            queryset = queryset.filter(date__gte=params["date_from"])
        if params.get("date_to"):
            queryset = queryset.filter(date__lte=params["date_to"])
        return queryset.distinct()

    @decorators.action(detail=False, methods=["post"], url_path="bulk-create")
    def bulk_create(self, request):
        """Create multiple task rows from the FE Add Tasks inline table.

        Accepted FE-friendly shape:
        {
          "type": "job",
          "source": 1,
          "project": 1,
          "tasks": [
            {"name": "Fix login", "date": "2026-06-14", "start": "10:00", "end": "12:00", "priority": "high", "status": "todo", "assigned": "Rahul"}
          ]
        }

        Also accepts pure API shape where every row already contains task_type,
        job/freelance_source/projects/assigned_by. The request is all-or-nothing.
        """
        if isinstance(request.data, list):
            rows = request.data
        else:
            rows = request.data.get("tasks") or request.data.get("rows") or request.data.get("items")
        if not isinstance(rows, list) or not rows:
            return response.Response({"detail": "tasks must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)

        common = request.data if isinstance(request.data, dict) else {}
        with transaction.atomic():
            payload = [build_task_bulk_payload(request, row, common) for row in rows]
            serializer = TaskSerializer(data=payload, many=True, context={"request": request})
            serializer.is_valid(raise_exception=True)
            created = serializer.save(owner=request.user)
        return bulk_created_response(TaskSerializer, created, request)


class MeetingViewSet(OwnedModelViewSet):
    queryset = Meeting.objects.select_related("job", "freelance_source").prefetch_related("projects", "meetingmember_set__member")
    serializer_class = MeetingSerializer
    search_fields = ["title", "agenda", "mom", "conclusion", "manual_source_name", "job__company_name", "freelance_source__name"]
    ordering_fields = ["date", "start_time", "status", "created_at"]

    def apply_common_filters(self, queryset):
        queryset = super().apply_common_filters(queryset)
        params = self.request.query_params
        if params.get("project"):
            queryset = queryset.filter(projects__id=params["project"])
        if params.get("date_from"):
            queryset = queryset.filter(date__gte=params["date_from"])
        if params.get("date_to"):
            queryset = queryset.filter(date__lte=params["date_to"])
        return queryset.distinct()

    @decorators.action(detail=False, methods=["get"], url_path="calendar")
    def calendar(self, request):
        """Calendar-friendly grouped meeting data for the /meetings calendar toggle."""
        qs = self.filter_queryset(self.get_queryset()).order_by("date", "start_time")
        days = {}
        for meeting in qs:
            key = meeting.date.isoformat()
            days.setdefault(key, []).append(MeetingSerializer(meeting, context={"request": request}).data)
        return response.Response({"days": days, "count": qs.count()})

    @decorators.action(detail=False, methods=["post"], url_path="bulk-create")
    def bulk_create(self, request):
        """Create multiple meetings in one request.

        Accepted FE-friendly shape:
        {
          "type": "job",
          "source": 1,
          "project": 1,
          "meetings": [
            {"title": "Sprint Planning", "date": "2026-06-14", "start": "10:00", "end": "11:00", "members": [1, 2]}
          ]
        }

        Each row may also include mom/conclusion/next_action and member_roles.
        The request is all-or-nothing.
        """
        if isinstance(request.data, list):
            rows = request.data
        else:
            rows = request.data.get("meetings") or request.data.get("rows") or request.data.get("items")
        if not isinstance(rows, list) or not rows:
            return response.Response({"detail": "meetings must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)

        common = request.data if isinstance(request.data, dict) else {}
        with transaction.atomic():
            payload = [build_meeting_bulk_payload(row, common) for row in rows]
            serializer = MeetingSerializer(data=payload, many=True, context={"request": request})
            serializer.is_valid(raise_exception=True)
            created = serializer.save(owner=request.user)
        return bulk_created_response(MeetingSerializer, created, request)

    @decorators.action(detail=True, methods=["patch"], url_path="mom")
    def update_mom(self, request, pk=None):
        meeting = self.get_object()
        for field in ["mom", "conclusion", "next_action", "action_items", "status"]:
            if field in request.data:
                value = normalize_action_items(request.data[field]) if field == "action_items" else request.data[field]
                setattr(meeting, field, value)
        meeting.save(update_fields=["mom", "conclusion", "next_action", "action_items", "status", "updated_at"])
        return response.Response(MeetingSerializer(meeting, context={"request": request}).data)


class LeaveRecordViewSet(OwnedModelViewSet):
    queryset = LeaveRecord.objects.select_related("job", "email_template").prefetch_related("email_recipients")
    serializer_class = LeaveRecordSerializer
    search_fields = ["title", "reason", "job__company_name"]
    ordering_fields = ["date", "deduction_amount", "created_at"]

    def apply_common_filters(self, queryset):
        queryset = super().apply_common_filters(queryset)
        params = self.request.query_params
        if params.get("job"):
            queryset = queryset.filter(job_id=params["job"])
        if params.get("date_from"):
            queryset = queryset.filter(date__gte=params["date_from"])
        if params.get("date_to"):
            queryset = queryset.filter(date__lte=params["date_to"])
        return queryset

    @decorators.action(detail=False, methods=["get"], url_path="calendar")
    def calendar(self, request):
        """Calendar-friendly grouped leave data for the /leave calendar toggle."""
        qs = self.filter_queryset(self.get_queryset()).order_by("date", "id")
        days = {}
        for leave in qs:
            key = leave.date.isoformat()
            days.setdefault(key, []).append(LeaveRecordSerializer(leave, context={"request": request}).data)
        return response.Response({"days": days, "count": qs.count()})


class SalaryRecordViewSet(OwnedModelViewSet):
    queryset = SalaryRecord.objects.select_related("job", "freelance_source", "project").prefetch_related("calculation_lines")
    serializer_class = SalaryRecordSerializer
    search_fields = ["manual_source_name", "job__company_name", "freelance_source__name", "project__name", "notes"]
    ordering_fields = ["month", "expected_amount", "received_amount", "pending_amount", "created_at"]

    def apply_common_filters(self, queryset):
        queryset = super().apply_common_filters(queryset)
        params = self.request.query_params
        if params.get("job"):
            queryset = queryset.filter(job_id=params["job"])
        if params.get("project"):
            queryset = queryset.filter(project_id=params["project"])
        if params.get("month"):
            start, end = month_bounds(params["month"])
            queryset = queryset.filter(month__gte=start, month__lte=end)
        return queryset

    @decorators.action(detail=False, methods=["post"], url_path="calculate")
    def calculate(self, request):
        salary_type = request.data.get("salary_type") or ("freelancing" if request.data.get("project") or request.data.get("freelance_source") else "job")
        month = request.data.get("month")
        if not month:
            return response.Response({"detail": "month is required."}, status=status.HTTP_400_BAD_REQUEST)

        if salary_type == SalaryRecord.SalaryType.JOB:
            job_id = request.data.get("job")
            if not job_id:
                return response.Response({"detail": "job is required for job salary calculation."}, status=status.HTTP_400_BAD_REQUEST)
            job = Job.objects.filter(owner=request.user, id=job_id).first()
            if not job:
                return response.Response({"detail": "Job not found."}, status=status.HTTP_404_NOT_FOUND)
            expected, lines = build_job_salary_calculation(job, month)
            return response.Response({"expected_amount": str(expected), "calculation_lines": lines, "calculation_type": "job"})

        if salary_type == SalaryRecord.SalaryType.FREELANCING:
            project = None
            source = None
            if request.data.get("project"):
                project = Project.objects.filter(owner=request.user, id=request.data.get("project"), source_type=Project.SourceType.FREELANCING).first()
                if not project:
                    return response.Response({"detail": "Freelance project not found."}, status=status.HTTP_404_NOT_FOUND)
                source = project.freelance_source
            elif request.data.get("freelance_source"):
                source = FreelanceSource.objects.filter(owner=request.user, id=request.data.get("freelance_source")).first()
                if not source:
                    return response.Response({"detail": "Freelance source not found."}, status=status.HTTP_404_NOT_FOUND)
            else:
                return response.Response({"detail": "project or freelance_source is required for freelance payment calculation."}, status=status.HTTP_400_BAD_REQUEST)

            expected, lines, period_start, period_end = build_freelance_payment_calculation(
                request.user,
                project=project,
                freelance_source=source,
                month_value=month,
                received_date=request.data.get("received_date"),
                period_start=request.data.get("period_start"),
                period_end=request.data.get("period_end"),
                exclude_record_id=request.data.get("record_id"),
            )
            return response.Response({
                "expected_amount": str(expected),
                "calculation_lines": lines,
                "calculation_type": "freelancing",
                "period_start": period_start.isoformat() if period_start else None,
                "period_end": period_end.isoformat() if period_end else None,
                "month": period_start.replace(day=1).isoformat() if period_start else month,
            })

        return response.Response({"detail": "Manual salary/payment uses manually entered expected_amount."}, status=status.HTTP_400_BAD_REQUEST)


class EmailLogViewSet(OwnedModelViewSet):
    queryset = EmailLog.objects.select_related("email_type", "template").prefetch_related("recipients")
    serializer_class = EmailLogSerializer
    search_fields = ["subject", "body", "to_emails", "email_type__name"]
    ordering_fields = ["created_at", "sent_at", "status"]

    def apply_common_filters(self, queryset):
        queryset = super().apply_common_filters(queryset)
        params = self.request.query_params
        if params.get("email_type"):
            queryset = queryset.filter(email_type_id=params["email_type"])
        return queryset

    @decorators.action(detail=True, methods=["post"], url_path="send")
    def send_email(self, request, pk=None):
        log = self.get_object()
        config = SMTPConfig.objects.filter(owner=request.user, is_active=True).order_by("-updated_at").first()
        if not config:
            log.status = EmailLog.Status.FAILED
            log.error_message = "SMTP is not configured."
            log.save(update_fields=["status", "error_message", "updated_at"])
            return response.Response({"detail": "SMTP is not configured."}, status=status.HTTP_400_BAD_REQUEST)

        recipients = [email.strip() for email in log.to_emails.split(",") if email.strip()]
        recipients += [m.email for m in log.recipients.all() if m.email]
        recipients = list(dict.fromkeys(recipients))
        if not recipients:
            return response.Response({"detail": "No recipient email found."}, status=status.HTTP_400_BAD_REQUEST)

        backend = EmailBackend(
            host=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
            use_tls=config.use_tls,
            use_ssl=config.use_ssl,
            timeout=12,
        )
        try:
            rendered_subject = render_template_text(log.subject, log.variable_values)
            rendered_body = render_template_text(log.body, log.variable_values)
            message = EmailMessage(
                subject=rendered_subject,
                body=rendered_body,
                from_email=f"{config.from_name} <{config.from_email}>" if config.from_name else config.from_email,
                to=recipients,
                cc=[x.strip() for x in log.cc.split(",") if x.strip()],
                bcc=[x.strip() for x in log.bcc.split(",") if x.strip()],
                connection=backend,
            )
            # Keep line breaks exactly as written in the template/body.
            message.content_subtype = "plain"
            message.send(fail_silently=False)
            log.status = EmailLog.Status.SENT
            log.sent_at = timezone.now()
            log.error_message = ""
            log.save(update_fields=["status", "sent_at", "error_message", "updated_at"])
            return response.Response(EmailLogSerializer(log, context={"request": request}).data)
        except Exception as exc:
            log.status = EmailLog.Status.FAILED
            log.error_message = str(exc)
            log.save(update_fields=["status", "error_message", "updated_at"])
            return response.Response(EmailLogSerializer(log, context={"request": request}).data, status=status.HTTP_400_BAD_REQUEST)


class AttachmentViewSet(OwnedModelViewSet):
    queryset = Attachment.objects.all()
    serializer_class = AttachmentSerializer
    search_fields = ["title", "notes", "file"]
    ordering_fields = ["created_at"]


class DashboardAPIView(APIView):
    def _existing_salary_keys(self, user):
        """Return period keys already represented by saved salary rows.

        New UI stores `month` as the actual work-month first day. Older local
        testing data sometimes stored the received date in `month` (for example
        2026-04-23 for March freelance payment). To stop dashboard virtual rows
        from double-counting those records, also cover the previous month based
        on `received_date` when available.
        """
        keys = set()

        def add_key(rec, month_value):
            if not month_value:
                return
            period = month_value.replace(day=1)
            if rec.job_id:
                keys.add((SalaryRecord.SalaryType.JOB, rec.job_id, None, period))
            elif rec.project_id:
                keys.add((SalaryRecord.SalaryType.FREELANCING, None, rec.project_id, period))
            elif rec.freelance_source_id:
                keys.add((SalaryRecord.SalaryType.FREELANCING, rec.freelance_source_id, None, period))

        for rec in SalaryRecord.objects.filter(owner=user):
            add_key(rec, rec.month)
            if rec.received_date:
                add_key(rec, add_months(rec.received_date.replace(day=1), -1))
        return keys

    def _virtual_pending_salary_items(self, request, today):
        """Show unpaid expected salary/payment rows.

        Jobs remain month based. Freelance monthly projects use running ledger
        balance so late payments and partial payments are handled correctly:

            earned from project start until last due cutoff
            - total received
            - pending already visible in saved partial records
            = extra virtual pending row
        """
        user = request.user
        month_limit = today.replace(day=1)
        existing_keys = self._existing_salary_keys(user)
        virtual = []

        for job in Job.objects.filter(owner=user, status__in=[Job.Status.ACTIVE, Job.Status.PROBATION]):
            for month_value in iter_month_starts(job.joining_date, month_limit):
                due_date = due_date_for_period(month_value, job.salary_received_day)
                if due_date > today:
                    continue
                if (SalaryRecord.SalaryType.JOB, job.id, None, month_value) in existing_keys:
                    continue
                expected, _lines = build_job_salary_calculation(job, month_value)
                if expected > 0:
                    virtual.append({
                        "id": f"job-{job.id}-{month_value.isoformat()}",
                        "salary_type": SalaryRecord.SalaryType.JOB,
                        "source_display": job.company_name,
                        "month": month_value.isoformat(),
                        "due_date": due_date.isoformat(),
                        "expected_amount": str(expected),
                        "received_amount": "0.00",
                        "pending_amount": str(expected),
                        "status": SalaryRecord.Status.PENDING,
                        "is_virtual": True,
                        "note": "Auto expected from active job setup. Record salary to mark received/partial.",
                    })

        monthly_projects = Project.objects.filter(
            owner=user,
            source_type=Project.SourceType.FREELANCING,
            status__in=[Project.Status.ACTIVE, Project.Status.IN_PROGRESS],
            billing_type=Project.BillingType.MONTHLY,
        ).select_related("freelance_source")
        sources_with_project_rows = set()
        for project in monthly_projects:
            if not project.freelance_source_id:
                continue
            sources_with_project_rows.add(project.freelance_source_id)
            start_date = project.start_date or project.created_at.date()
            cutoff = freelance_due_cutoff(project, today)
            if project.end_date:
                cutoff = min(cutoff, project.end_date)
            if cutoff < start_date:
                continue

            saved = SalaryRecord.objects.filter(owner=user, salary_type=SalaryRecord.SalaryType.FREELANCING, project=project)

            if not saved.exists():
                # No payment record yet: keep simple retainer behavior. If the
                # project was active at any point in a due month, show the full
                # monthly project amount. This keeps projects like SSPLive at
                # ₹5,000 for the first due month, as requested.
                end_limit = min(project.end_date.replace(day=1), month_limit) if project.end_date else month_limit
                for month_value in iter_month_starts(start_date, end_limit):
                    due_day = project.payment_due_day or project.freelance_source.payment_due_day
                    due_date = due_date_for_period(month_value, due_day)
                    if due_date > today:
                        continue
                    expected = money(project.billing_amount)
                    if expected > 0:
                        virtual.append({
                            "id": f"project-{project.id}-{month_value.isoformat()}",
                            "salary_type": SalaryRecord.SalaryType.FREELANCING,
                            "source_display": f"{project.freelance_source.name} · {project.name}",
                            "month": month_value.isoformat(),
                            "due_date": due_date.isoformat(),
                            "expected_amount": str(expected),
                            "received_amount": "0.00",
                            "pending_amount": str(expected),
                            "status": SalaryRecord.Status.PENDING,
                            "is_virtual": True,
                            "note": "Auto expected from active freelance project billing.",
                        })
                continue

            accrued, _lines = accrued_monthly_amount(project.billing_amount, start_date, cutoff)
            received_total = money(saved.aggregate(total=Sum("received_amount"))["total"] or Decimal("0.00"))
            visible_existing_pending = money(
                saved.exclude(status=SalaryRecord.Status.RECEIVED).aggregate(total=Sum("pending_amount"))["total"] or Decimal("0.00")
            )
            ledger_pending = money(max(accrued - received_total, Decimal("0.00")))
            extra_pending = money(max(ledger_pending - visible_existing_pending, Decimal("0.00")))
            if extra_pending > 0:
                due_day = project.payment_due_day or project.freelance_source.payment_due_day
                due_date = due_date_for_period(cutoff.replace(day=1), due_day)
                virtual.append({
                    "id": f"project-ledger-{project.id}-{cutoff.isoformat()}",
                    "salary_type": SalaryRecord.SalaryType.FREELANCING,
                    "source_display": f"{project.freelance_source.name} · {project.name}",
                    "month": cutoff.replace(day=1).isoformat(),
                    "period_start": start_date.isoformat(),
                    "period_end": cutoff.isoformat(),
                    "due_date": due_date.isoformat(),
                    "expected_amount": str(accrued),
                    "received_amount": str(received_total),
                    "pending_amount": str(extra_pending),
                    "status": SalaryRecord.Status.PENDING,
                    "is_virtual": True,
                    "note": "Running freelance ledger pending using actual month days. Existing partial rows are kept separately; this row shows remaining extra balance.",
                })

        for source in FreelanceSource.objects.filter(owner=user, status=FreelanceSource.Status.ACTIVE, default_billing_type=FreelanceSource.DefaultBillingType.MONTHLY):
            if source.id in sources_with_project_rows:
                continue
            start_date = source.created_at.date()
            for month_value in iter_month_starts(start_date, month_limit):
                due_date = due_date_for_period(month_value, source.payment_due_day)
                if due_date > today:
                    continue
                if (SalaryRecord.SalaryType.FREELANCING, source.id, None, month_value) in existing_keys:
                    continue
                expected, _lines, _period_start, _period_end = build_freelance_payment_calculation(user, freelance_source=source, month_value=month_value)
                if expected > 0:
                    virtual.append({
                        "id": f"source-{source.id}-{month_value.isoformat()}",
                        "salary_type": SalaryRecord.SalaryType.FREELANCING,
                        "source_display": source.name,
                        "month": month_value.isoformat(),
                        "due_date": due_date.isoformat(),
                        "expected_amount": str(expected),
                        "received_amount": "0.00",
                        "pending_amount": str(expected),
                        "status": SalaryRecord.Status.PENDING,
                        "is_virtual": True,
                        "note": "Auto expected from freelance source default monthly amount.",
                    })

        virtual.sort(key=lambda item: (item.get("due_date") or item.get("month"), item["source_display"]))
        return virtual

    def get(self, request):
        user = request.user
        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        month_start = today.replace(day=1)
        existing_qs = SalaryRecord.objects.filter(owner=user).exclude(status=SalaryRecord.Status.RECEIVED)
        existing_pending = existing_qs.aggregate(total=Sum("pending_amount"))["total"] or Decimal("0.00")
        virtual_pending = self._virtual_pending_salary_items(request, today)
        virtual_total = sum(Decimal(item["pending_amount"]) for item in virtual_pending)
        existing_pending_items = SalaryRecordSerializer(
            existing_qs.order_by("month")[:20], many=True, context={"request": request}
        ).data
        pending_items = (list(existing_pending_items) + virtual_pending)[:30]
        data = {
            "summary": {
                "tasks_due_today": Task.objects.filter(owner=user, date=today).exclude(status=Task.Status.COMPLETED).count(),
                "upcoming_meetings_24h": Meeting.objects.filter(owner=user, date__gte=today, date__lte=tomorrow, status=Meeting.Status.SCHEDULED).count(),
                "pending_salary_total": str(money(existing_pending + virtual_total)),
                "leave_days_used_this_month": LeaveRecord.objects.filter(owner=user, date__gte=month_start, date__lte=today).count(),
            },
            "today_tasks": TaskSerializer(
                Task.objects.filter(owner=user, date=today).order_by("priority", "start_time")[:10], many=True, context={"request": request}
            ).data,
            "upcoming_meetings": MeetingSerializer(
                Meeting.objects.filter(owner=user, date__gte=today, status=Meeting.Status.SCHEDULED).order_by("date", "start_time")[:10], many=True, context={"request": request}
            ).data,
            "pending_salaries": pending_items,
        }
        return response.Response(data)


class ReportsHubAPIView(APIView):
    def get(self, request):
        user = request.user
        return response.Response({
            "task_reports": Task.objects.filter(owner=user).count(),
            "meeting_reports": Meeting.objects.filter(owner=user).count(),
            "salary_reports": SalaryRecord.objects.filter(owner=user).count(),
            "leave_reports": LeaveRecord.objects.filter(owner=user).count(),
            "email_reports": EmailLog.objects.filter(owner=user).count(),
            "employee_reports": WorkMember.objects.filter(owner=user).count(),
        })


class TaskReportAPIView(APIView):
    def get(self, request):
        qs = Task.objects.filter(owner=request.user)
        return response.Response({
            "by_status": list(qs.values("status").annotate(count=Count("id")).order_by("status")),
            "by_priority": list(qs.values("priority").annotate(count=Count("id")).order_by("priority")),
            "by_type": list(qs.values("task_type").annotate(count=Count("id")).order_by("task_type")),
            "total_duration_minutes": qs.aggregate(total=Sum("duration_minutes"))["total"] or 0,
        })


class MeetingReportAPIView(APIView):
    def get(self, request):
        qs = Meeting.objects.filter(owner=request.user)
        return response.Response({
            "by_status": list(qs.values("status").annotate(count=Count("id")).order_by("status")),
            "by_type": list(qs.values("meeting_type").annotate(count=Count("id")).order_by("meeting_type")),
            "mom_filled": qs.exclude(mom="").count(),
            "mom_pending": qs.filter(mom="").count(),
        })


class SalaryReportAPIView(APIView):
    def get(self, request):
        qs = SalaryRecord.objects.filter(owner=request.user)
        totals = qs.aggregate(
            expected=Sum("expected_amount"),
            received=Sum("received_amount"),
            pending=Sum("pending_amount"),
        )
        return response.Response({
            "totals": {key: str(money(value or 0)) for key, value in totals.items()},
            "by_status": list(qs.values("status").annotate(count=Count("id"), pending=Sum("pending_amount")).order_by("status")),
        })


class LeaveReportAPIView(APIView):
    def get(self, request):
        qs = LeaveRecord.objects.filter(owner=request.user)
        return response.Response({
            "by_type": list(qs.values("leave_type").annotate(count=Count("id"), deduction=Sum("deduction_amount")).order_by("leave_type")),
            "paid_count": qs.filter(is_paid=True).count(),
            "unpaid_count": qs.filter(is_paid=False).count(),
        })


class EmailReportAPIView(APIView):
    def get(self, request):
        qs = EmailLog.objects.filter(owner=request.user)
        return response.Response({
            "by_status": list(qs.values("status").annotate(count=Count("id")).order_by("status")),
            "failed_count": qs.filter(status=EmailLog.Status.FAILED).count(),
            "sent_count": qs.filter(status=EmailLog.Status.SENT).count(),
        })


class EmployeeReportAPIView(APIView):
    def get(self, request):
        qs = WorkMember.objects.filter(owner=request.user)
        return response.Response({
            "by_type": list(qs.values("member_type").annotate(count=Count("id")).order_by("member_type")),
            "active_count": qs.filter(is_active=True).count(),
        })


class DesignationReportAPIView(APIView):
    def get(self, request):
        qs = DesignationHistory.objects.filter(owner=request.user).select_related("job")
        return response.Response(DesignationHistorySerializer(qs, many=True, context={"request": request}).data)


class OptionsAPIView(APIView):
    def get(self, request):
        return response.Response({
            "member_types": WorkMember.MemberType.choices,
            "job_statuses": Job.Status.choices,
            "source_platforms": FreelanceSource.Platform.choices,
            "source_default_billing_types": FreelanceSource.DefaultBillingType.choices,
            "project_source_types": Project.SourceType.choices,
            "project_statuses": Project.Status.choices,
            "project_billing_types": Project.BillingType.choices,
            "task_types": Task.TaskType.choices,
            "task_priorities": Task.Priority.choices,
            "task_statuses": Task.Status.choices,
            "meeting_types": Meeting.MeetingType.choices,
            "meeting_statuses": Meeting.Status.choices,
            "leave_types": LeaveRecord.LeaveType.choices,
            "salary_types": SalaryRecord.SalaryType.choices,
            "salary_statuses": SalaryRecord.Status.choices,
            "email_statuses": EmailLog.Status.choices,
            "email_variable_categories": EmailVariable.Category.choices,
        })
