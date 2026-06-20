from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


def default_working_days():
    # Python weekday numbers: Monday=0 ... Sunday=6. Default = Monday to Friday.
    return [0, 1, 2, 3, 4]


class OwnedModel(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="%(class)s_items")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class WorkMember(OwnedModel):
    class MemberType(models.TextChoices):
        SELF = "self", "Self"
        SENIOR = "senior", "Senior / Manager"
        EMPLOYEE = "employee", "Employee"
        CLIENT = "client", "Client"
        FREELANCER = "freelancer", "Freelancer"

    class SourceType(models.TextChoices):
        JOB = "job", "Job"
        FREELANCING = "freelancing", "Freelancing"
        MANUAL = "manual", "Manual / Other"

    name = models.CharField(max_length=150)
    member_type = models.CharField(max_length=20, choices=MemberType.choices, default=MemberType.EMPLOYEE)
    source_type = models.CharField(max_length=20, choices=SourceType.choices, default=SourceType.MANUAL)
    job = models.ForeignKey("Job", on_delete=models.SET_NULL, null=True, blank=True, related_name="members")
    freelance_source = models.ForeignKey("FreelanceSource", on_delete=models.SET_NULL, null=True, blank=True, related_name="members")
    designation = models.CharField(max_length=120, blank=True)
    company = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=25, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["owner", "member_type"])]

    def __str__(self):
        return self.name


class Job(OwnedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PROBATION = "probation", "Probation"
        RESIGNED = "resigned", "Resigned"
        INACTIVE = "inactive", "Inactive"

    company_name = models.CharField(max_length=180)
    job_title = models.CharField(max_length=180)
    current_designation = models.CharField(max_length=150, blank=True)
    reporting_manager = models.ForeignKey(WorkMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="reported_jobs")
    joining_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    current_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    salary_cycle_start_day = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(31)])
    salary_cycle_end_day = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1), MaxValueValidator(31)])
    salary_received_day = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(31)])
    working_days = models.JSONField(default=default_working_days, blank=True, help_text="Salary working weekdays. Monday=0 ... Sunday=6. Default is Monday-Friday.")
    probation_enabled = models.BooleanField(default=False)
    probation_start_date = models.DateField(null=True, blank=True)
    probation_end_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-joining_date", "company_name"]
        indexes = [models.Index(fields=["owner", "status"])]

    def __str__(self):
        return f"{self.company_name} - {self.job_title}"


class FreelanceSource(OwnedModel):
    class Platform(models.TextChoices):
        DIRECT = "direct", "Direct Client"
        UPWORK = "upwork", "Upwork"
        FIVERR = "fiverr", "Fiverr"
        TOPTAL = "toptal", "Toptal"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"
        CLOSED = "closed", "Closed"

    class DefaultBillingType(models.TextChoices):
        PROJECT_WISE = "project_wise", "Project Wise"
        MONTHLY = "monthly", "Monthly Retainer"
        MANUAL = "manual", "Manual"

    name = models.CharField(max_length=180)
    primary_contact_name = models.CharField(max_length=150, blank=True)
    platform = models.CharField(max_length=20, choices=Platform.choices, default=Platform.DIRECT)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=25, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    default_billing_type = models.CharField(max_length=20, choices=DefaultBillingType.choices, default=DefaultBillingType.PROJECT_WISE)
    default_monthly_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, null=True, blank=True)
    payment_due_day = models.PositiveSmallIntegerField(default=5, help_text="Expected payment day of next/current month for freelance billing.")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["owner", "status"])]

    def __str__(self):
        return self.name


