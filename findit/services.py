"""FindIt business rules: numbering, bulk creation, status, edits with history, photos, reset, search."""

import io
import re
import uuid
from decimal import Decimal

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, F, Q
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import Asset, AssetHistory, AssetImage, Household, HouseholdMember, Purpose, ReferenceSequence

REF_PREFIX = "VHS"
SEQUENCE_KEY = "asset"
MAX_BULK = 500
MAX_IMAGES_PER_ASSET = 30
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
IMAGE_MAX_SIDE = 1600
THUMB_MAX_SIDE = 360
MAX_NESTING = 20

# Field -> label shown in history.
TRACKED_FIELDS = {
    "name": "Name",
    "purpose": "Purpose",
    "location": "Location",
    "stored_inside": "Stored inside",
    "latitude": "Latitude",
    "longitude": "Longitude",
    "description": "Description",
}


class FindItError(Exception):
    def __init__(self, message, code="invalid", status=400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


# ---------------------------------------------------------------------------
# Household
# ---------------------------------------------------------------------------


def household_for(user, create=True):
    """The user's household; the first visit creates one with the user as owner."""
    membership = HouseholdMember.objects.select_related("household").filter(user=user).first()
    if membership:
        return membership.household
    if not create:
        return None
    with transaction.atomic():
        household = Household.objects.create(name="My Home", created_by=user)
        HouseholdMember.objects.create(household=household, user=user, role=HouseholdMember.Role.OWNER)
    return household


def is_owner(user, household):
    return HouseholdMember.objects.filter(household=household, user=user, role=HouseholdMember.Role.OWNER).exists()


# ---------------------------------------------------------------------------
# Reference numbers and bulk creation
# ---------------------------------------------------------------------------


def reserve_numbers(count):
    """Reserve `count` consecutive numbers. Must run inside a transaction.

    The UPDATE ... SET last_value = last_value + n is atomic in the database, so two requests can
    never receive the same numbers; numbers are never handed back, even if a box is reset or archived.
    """
    ReferenceSequence.objects.get_or_create(key=SEQUENCE_KEY, defaults={"last_value": _highest_number()})
    ReferenceSequence.objects.filter(key=SEQUENCE_KEY).update(last_value=F("last_value") + count)
    last = ReferenceSequence.objects.get(key=SEQUENCE_KEY).last_value
    return list(range(last - count + 1, last + 1))


def _highest_number():
    top = Asset.objects.order_by("-number").values_list("number", flat=True).first()
    return top or 0


def bulk_create_assets(household, count, user):
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise FindItError("Enter how many labels to create.", "invalid_count")
    if count < 1 or count > MAX_BULK:
        raise FindItError(f"Create between 1 and {MAX_BULK} at a time.", "invalid_count")
    with transaction.atomic():
        numbers = reserve_numbers(count)
        assets = [
            Asset(
                uuid=uuid.uuid4(), number=n, reference=f"{REF_PREFIX}{n}", household=household,
                created_by=user, updated_by=user,
            )
            for n in numbers
        ]
        Asset.objects.bulk_create(assets)
        created = list(Asset.objects.filter(number__in=numbers).order_by("number"))
        batch = uuid.uuid4()
        AssetHistory.objects.bulk_create(
            [AssetHistory(asset=a, change_id=batch, action=AssetHistory.Action.CREATED, user=user,
                          new_value=a.reference) for a in created]
        )
    return created


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def has_details(asset):
    return bool(
        asset.name.strip() or asset.purpose_id or asset.location.strip() or asset.description.strip()
        or asset.stored_inside_id or asset.latitude is not None
        # Query directly: asset.images may be a prefetched (stale) list.
        or AssetImage.objects.filter(asset_id=asset.pk).exists()
    )


def _auto_status(asset):
    """Empty <-> Draft follows the content; Ready and Archived are only changed on purpose."""
    if asset.status == Asset.Status.ARCHIVED:
        return asset.status
    if not has_details(asset):
        return Asset.Status.EMPTY
    if asset.status == Asset.Status.EMPTY:
        return Asset.Status.DRAFT
    return asset.status


def _log(asset, user, action, change_id, field="", old="", new=""):
    return AssetHistory(asset=asset, change_id=change_id, action=action, field=field,
                        old_value=old or "", new_value=new or "", user=user)


def _sync_status(asset, user, change_id, rows):
    new_status = _auto_status(asset)
    if new_status != asset.status:
        rows.append(_log(asset, user, AssetHistory.Action.STATUS, change_id, "status",
                         asset.get_status_display(), Asset.Status(new_status).label))
        asset.status = new_status


def set_status(asset, status, user):
    if status not in Asset.Status.values:
        raise FindItError("Unknown status.", "invalid_status")
    if status == asset.status:
        return asset
    if status == Asset.Status.READY and not asset.name.strip():
        raise FindItError("Give the box a name before marking it Ready.", "name_required")
    if status in (Asset.Status.DRAFT, Asset.Status.EMPTY) and not has_details(asset):
        status = Asset.Status.EMPTY
    if status == Asset.Status.EMPTY and has_details(asset):
        raise FindItError("This box has details. Use Reset to empty it.", "has_details")
    change_id = uuid.uuid4()
    if status == Asset.Status.ARCHIVED:
        action = AssetHistory.Action.ARCHIVED
    elif asset.status == Asset.Status.ARCHIVED:
        action = AssetHistory.Action.RESTORED
    else:
        action = AssetHistory.Action.STATUS
    old_label = asset.get_status_display()
    asset.status = status
    asset.updated_by = user
    with transaction.atomic():
        asset.save(update_fields=["status", "updated_by", "updated_at"])
        _log(asset, user, action, change_id, "status", old_label, asset.get_status_display()).save()
    return asset


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def _display(field, value):
    if value is None or value == "":
        return ""
    if field == "purpose":
        return value.name
    if field == "stored_inside":
        return f"{value.reference} {value.name}".strip()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def storage_chain(asset):
    """Containers this asset is inside, nearest first (stops on bad data instead of looping)."""
    chain, seen, current = [], {asset.pk}, asset.stored_inside
    while current is not None and current.pk not in seen and len(chain) < MAX_NESTING:
        chain.append(current)
        seen.add(current.pk)
        current = current.stored_inside
    return chain


def _check_container(asset, container):
    if container is None:
        return
    if container.household_id != asset.household_id:
        raise FindItError("That container is not in your household.", "invalid_container")
    if container.pk == asset.pk:
        raise FindItError("A box cannot be stored inside itself.", "circular")
    node, depth = container, 0
    while node is not None and depth <= MAX_NESTING:
        if node.pk == asset.pk:
            raise FindItError(f"{container.reference} is already inside {asset.reference}, so it can't hold it.", "circular")
        node, depth = node.stored_inside, depth + 1


def update_asset(asset, data, user):
    """Apply edited fields, log one history row per changed field (grouped), and adjust status."""
    change_id = uuid.uuid4()
    rows = []
    if "stored_inside" in data:
        _check_container(asset, data["stored_inside"])
    if "purpose" in data and data["purpose"] is not None and data["purpose"].household_id != asset.household_id:
        raise FindItError("That purpose is not in your household.", "invalid_purpose")
    for field, value in data.items():
        if field not in TRACKED_FIELDS:
            continue
        if isinstance(value, str):
            value = value.strip()
        old = getattr(asset, field)
        same = (old.pk if hasattr(old, "pk") else old) == (value.pk if hasattr(value, "pk") else value)
        if isinstance(old, Decimal) or isinstance(value, Decimal):
            same = (Decimal(old) if old is not None else None) == (Decimal(value) if value is not None else None)
        if same:
            continue
        rows.append(_log(asset, user, AssetHistory.Action.UPDATED, change_id, field, _display(field, old), _display(field, value)))
        setattr(asset, field, value)
    if not rows:
        return asset
    with transaction.atomic():
        _sync_status(asset, user, change_id, rows)
        asset.updated_by = user
        asset.save()
        AssetHistory.objects.bulk_create(rows)
    return asset


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------


def _jpeg(img, max_side, quality):
    img = img.copy()
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue(), img.size


def add_image(asset, upload, user):
    if asset.images.count() >= MAX_IMAGES_PER_ASSET:
        raise FindItError(f"A box can have up to {MAX_IMAGES_PER_ASSET} photos.", "too_many_images")
    if upload.size > MAX_UPLOAD_BYTES:
        raise FindItError("Photo is too large (max 15 MB).", "file_too_large")
    try:
        img = Image.open(upload)
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise FindItError("That file is not a photo we can read. Use JPG, PNG or WEBP.", "invalid_image")
    # Re-encode: strips EXIF (incl. GPS), fixes rotation, and keeps the server's disk use small.
    img = ImageOps.exif_transpose(img).convert("RGB")
    full, size = _jpeg(img, IMAGE_MAX_SIDE, 82)
    thumb, _ = _jpeg(img, THUMB_MAX_SIDE, 75)
    change_id = uuid.uuid4()
    name = (getattr(upload, "name", "") or "photo")[:255]
    with transaction.atomic():
        record = AssetImage(asset=asset, original_name=name, width=size[0], height=size[1], uploaded_by=user)
        record.image.save("photo.jpg", ContentFile(full), save=False)
        record.thumbnail.save("thumb.jpg", ContentFile(thumb), save=False)
        record.save()
        rows = [_log(asset, user, AssetHistory.Action.IMAGE_ADDED, change_id, "images", "", name)]
        _sync_status(asset, user, change_id, rows)
        asset.updated_by = user
        asset.save()
        AssetHistory.objects.bulk_create(rows)
    return record


def _delete_files(image):
    for field in (image.image, image.thumbnail):
        if field:
            field.delete(save=False)


def remove_image(image, user):
    asset = image.asset
    change_id = uuid.uuid4()
    with transaction.atomic():
        name = image.original_name or "photo"
        rows = [_log(asset, user, AssetHistory.Action.IMAGE_REMOVED, change_id, "images", name, "")]
        _delete_files(image)
        image.delete()
        _sync_status(asset, user, change_id, rows)
        asset.updated_by = user
        asset.save()
        AssetHistory.objects.bulk_create(rows)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def reset_asset(asset, user):
    """Clear details and photos for reuse. UUID, VHS number, QR label and history stay."""
    change_id = uuid.uuid4()
    rows = [_log(asset, user, AssetHistory.Action.RESET, change_id, "", asset.get_status_display(), "Empty")]
    for field in TRACKED_FIELDS:
        old = getattr(asset, field)
        if old not in (None, ""):
            rows.append(_log(asset, user, AssetHistory.Action.RESET, change_id, field, _display(field, old), ""))
    images = list(asset.images.all())
    for image in images:
        rows.append(_log(asset, user, AssetHistory.Action.IMAGE_REMOVED, change_id, "images", image.original_name or "photo", ""))
    with transaction.atomic():
        for image in images:
            _delete_files(image)
            image.delete()
        asset.name = asset.location = asset.description = ""
        asset.purpose = asset.stored_inside = None
        asset.latitude = asset.longitude = None
        asset.status = Asset.Status.EMPTY
        asset.updated_by = user
        asset.save()
        AssetHistory.objects.bulk_create(rows)
    return asset


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

REF_RE = re.compile(r"^\s*vhs\s*-?\s*(\d+)\s*$", re.IGNORECASE)
UUID_RE = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.IGNORECASE)


