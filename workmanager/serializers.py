from rest_framework import serializers

from .email_utils import extract_variables, render_template_text
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
)


class OwnerPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        request = self.context.get("request")
        base_queryset = super().get_queryset()
        if request and request.user and request.user.is_authenticated and hasattr(base_queryset.model, "owner"):
            return base_queryset.filter(owner=request.user)
        return base_queryset.none()




def ensure_project_source(projects, source_type, job=None, freelance_source=None):
    for project in projects or []:
        if source_type == Project.SourceType.JOB:
            if project.source_type != Project.SourceType.JOB or project.job_id != (job.id if job else None):
                raise serializers.ValidationError({"projects": "Selected project must belong to the selected job."})
        elif source_type == Project.SourceType.FREELANCING:
            if project.source_type != Project.SourceType.FREELANCING or project.freelance_source_id != (freelance_source.id if freelance_source else None):
                raise serializers.ValidationError({"projects": "Selected project must belong to the selected freelancing source."})
        elif source_type == Project.SourceType.MANUAL:
            if project.source_type != Project.SourceType.MANUAL:
                raise serializers.ValidationError({"projects": "Selected project must be a manual project."})


def validate_source_by_type(attrs, type_field, job_required=True, freelance_required=True, manual_required=False):
    source_type = attrs.get(type_field)
    job = attrs.get("job")
    freelance_source = attrs.get("freelance_source")
    manual_source_name = attrs.get("manual_source_name")

    if source_type == "job":
        if job_required and not job:
            raise serializers.ValidationError({"job": "Job is required for job type."})
        attrs["freelance_source"] = None
        attrs["manual_source_name"] = ""
    elif source_type == "freelancing":
        if freelance_required and not freelance_source:
            raise serializers.ValidationError({"freelance_source": "Freelancing source is required for freelancing type."})
        attrs["job"] = None
        attrs["manual_source_name"] = ""
    elif source_type in ("manual", "general"):
        attrs["job"] = None
        attrs["freelance_source"] = None
        if manual_required and not manual_source_name:
            raise serializers.ValidationError({"manual_source_name": "Manual source name is required."})
    return attrs

class WorkMemberSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source="member_type", required=False)
    job = OwnerPrimaryKeyRelatedField(queryset=Job.objects.all(), required=False, allow_null=True)
    freelance_source = OwnerPrimaryKeyRelatedField(queryset=FreelanceSource.objects.all(), required=False, allow_null=True)
    source_display = serializers.SerializerMethodField()

    class Meta:
        model = WorkMember
        fields = [
            "id", "name", "type", "member_type", "source_type", "job", "freelance_source", "source_display",
            "designation", "company", "email", "phone", "is_active", "notes", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "source_display", "created_at", "updated_at"]

    def get_source_display(self, obj):
        if obj.source_type == WorkMember.SourceType.JOB and obj.job_id:
            return obj.job.company_name
        if obj.source_type == WorkMember.SourceType.FREELANCING and obj.freelance_source_id:
            return obj.freelance_source.name
        return obj.company or "Manual / Other"

    def validate(self, attrs):
        source_type = attrs.get("source_type", getattr(self.instance, "source_type", WorkMember.SourceType.MANUAL))
        attrs["source_type"] = source_type
        if source_type == WorkMember.SourceType.JOB:
            if not attrs.get("job", getattr(self.instance, "job", None)):
                raise serializers.ValidationError({"job": "Job is required when member belongs to Job."})
            attrs["freelance_source"] = None
            attrs["company"] = ""
        elif source_type == WorkMember.SourceType.FREELANCING:
            if not attrs.get("freelance_source", getattr(self.instance, "freelance_source", None)):
                raise serializers.ValidationError({"freelance_source": "Freelancing Source is required when member belongs to Freelancing."})
            attrs["job"] = None
            attrs["company"] = ""
        else:
            attrs["job"] = None
            attrs["freelance_source"] = None
        return attrs