class Project(OwnedModel):
    class SourceType(models.TextChoices):
        JOB = "job", "Job"
        FREELANCING = "freelancing", "Freelancing"
        MANUAL = "manual", "Manual"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        HOLD = "hold", "On Hold"
        CANCELLED = "cancelled", "Cancelled"

    class BillingType(models.TextChoices):
        FIXED_PROJECT = "fixed_project", "Fixed Project Amount"
        MONTHLY = "monthly", "Monthly Retainer"
        MANUAL = "manual", "Manual / Decide During Payment"

    name = models.CharField(max_length=180)
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, null=True, blank=True, related_name="projects")
    freelance_source = models.ForeignKey(FreelanceSource, on_delete=models.CASCADE, null=True, blank=True, related_name="projects")
    manual_source_name = models.CharField(max_length=180, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    budget = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    billing_type = models.CharField(max_length=20, choices=BillingType.choices, default=BillingType.MANUAL)
    billing_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="For fixed_project this is total project money; for monthly this is monthly amount.")
    payment_due_day = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Optional freelance payment due day. Falls back to source due day.")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["owner", "source_type", "status"])]

    @property
    def source_display(self):
        if self.job_id:
            return self.job.company_name
        if self.freelance_source_id:
            return self.freelance_source.name
        return self.manual_source_name or "Manual"

    def __str__(self):
        return self.name


class DesignationHistory(OwnedModel):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="designation_history")
    old_designation = models.CharField(max_length=150, blank=True)
    new_designation = models.CharField(max_length=150)
    effective_date = models.DateField()
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-effective_date", "-id"]

    def __str__(self):
        return f"{self.job} → {self.new_designation}"


class SalaryChangeHistory(OwnedModel):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="salary_changes")
    old_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    new_amount = models.DecimalField(max_digits=12, decimal_places=2)
    effective_date = models.DateField()
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-effective_date", "-id"]

    def __str__(self):
        return f"{self.job} salary {self.old_amount} → {self.new_amount}"


class SalaryRule(OwnedModel):
    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name="salary_rule")
    # Cycle/received are stored on Job. Kept here only for backward compatibility.
    cycle_start_day = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(31)])
    cycle_end_day = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1), MaxValueValidator(31)])
    received_day = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(31)])
    working_days = models.JSONField(default=default_working_days, blank=True, help_text="Salary working weekdays. Monday=0 ... Sunday=6.")
    working_days_per_month = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    is_default = models.BooleanField(default=False)

    def __str__(self):
        return f"Salary rule for {self.job}"


class EmailType(OwnedModel):
    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=80)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("owner", "code")]

    def __str__(self):
        return self.name




class EmailVariable(OwnedModel):
    class Category(models.TextChoices):
        COMMON = "common", "Common"
        PERSON = "person", "Person / Recipient"
        JOB = "job", "Job"
        PROJECT = "project", "Project"
        TASK = "task", "Task"
        MEETING = "meeting", "Meeting / MOM"
        LEAVE = "leave", "Leave"
        SALARY = "salary", "Salary / Payment"
        CUSTOM = "custom", "Custom"

    key = models.SlugField(max_length=80, help_text="Variable key used in templates, for example name or project_name.")
    label = models.CharField(max_length=120, help_text="User-friendly label shown in the template builder.")
    description = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.CUSTOM)
    default_value = models.CharField(max_length=250, blank=True, help_text="Optional fallback/example value shown while composing email.")
    is_required = models.BooleanField(default=False, help_text="Ask this value before sending when used in a template.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category", "label"]
        unique_together = [("owner", "key")]
        indexes = [models.Index(fields=["owner", "category", "is_active"])]

    @property
    def placeholder(self):
        return "{" + self.key + "}"

    def __str__(self):
        return f"{self.label} ({self.placeholder})"


class EmailTemplate(OwnedModel):
    email_type = models.ForeignKey(EmailType, on_delete=models.CASCADE, related_name="templates")
    name = models.CharField(max_length=120)
    subject = models.CharField(max_length=250)
    body = models.TextField()
    available_variables = models.JSONField(default=list, blank=True, help_text="Optional extra variable names to ask while sending this template.")
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["email_type__name", "name"]

    def __str__(self):
        return self.name


