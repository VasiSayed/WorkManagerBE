"""FindIt: permanent QR labels on boxes and bags, and what is inside them.

* A Household is shared by family members; everything below belongs to one household.
* Every Asset gets a permanent UUID (encoded in its QR label) and a permanent reference
  number "VHS<n>" from one global sequence that never goes backwards or reuses a number.
* Assets are never deleted: they are archived, or reset (details cleared, identity kept).
* Every change is written to AssetHistory.
"""

import os
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

class PrivateStorage(FileSystemStorage):
    """Photos of what's in the boxes are private: kept outside MEDIA_ROOT (which is served publicly)
    and only sent through short-lived signed links. Folder: settings.FINDIT_PRIVATE_ROOT."""

    @property
    def base_location(self):
        return getattr(settings, "FINDIT_PRIVATE_ROOT", settings.BASE_DIR / "private_media")

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        return None


def private_storage():
    return PrivateStorage()


class Household(models.Model):
    name = models.CharField(max_length=120, default="My Home")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class HouseholdMember(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MEMBER = "member", "Member"

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="members")
    # One household per person keeps the app simple: whatever you scan, you see your family's boxes.
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="findit_membership")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["joined_at"]

    def __str__(self):
        return f"{self.user} in {self.household}"


class ReferenceSequence(models.Model):
    """Single row holding the last VHS number handed out. Only ever increases."""

    key = models.CharField(max_length=30, primary_key=True)
    last_value = models.BigIntegerField(default=0)


class Purpose(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="purposes")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    image = models.ImageField(storage=private_storage, upload_to="findit/purposes/", null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["household", "name"], name="findit_unique_purpose_name")]

    def __str__(self):
        return self.name


class Asset(models.Model):
    class Status(models.TextChoices):
        EMPTY = "empty", "Empty"
        DRAFT = "draft", "Draft"
        READY = "ready", "Ready"
        ARCHIVED = "archived", "Archived"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    number = models.BigIntegerField(unique=True, editable=False)
    reference = models.CharField(max_length=40, unique=True, editable=False)
    household = models.ForeignKey(Household, on_delete=models.PROTECT, related_name="assets")
    name = models.CharField(max_length=200, blank=True)
    purpose = models.ForeignKey(Purpose, on_delete=models.SET_NULL, null=True, blank=True, related_name="assets")
    location = models.CharField(max_length=300, blank=True)
    stored_inside = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="contents"
    )
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True, validators=[MinValueValidator(-90), MaxValueValidator(90)]
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True, validators=[MinValueValidator(-180), MaxValueValidator(180)]
    )
    description = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.EMPTY, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-number"]
        indexes = [models.Index(fields=["household", "status"], name="findit_asset_hh_status_idx")]

    def __str__(self):
        return f"{self.reference} {self.name}".strip()


def asset_image_path(instance, filename):
    return f"findit/{instance.asset.household_id}/{instance.asset.uuid.hex}/{uuid.uuid4().hex}.jpg"


def asset_thumb_path(instance, filename):
    return f"findit/{instance.asset.household_id}/{instance.asset.uuid.hex}/thumb_{uuid.uuid4().hex}.jpg"


class AssetImage(models.Model):
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(storage=private_storage, upload_to=asset_image_path)
    thumbnail = models.ImageField(storage=private_storage, upload_to=asset_thumb_path, null=True, blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class AssetHistory(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Details changed"
        STATUS = "status", "Status changed"
        IMAGE_ADDED = "image_added", "Photo added"
        IMAGE_REMOVED = "image_removed", "Photo removed"
        ARCHIVED = "archived", "Archived"
        RESTORED = "restored", "Restored"
        RESET = "reset", "Reset"

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="history")
    # Rows written by one save share a change_id, so the app shows them as one entry.
    change_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    field = models.CharField(max_length=40, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