class JobSerializer(serializers.ModelSerializer):
    reporting_manager = OwnerPrimaryKeyRelatedField(queryset=WorkMember.objects.all(), required=False, allow_null=True)
    reporting_manager_name = serializers.CharField(source="reporting_manager.name", read_only=True)
    projects_count = serializers.IntegerField(source="projects.count", read_only=True)

    class Meta:
        model = Job
        fields = [
            "id", "company_name", "job_title", "current_designation", "reporting_manager", "reporting_manager_name",
            "joining_date", "status", "current_salary", "salary_cycle_start_day", "salary_cycle_end_day",
            "salary_received_day", "working_days", "probation_enabled", "probation_start_date", "probation_end_date", "notes",
            "projects_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "projects_count", "created_at", "updated_at"]


class FreelanceSourceSerializer(serializers.ModelSerializer):
    projects_count = serializers.IntegerField(source="projects.count", read_only=True)

    class Meta:
        model = FreelanceSource
        fields = [
            "id", "name", "primary_contact_name", "platform", "email", "phone", "status",
            "default_billing_type", "default_monthly_amount", "payment_due_day",
            "notes", "projects_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "projects_count", "created_at", "updated_at"]


class ProjectSerializer(serializers.ModelSerializer):
    job = OwnerPrimaryKeyRelatedField(queryset=Job.objects.all(), required=False, allow_null=True)
    freelance_source = OwnerPrimaryKeyRelatedField(queryset=FreelanceSource.objects.all(), required=False, allow_null=True)
    source_display = serializers.CharField(read_only=True)
    tasks_count = serializers.IntegerField(source="tasks.count", read_only=True)

    class Meta:
        model = Project
        fields = [
            "id", "name", "source_type", "job", "freelance_source", "manual_source_name", "source_display",
            "status", "start_date", "end_date", "budget", "billing_type", "billing_amount", "payment_due_day",
            "notes", "tasks_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "source_display", "tasks_count", "created_at", "updated_at"]

    def validate(self, attrs):
        source_type = attrs.get("source_type", getattr(self.instance, "source_type", None))
        attrs["source_type"] = source_type
        if source_type == Project.SourceType.JOB:
            job = attrs.get("job", getattr(self.instance, "job", None))
            if not job:
                raise serializers.ValidationError({"job": "Job is required when Source Type is Job."})
            attrs["freelance_source"] = None
            attrs["manual_source_name"] = ""
            attrs["billing_type"] = Project.BillingType.MANUAL
            attrs["billing_amount"] = 0
            attrs["payment_due_day"] = None
        elif source_type == Project.SourceType.FREELANCING:
            if not attrs.get("freelance_source", getattr(self.instance, "freelance_source", None)):
                raise serializers.ValidationError({"freelance_source": "Freelancing Source is required when Source Type is Freelancing."})
            attrs["job"] = None
            attrs["manual_source_name"] = ""
        elif source_type == Project.SourceType.MANUAL:
            if not attrs.get("manual_source_name", getattr(self.instance, "manual_source_name", "")):
                raise serializers.ValidationError({"manual_source_name": "Manual Source is required when Source Type is Manual."})
            attrs["job"] = None
            attrs["freelance_source"] = None
            attrs["billing_type"] = Project.BillingType.MANUAL
            attrs["billing_amount"] = 0
            attrs["payment_due_day"] = None
        return attrs


class DesignationHistorySerializer(serializers.ModelSerializer):
    job = OwnerPrimaryKeyRelatedField(queryset=Job.objects.all())
    job_name = serializers.CharField(source="job.company_name", read_only=True)

    class Meta:
        model = DesignationHistory
        fields = ["id", "job", "job_name", "old_designation", "new_designation", "effective_date", "reason", "created_at", "updated_at"]
        read_only_fields = ["id", "job_name", "created_at", "updated_at"]


class SalaryChangeHistorySerializer(serializers.ModelSerializer):
    job = OwnerPrimaryKeyRelatedField(queryset=Job.objects.all())
    job_name = serializers.CharField(source="job.company_name", read_only=True)

    class Meta:
        model = SalaryChangeHistory
        fields = ["id", "job", "job_name", "old_amount", "new_amount", "effective_date", "reason", "created_at", "updated_at"]
        read_only_fields = ["id", "job_name", "created_at", "updated_at"]


class SalaryRuleSerializer(serializers.ModelSerializer):
    job = OwnerPrimaryKeyRelatedField(queryset=Job.objects.all())
    job_name = serializers.CharField(source="job.company_name", read_only=True)

    class Meta:
        model = SalaryRule
        fields = ["id", "job", "job_name", "working_days", "cycle_start_day", "cycle_end_day", "received_day", "working_days_per_month", "is_default", "created_at", "updated_at"]
        read_only_fields = ["id", "job_name", "cycle_start_day", "cycle_end_day", "received_day", "working_days_per_month", "created_at", "updated_at"]

    def validate(self, attrs):
        job = attrs.get("job", getattr(self.instance, "job", None))
        if job:
            attrs["cycle_start_day"] = job.salary_cycle_start_day
            attrs["cycle_end_day"] = job.salary_cycle_end_day
            attrs["received_day"] = job.salary_received_day
            attrs.setdefault("working_days", job.working_days)
        return attrs


class EmailTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmailType
        fields = ["id", "name", "code", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]




class EmailVariableSerializer(serializers.ModelSerializer):
    placeholder = serializers.CharField(read_only=True)

    class Meta:
        model = EmailVariable
        fields = [
            "id", "key", "label", "description", "category", "default_value",
            "is_required", "is_active", "placeholder", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "placeholder", "created_at", "updated_at"]

    def validate_key(self, value):
        value = (value or "").strip().lower().replace("-", "_")
        if not value:
            raise serializers.ValidationError("Variable key is required.")
        if not value.replace("_", "").isalnum() or value[0].isdigit():
            raise serializers.ValidationError("Use snake_case letters, numbers and underscores. It cannot start with a number.")
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        owner = getattr(request, "user", None)
        key = attrs.get("key", getattr(self.instance, "key", None))
        if owner and key:
            qs = EmailVariable.objects.filter(owner=owner, key=key)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"key": "This variable key already exists."})
        return attrs


class EmailTemplateSerializer(serializers.ModelSerializer):
    email_type = OwnerPrimaryKeyRelatedField(queryset=EmailType.objects.all())
    email_type_name = serializers.CharField(source="email_type.name", read_only=True)
    template_variables = serializers.SerializerMethodField()
    variable_details = serializers.SerializerMethodField()

    class Meta:
        model = EmailTemplate
        fields = [
            "id", "email_type", "email_type_name", "name", "subject", "body",
            "available_variables", "template_variables", "variable_details", "is_default", "is_active", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "email_type_name", "template_variables", "variable_details", "created_at", "updated_at"]

    def get_template_variables(self, obj):
        return extract_variables(obj.subject, obj.body, extra=obj.available_variables)

    def get_variable_details(self, obj):
        request = self.context.get("request")
        keys = self.get_template_variables(obj)
        variables = {}
        if request and request.user and request.user.is_authenticated and keys:
            variables = {v.key: v for v in EmailVariable.objects.filter(owner=request.user, key__in=keys)}
        result = []
        for key in keys:
            variable = variables.get(key)
            if variable:
                result.append(EmailVariableSerializer(variable, context=self.context).data)
            else:
                result.append({
                    "id": None, "key": key, "label": key.replace("_", " ").title(),
                    "description": "", "category": "custom", "default_value": "",
                    "is_required": False, "is_active": True, "placeholder": "{" + key + "}",
                })
        return result


class SMTPConfigSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    has_password = serializers.SerializerMethodField()

    class Meta:
        model = SMTPConfig
        fields = [
            "id", "host", "port", "username", "password", "has_password", "use_tls", "use_ssl", "from_email", "from_name",
            "is_active", "last_tested_at", "last_test_status", "last_test_message", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "has_password", "last_tested_at", "last_test_status", "last_test_message", "created_at", "updated_at"]

    def get_has_password(self, obj):
        return bool(obj.password)


class TaskSerializer(serializers.ModelSerializer):
    job = OwnerPrimaryKeyRelatedField(queryset=Job.objects.all(), required=False, allow_null=True)
    freelance_source = OwnerPrimaryKeyRelatedField(queryset=FreelanceSource.objects.all(), required=False, allow_null=True)
    projects = OwnerPrimaryKeyRelatedField(queryset=Project.objects.all(), many=True, required=False)
    members = OwnerPrimaryKeyRelatedField(queryset=WorkMember.objects.all(), many=True, required=False)
    assigned_by = OwnerPrimaryKeyRelatedField(queryset=WorkMember.objects.all(), required=False, allow_null=True)
    source_display = serializers.CharField(read_only=True)
    assigned_by_name = serializers.CharField(source="assigned_by.name", read_only=True)
    project_names = serializers.SerializerMethodField()
    member_names = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id", "task_type", "name", "description", "job", "freelance_source", "manual_source_name", "source_display",
            "projects", "project_names", "assigned_by", "assigned_by_name", "members", "member_names", "date", "start_time",
            "end_time", "duration_minutes", "priority", "status", "reason", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "source_display", "project_names", "assigned_by_name", "member_names", "duration_minutes", "created_at", "updated_at"]

    def get_project_names(self, obj):
        return list(obj.projects.values_list("name", flat=True))

    def get_member_names(self, obj):
        return list(obj.members.values_list("name", flat=True))

    def validate(self, attrs):
        task_type = attrs.get("task_type", getattr(self.instance, "task_type", None))
        attrs["task_type"] = task_type
        if self.instance:
            attrs.setdefault("job", self.instance.job)
            attrs.setdefault("freelance_source", self.instance.freelance_source)
            attrs.setdefault("manual_source_name", self.instance.manual_source_name)
        attrs = validate_source_by_type(attrs, "task_type", job_required=(task_type == Task.TaskType.JOB), freelance_required=(task_type == Task.TaskType.FREELANCING), manual_required=False)
        projects = attrs.get("projects", list(self.instance.projects.all()) if self.instance else [])
        ensure_project_source(projects, task_type, attrs.get("job"), attrs.get("freelance_source"))
        return attrs