class SMTPConfig(OwnedModel):
    host = models.CharField(max_length=150)
    port = models.PositiveIntegerField(default=587)
    username = models.CharField(max_length=150)
    password = models.CharField(max_length=255)
    use_tls = models.BooleanField(default=True)
    use_ssl = models.BooleanField(default=False)
    from_email = models.EmailField()
    from_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_status = models.CharField(max_length=30, blank=True)
    last_test_message = models.TextField(blank=True)

    class Meta:
        verbose_name = "SMTP configuration"
        verbose_name_plural = "SMTP configurations"

    def __str__(self):
        return self.from_email


class Task(OwnedModel):
    class TaskType(models.TextChoices):
        JOB = "job", "Job"
        FREELANCING = "freelancing", "Freelancing"
        MANUAL = "manual", "Manual"

    class Priority(models.TextChoices):
        URGENT = "urgent", "Urgent"
        HIGH = "high", "High"
        NORMAL = "normal", "Normal"
        LOW = "low", "Low"

    class Status(models.TextChoices):
        TODO = "todo", "Todo"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        HOLD = "hold", "On Hold"

    task_type = models.CharField(max_length=20, choices=TaskType.choices)
    name = models.CharField(max_length=220)
    description = models.TextField(blank=True)
    job = models.ForeignKey(Job, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks")
    freelance_source = models.ForeignKey(FreelanceSource, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks")
    manual_source_name = models.CharField(max_length=180, blank=True)
    projects = models.ManyToManyField(Project, blank=True, related_name="tasks")
    assigned_by = models.ForeignKey(WorkMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_tasks")
    members = models.ManyToManyField(WorkMember, blank=True, related_name="tasks")
    date = models.DateField(default=timezone.localdate)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(default=0)
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TODO)
    reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [models.Index(fields=["owner", "task_type", "status", "date"])]

    @property
    def source_display(self):
        if self.job_id:
            return self.job.company_name
        if self.freelance_source_id:
            return self.freelance_source.name
        return self.manual_source_name or "Manual"

    def save(self, *args, **kwargs):
        if self.start_time and self.end_time:
            start = self.start_time.hour * 60 + self.start_time.minute
            end = self.end_time.hour * 60 + self.end_time.minute
            self.duration_minutes = max(end - start, 0)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Meeting(OwnedModel):
    class MeetingType(models.TextChoices):
        JOB = "job", "Job Meeting"
        FREELANCING = "freelancing", "Freelancing Meeting"
        GENERAL = "general", "General Meeting"
        MANUAL = "manual", "Manual Meeting"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    meeting_type = models.CharField(max_length=20, choices=MeetingType.choices)
    title = models.CharField(max_length=220)
    job = models.ForeignKey(Job, on_delete=models.SET_NULL, null=True, blank=True, related_name="meetings")
    freelance_source = models.ForeignKey(FreelanceSource, on_delete=models.SET_NULL, null=True, blank=True, related_name="meetings")
    manual_source_name = models.CharField(max_length=180, blank=True)
    projects = models.ManyToManyField(Project, blank=True, related_name="meetings")
    members = models.ManyToManyField(WorkMember, through="MeetingMember", blank=True, related_name="meetings")
    date = models.DateField(default=timezone.localdate)
    start_time = models.TimeField()
    end_time = models.TimeField(null=True, blank=True)
    location = models.CharField(max_length=250, blank=True)
    link = models.URLField(blank=True)
    agenda = models.TextField(blank=True)
    mom = models.TextField(blank=True)
    conclusion = models.TextField(blank=True)
    next_action = models.TextField(blank=True)
    action_items = models.JSONField(default=list, blank=True, help_text="Point-wise MOM actions: [{text, done}]. done defaults to false.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)

    class Meta:
        ordering = ["-date", "-start_time"]
        indexes = [models.Index(fields=["owner", "meeting_type", "status", "date"])]

    def __str__(self):
        return self.title


class MeetingMember(models.Model):
    class Role(models.TextChoices):
        HOST = "host", "Host"
        ATTENDEE = "attendee", "Attendee"
        CLIENT = "client", "Client"
        OPTIONAL = "optional", "Optional"

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE)
    member = models.ForeignKey(WorkMember, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.ATTENDEE)
    attended = models.BooleanField(default=False)

    class Meta:
        unique_together = [("meeting", "member")]

    def __str__(self):
        return f"{self.member} in {self.meeting}"


class LeaveRecord(OwnedModel):
    class LeaveType(models.TextChoices):
        FULL_DAY = "full_day", "Full Day"
        HALF_DAY = "half_day", "Half Day"
        LEAVE = "leave", "Leave"
        SICK_LEAVE = "sick_leave", "Sick Leave"
        PAID_LEAVE = "paid_leave", "Paid Leave"
        UNPAID_LEAVE = "unpaid_leave", "Unpaid Leave"
        WEEK_OFF = "week_off", "Week Off"
        HOLIDAY = "holiday", "Holiday"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="leave_records")
    leave_type = models.CharField(max_length=20, choices=LeaveType.choices)
    date = models.DateField()
    title = models.CharField(max_length=180)
    reason = models.TextField(blank=True)
    is_paid = models.BooleanField(default=True)
    deduction_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.APPROVED)
    notes = models.TextField(blank=True)
    send_email = models.BooleanField(default=False)
    email_template = models.ForeignKey(EmailTemplate, on_delete=models.SET_NULL, null=True, blank=True)
    email_recipients = models.ManyToManyField(WorkMember, blank=True, related_name="leave_emails")

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [models.Index(fields=["owner", "leave_type", "status", "date"])]

    def __str__(self):
        return f"{self.title} - {self.date}"