def exact_match(queryset, q):
    """A VHS number or UUID (or a scanned label link) that points at one asset."""
    q = (q or "").strip()
    m = REF_RE.match(q)
    if m:
        return queryset.filter(number=int(m.group(1))).first()
    if q.isdigit():
        return queryset.filter(number=int(q)).first()
    m = UUID_RE.search(q)
    if m:
        try:
            return queryset.filter(uuid=uuid.UUID(m.group(0))).first()
        except ValueError:
            return None
    return None


def search(queryset, q):
    """Every word must appear somewhere: name, number, description, purpose, location or a container's name."""
    q = (q or "").strip()
    if not q:
        return queryset
    hit = exact_match(queryset, q)
    if hit is not None:
        return queryset.filter(pk=hit.pk)
    for word in q.split()[:8]:
        queryset = queryset.filter(
            Q(name__icontains=word) | Q(reference__icontains=word) | Q(description__icontains=word)
            | Q(purpose__name__icontains=word) | Q(location__icontains=word)
            | Q(stored_inside__name__icontains=word) | Q(stored_inside__location__icontains=word)
        )
    return queryset


def location_suggestions(household, q="", limit=12):
    qs = Asset.objects.filter(household=household).exclude(location="")
    if q:
        qs = qs.filter(location__icontains=q)
    rows = qs.values("location").annotate(uses=Count("id")).order_by("-uses", "location")[:limit]
    return [r["location"] for r in rows]


def purpose_for(household, name):
    """Find or create a purpose by name (used when a purpose is typed in the box form)."""
    name = (name or "").strip()[:120]
    if not name:
        return None
    existing = Purpose.objects.filter(household=household, name__iexact=name).first()
    return existing or Purpose.objects.create(household=household, name=name)