def normalize_action_items(value):
    """Normalize MOM action point JSON into [{text: str, done: bool}].

    Older payloads used {text, status}; they are accepted and converted so
    existing data keeps working while the UI now stores a simple done flag.
    """
    if value in (None, ""):
        return []
    if isinstance(value, str):
        rows = [line.strip() for line in value.splitlines() if line.strip()]
        return [{"text": row, "done": False} for row in rows]
    if not isinstance(value, list):
        raise serializers.ValidationError("Action items must be a list.")
    rows = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            done = False
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("label") or item.get("title") or "").strip()
            if "done" in item:
                done = bool(item.get("done"))
            else:
                done = str(item.get("status") or "").lower() in {"done", "completed", "closed"}
        else:
            continue
        if text:
            rows.append({"text": text, "done": done})
    return rows


class MeetingMemberSerializer(serializers.ModelSerializer):
    member = OwnerPrimaryKeyRelatedField(queryset=WorkMember.objects.all())
    member_name = serializers.CharField(source="member.name", read_only=True)

    class Meta:
        model = MeetingMember
        fields = ["id", "member", "member_name", "role", "attended"]
        read_only_fields = ["id", "member_name"]


class MeetingSerializer(serializers.ModelSerializer):
    job = OwnerPrimaryKeyRelatedField(queryset=Job.objects.all(), required=False, allow_null=True)
    freelance_source = OwnerPrimaryKeyRelatedField(queryset=FreelanceSource.objects.all(), required=False, allow_null=True)
    projects = OwnerPrimaryKeyRelatedField(queryset=Project.objects.all(), many=True, required=False)
    member_roles = MeetingMemberSerializer(source="meetingmember_set", many=True, required=False)
    project_names = serializers.SerializerMethodField()

    class Meta:
        model = Meeting
        fields = [
            "id", "meeting_type", "title", "job", "freelance_source", "manual_source_name", "projects", "project_names",
            "member_roles", "date", "start_time", "end_time", "location", "link", "agenda", "mom", "conclusion",
            "next_action", "action_items", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "project_names", "created_at", "updated_at"]

    def get_project_names(self, obj):
        return list(obj.projects.values_list("name", flat=True))

    def validate_action_items(self, value):
        return normalize_action_items(value)

    def validate(self, attrs):
        meeting_type = attrs.get("meeting_type", getattr(self.instance, "meeting_type", None))
        attrs["meeting_type"] = meeting_type
        if self.instance:
            attrs.setdefault("job", self.instance.job)
            attrs.setdefault("freelance_source", self.instance.freelance_source)
            attrs.setdefault("manual_source_name", self.instance.manual_source_name)
        attrs = validate_source_by_type(attrs, "meeting_type", job_required=(meeting_type == Meeting.MeetingType.JOB), freelance_required=(meeting_type == Meeting.MeetingType.FREELANCING), manual_required=False)
        projects = attrs.get("projects", list(self.instance.projects.all()) if self.instance else [])
        if meeting_type != Meeting.MeetingType.GENERAL:
            ensure_project_source(projects, meeting_type, attrs.get("job"), attrs.get("freelance_source"))
        return attrs

    def create(self, validated_data):
        member_roles = validated_data.pop("meetingmember_set", [])
        projects = validated_data.pop("projects", [])
        meeting = Meeting.objects.create(**validated_data)
        meeting.projects.set(projects)
        for item in member_roles:
            MeetingMember.objects.create(meeting=meeting, **item)
        return meeting

    def update(self, instance, validated_data):
        member_roles = validated_data.pop("meetingmember_set", None)
        projects = validated_data.pop("projects", None)
        instance = super().update(instance, validated_data)
        if projects is not None:
            instance.projects.set(projects)
        if member_roles is not None:
            instance.meetingmember_set.all().delete()
            for item in member_roles:
                MeetingMember.objects.create(meeting=instance, **item)
        return instance