class SalaryRecord(OwnedModel):
    class SalaryType(models.TextChoices):
        JOB = "job", "Job Salary"
        FREELANCING = "freelancing", "Freelance Payment"
        MANUAL = "manual", "Manual Payment"

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PARTIAL = "partial", "Partial"
        PENDING = "pending", "Pending"
        CANCELLED = "cancelled", "Cancelled"

    salary_type = models.CharField(max_length=20, choices=SalaryType.choices)
    job = models.ForeignKey(Job, on_delete=models.SET_NULL, null=True, blank=True, related_name="salary_records")
    freelance_source = models.ForeignKey(FreelanceSource, on_delete=models.SET_NULL, null=True, blank=True, related_name="salary_records")
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name="salary_records")
    manual_source_name = models.CharField(max_length=180, blank=True)
    month = models.DateField(help_text="Use first day of selected salary/work month for grouping and reports.")
    period_start = models.DateField(null=True, blank=True, help_text="Actual earning period start date. Used mainly for freelance running ledger.")
    period_end = models.DateField(null=True, blank=True, help_text="Actual earning period end date. Used mainly for freelance running ledger.")
    expected_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    received_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    pending_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    received_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-month", "-id"]
        indexes = [models.Index(fields=["owner", "salary_type", "status", "month"])]

    @property
    def source_display(self):
        if self.job_id:
            return self.job.company_name
        if self.freelance_source_id:
            return self.freelance_source.name
        return self.manual_source_name or "Manual"

    def save(self, *args, **kwargs):
        self.pending_amount = max(self.expected_amount - self.received_amount, Decimal("0.00"))
        if self.received_amount <= 0:
            self.status = self.Status.PENDING
        elif self.pending_amount > 0:
            self.status = self.Status.PARTIAL
        else:
            self.status = self.Status.RECEIVED
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.source_display} - {self.month:%B %Y}"


class SalaryCalculationLine(models.Model):
    salary_record = models.ForeignKey(SalaryRecord, on_delete=models.CASCADE, related_name="calculation_lines")
    from_date = models.DateField()
    to_date = models.DateField()
    daily_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    days = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["from_date", "id"]

    def save(self, *args, **kwargs):
        if not self.amount:
            self.amount = self.daily_rate * self.days
        super().save(*args, **kwargs)


class EmailLog(OwnedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    email_type = models.ForeignKey(EmailType, on_delete=models.SET_NULL, null=True, blank=True, related_name="email_logs")
    template = models.ForeignKey(EmailTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="email_logs")
    recipients = models.ManyToManyField(WorkMember, blank=True, related_name="email_logs")
    to_emails = models.TextField(blank=True, help_text="Comma separated fallback emails.")
    cc = models.TextField(blank=True)
    bcc = models.TextField(blank=True)
    subject = models.CharField(max_length=250)
    body = models.TextField()
    variable_values = models.JSONField(default=dict, blank=True, help_text="Values used to replace {{variables}} in email subject/body.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    sent_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    related_module = models.CharField(max_length=50, blank=True)
    related_object_id = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "status", "created_at"])]

    def __str__(self):
        return self.subject


class Attachment(OwnedModel):
    class Module(models.TextChoices):
        TASK = "task", "Task"
        MEETING = "meeting", "Meeting"
        SALARY = "salary", "Salary"
        PROJECT = "project", "Project"
        EMAIL = "email", "Email"
        OTHER = "other", "Other"

    module = models.CharField(max_length=30, choices=Module.choices)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    title = models.CharField(max_length=180, blank=True)
    file = models.FileField(upload_to="attachments/%Y/%m/")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "module", "object_id"])]

    def __str__(self):
        return self.title or self.file.name



from django.db import models
from django.utils import timezone


def document_upload_path(instance, filename):
    owner_id = instance.owner_id or "unknown"
    today = timezone.now().date()
    return f"documents/{owner_id}/{today:%Y/%m/%d}/{filename}"


class Note(OwnedModel):
    class DataType(models.TextChoices):
        PLAIN = "plain", "Plain"
        POINTS = "points", "Point Wise"
        TABLE = "table", "Table"
        CHECKLIST = "checklist", "Checklist"
        CUSTOM = "custom", "Custom"

    class Status(models.TextChoices):
        WILL_START = "will_start", "Will Start"
        STARTED = "started", "Started"
        PARTIAL_DONE = "partial_done", "Partial Done"
        ALMOST_DONE = "almost_done", "Almost Done"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    label = models.CharField(max_length=180)
    description = models.TextField(blank=True)

    job = models.ForeignKey(
        Job,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
       related_name="note_records",
    )
    freelance_source = models.ForeignKey(
        FreelanceSource,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="note_records",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="note_records",
    )

    members = models.ManyToManyField(
        WorkMember,
        blank=True,
        related_name="note_records",
    )

    data_type = models.CharField(
        max_length=30,
        choices=DataType.choices,
        default=DataType.PLAIN,
    )
    data = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.WILL_START,
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["owner", "status", "data_type", "is_active"]),
            models.Index(fields=["owner", "job", "project"]),
        ]

    def __str__(self):
        return self.label


class DocumentCategory(OwnedModel):
    name = models.CharField(max_length=180)
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [["owner", "parent", "name"]]
        indexes = [
            models.Index(fields=["owner", "is_active"]),
            models.Index(fields=["owner", "parent"]),
        ]

    def __str__(self):
        return self.name


class Document(OwnedModel):
    name = models.CharField(max_length=180)
    description = models.TextField(blank=True)

    category = models.ForeignKey(
        DocumentCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )

    job = models.ForeignKey(
        Job,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    freelance_source = models.ForeignKey(
        FreelanceSource,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )

    document_file = models.FileField(
        upload_to=document_upload_path,
        null=True,
        blank=True,
    )

    external_url = models.URLField(blank=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["owner", "category", "is_active"]),
            models.Index(fields=["owner", "job", "project"]),
        ]

    def __str__(self):
        return self.name

    @property
    def document_path(self):
        if self.document_file:
            return self.document_file.name
        return ""

    @property
    def document_url(self):
        if self.document_file:
            try:
                return self.document_file.url
            except ValueError:
                return ""
        return self.external_url or ""
    