class LeaveRecordSerializer(serializers.ModelSerializer):
    job = OwnerPrimaryKeyRelatedField(queryset=Job.objects.all())
    email_template = OwnerPrimaryKeyRelatedField(queryset=EmailTemplate.objects.all(), required=False, allow_null=True)
    email_recipients = OwnerPrimaryKeyRelatedField(queryset=WorkMember.objects.all(), many=True, required=False)
    company_name = serializers.CharField(source="job.company_name", read_only=True)

    class Meta:
        model = LeaveRecord
        fields = [
            "id", "job", "company_name", "leave_type", "date", "title", "reason", "is_paid", "deduction_amount",
            "status", "notes", "send_email", "email_template", "email_recipients", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company_name", "created_at", "updated_at"]


class SalaryCalculationLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalaryCalculationLine
        fields = ["id", "from_date", "to_date", "daily_rate", "days", "amount", "notes"]
        read_only_fields = ["id"]


class SalaryRecordSerializer(serializers.ModelSerializer):
    job = OwnerPrimaryKeyRelatedField(queryset=Job.objects.all(), required=False, allow_null=True)
    freelance_source = OwnerPrimaryKeyRelatedField(queryset=FreelanceSource.objects.all(), required=False, allow_null=True)
    project = OwnerPrimaryKeyRelatedField(queryset=Project.objects.all(), required=False, allow_null=True)
    calculation_lines = SalaryCalculationLineSerializer(many=True, required=False)
    source_display = serializers.CharField(read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)

    class Meta:
        model = SalaryRecord
        fields = [
            "id", "salary_type", "job", "freelance_source", "project", "project_name", "manual_source_name", "source_display", "month",
            "period_start", "period_end", "expected_amount", "received_amount", "pending_amount", "received_date", "status", "notes", "calculation_lines",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "source_display", "project_name", "pending_amount", "status", "created_at", "updated_at"]

    def validate(self, attrs):
        month = attrs.get("month")
        if month:
            attrs["month"] = month.replace(day=1)
        salary_type = attrs.get("salary_type", getattr(self.instance, "salary_type", None))
        job = attrs.get("job", getattr(self.instance, "job", None))
        freelance_source = attrs.get("freelance_source", getattr(self.instance, "freelance_source", None))
        project = attrs.get("project", getattr(self.instance, "project", None))
        if salary_type == SalaryRecord.SalaryType.JOB:
            if not job:
                raise serializers.ValidationError({"job": "Job is required for job salary."})
            attrs["freelance_source"] = None
            attrs["project"] = None
            attrs["manual_source_name"] = ""
        elif salary_type == SalaryRecord.SalaryType.FREELANCING:
            if not freelance_source and not project:
                raise serializers.ValidationError({"freelance_source": "Freelance source or project is required for freelance payment."})
            if project:
                if project.source_type != Project.SourceType.FREELANCING:
                    raise serializers.ValidationError({"project": "Project must be a freelancing project."})
                if freelance_source and project.freelance_source_id != freelance_source.id:
                    raise serializers.ValidationError({"project": "Project does not belong to selected freelancing source."})
                attrs["freelance_source"] = project.freelance_source
            attrs["job"] = None
            attrs["manual_source_name"] = ""
        elif salary_type == SalaryRecord.SalaryType.MANUAL:
            attrs["job"] = None
            attrs["freelance_source"] = None
            attrs["project"] = None
        return attrs

    def create(self, validated_data):
        lines = validated_data.pop("calculation_lines", [])
        record = SalaryRecord.objects.create(**validated_data)
        for line in lines:
            SalaryCalculationLine.objects.create(salary_record=record, **line)
        return record

    def update(self, instance, validated_data):
        lines = validated_data.pop("calculation_lines", None)
        instance = super().update(instance, validated_data)
        if lines is not None:
            instance.calculation_lines.all().delete()
            for line in lines:
                SalaryCalculationLine.objects.create(salary_record=instance, **line)
        return instance


class EmailLogSerializer(serializers.ModelSerializer):
    email_type = OwnerPrimaryKeyRelatedField(queryset=EmailType.objects.all(), required=False, allow_null=True)
    template = OwnerPrimaryKeyRelatedField(queryset=EmailTemplate.objects.all(), required=False, allow_null=True)
    recipients = OwnerPrimaryKeyRelatedField(queryset=WorkMember.objects.all(), many=True, required=False)
    email_type_name = serializers.CharField(source="email_type.name", read_only=True)
    recipient_names = serializers.SerializerMethodField()
    template_variables = serializers.SerializerMethodField()
    preview_subject = serializers.SerializerMethodField()
    preview_body = serializers.SerializerMethodField()

    class Meta:
        model = EmailLog
        fields = [
            "id", "email_type", "email_type_name", "template", "template_variables", "recipients", "recipient_names", "to_emails", "cc", "bcc",
            "subject", "body", "variable_values", "preview_subject", "preview_body", "status", "sent_at", "error_message",
            "related_module", "related_object_id", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "email_type_name", "recipient_names", "template_variables", "preview_subject", "preview_body", "sent_at", "error_message", "created_at", "updated_at"]

    def get_recipient_names(self, obj):
        return list(obj.recipients.values_list("name", flat=True))

    def get_template_variables(self, obj):
        if obj.template_id:
            return extract_variables(obj.template.subject, obj.template.body, extra=obj.template.available_variables)
        return extract_variables(obj.subject, obj.body)

    def get_preview_subject(self, obj):
        return render_template_text(obj.subject, obj.variable_values)

    def get_preview_body(self, obj):
        return render_template_text(obj.body, obj.variable_values)

    def validate(self, attrs):
        template = attrs.get("template", getattr(self.instance, "template", None))
        if template:
            attrs.setdefault("email_type", template.email_type)
            if not attrs.get("subject") and not getattr(self.instance, "subject", ""):
                attrs["subject"] = template.subject
            if not attrs.get("body") and not getattr(self.instance, "body", ""):
                attrs["body"] = template.body
        return attrs


class AttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attachment
        fields = ["id", "module", "object_id", "title", "file", "notes", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]
