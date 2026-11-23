from django.core import signing
from django.urls import reverse
from rest_framework import serializers

from . import services
from .labels import scan_url
from .models import Asset, AssetHistory, AssetImage, HouseholdMember, Purpose

FILE_SALT = "findit-file"
FILE_MAX_AGE = 60 * 60  # links in API responses stay valid for an hour


def file_url(request, image_field, kind, obj_id):
    if not image_field:
        return None
    token = signing.dumps({"k": kind, "id": obj_id}, salt=FILE_SALT, compress=True)
    url = reverse("findit-file", args=[token])
    return request.build_absolute_uri(url) if request else url


class HouseholdScopedPK(serializers.PrimaryKeyRelatedField):
    """Only objects from the requesting user's household can be referenced."""

    def get_queryset(self):
        household = self.context.get("household")
        if household is None:
            return self.queryset.none()
        return self.queryset.filter(household=household)


class PurposeSerializer(serializers.ModelSerializer):
    assets = serializers.IntegerField(read_only=True, default=0)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Purpose
        fields = ["id", "uuid", "name", "description", "image", "image_url", "is_active", "assets", "created_at", "updated_at"]
        read_only_fields = ["id", "uuid", "image_url", "assets", "created_at", "updated_at"]
        extra_kwargs = {"image": {"write_only": True, "required": False}}

    def get_image_url(self, obj):
        return file_url(self.context.get("request"), obj.image, "purpose", obj.pk)

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Name is required.")
        qs = Purpose.objects.filter(household=self.context["household"], name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A purpose with this name already exists.")
        return value


class AssetImageSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    thumb_url = serializers.SerializerMethodField()

    class Meta:
        model = AssetImage
        fields = ["id", "url", "thumb_url", "original_name", "width", "height", "created_at"]

    def get_url(self, obj):
        return file_url(self.context.get("request"), obj.image, "image", obj.pk)

    def get_thumb_url(self, obj):
        return file_url(self.context.get("request"), obj.thumbnail or obj.image, "thumb", obj.pk)


def _brief(asset):
    return {"id": asset.id, "uuid": str(asset.uuid), "reference": asset.reference, "name": asset.name,
            "location": asset.location}


class AssetListSerializer(serializers.ModelSerializer):
    """Compact row for lists and search results (one thumbnail, short description)."""

    purpose_name = serializers.CharField(source="purpose.name", read_only=True, default="")
    thumb_url = serializers.SerializerMethodField()
    image_count = serializers.IntegerField(read_only=True, default=0)
    short_description = serializers.SerializerMethodField()
    stored_inside = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = [
            "id", "uuid", "reference", "number", "name", "purpose", "purpose_name", "location", "stored_inside",
            "short_description", "status", "thumb_url", "image_count", "updated_at",
        ]

    def get_thumb_url(self, obj):
        first = obj.images.all()[:1]
        first = first[0] if first else None
        return file_url(self.context.get("request"), first.thumbnail or first.image, "thumb", first.pk) if first else None

    def get_short_description(self, obj):
        text = " ".join(obj.description.split())
        return text[:140] + ("…" if len(text) > 140 else "")

    def get_stored_inside(self, obj):
        return _brief(obj.stored_inside) if obj.stored_inside_id else None


class AssetDetailSerializer(AssetListSerializer):
    images = AssetImageSerializer(many=True, read_only=True)
    storage_path = serializers.SerializerMethodField()
    contents = serializers.SerializerMethodField()
    qr_data = serializers.SerializerMethodField()
    maps_url = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()

    class Meta(AssetListSerializer.Meta):
        fields = AssetListSerializer.Meta.fields + [
            "description", "latitude", "longitude", "maps_url", "images", "storage_path", "contents", "qr_data",
            "created_at", "created_by_name", "updated_by_name",
        ]

    def get_storage_path(self, obj):
        """[this box's container, its container, ...] each with its own text location."""
        return [_brief(a) for a in services.storage_chain(obj)]

    def get_contents(self, obj):
        return [_brief(a) for a in obj.contents.exclude(status=Asset.Status.ARCHIVED).order_by("number")]

    def get_qr_data(self, obj):
        return scan_url(obj.uuid)

    def get_maps_url(self, obj):
        if obj.latitude is None or obj.longitude is None:
            return None
        return f"https://www.google.com/maps?q={obj.latitude},{obj.longitude}"

    def _name(self, user):
        return (user.full_name or user.email) if user else ""

    def get_created_by_name(self, obj):
        return self._name(obj.created_by)

    def get_updated_by_name(self, obj):
        return self._name(obj.updated_by)


class AssetUpdateSerializer(serializers.Serializer):
    """Editable details. Everything optional; send only what changed."""

    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    purpose = HouseholdScopedPK(queryset=Purpose.objects.all(), required=False, allow_null=True)
    purpose_name = serializers.CharField(max_length=120, required=False, allow_blank=True, write_only=True)
    location = serializers.CharField(max_length=300, required=False, allow_blank=True)
    stored_inside = HouseholdScopedPK(queryset=Asset.objects.all(), required=False, allow_null=True)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True, min_value=-90, max_value=90)
    longitude = serializers.DecimalField(max_digits=10, decimal_places=6, required=False, allow_null=True, min_value=-180, max_value=180)
    description = serializers.CharField(required=False, allow_blank=True, max_length=20000)

    def to_internal_value(self, data):
        # GPS from the browser can carry more decimals than we store.
        if hasattr(data, "copy"):
            data = data.copy()
        for key in ("latitude", "longitude"):
            value = data.get(key) if hasattr(data, "get") else None
            if value not in (None, ""):
                try:
                    data[key] = round(float(value), 6)
                except (TypeError, ValueError):
                    pass
            elif value == "":
                data[key] = None
        return super().to_internal_value(data)

    def validate(self, attrs):
        if ("latitude" in attrs) != ("longitude" in attrs) or (attrs.get("latitude") is None) != (attrs.get("longitude") is None):
            raise serializers.ValidationError({"latitude": "Send latitude and longitude together."})
        typed = attrs.pop("purpose_name", None)
        if typed is not None and "purpose" not in attrs:
            attrs["purpose"] = services.purpose_for(self.context["household"], typed)
        return attrs


class HistorySerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    field_label = serializers.SerializerMethodField()
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = AssetHistory
        fields = ["id", "change_id", "action", "action_label", "field", "field_label", "old_value", "new_value",
                  "user_name", "created_at"]

    def get_field_label(self, obj):
        return {**services.TRACKED_FIELDS, "status": "Status", "images": "Photo"}.get(obj.field, obj.field)

    def get_user_name(self, obj):
        return (obj.user.full_name or obj.user.email) if obj.user else ""


class MemberSerializer(serializers.ModelSerializer):
    email = serializers.CharField(source="user.email", read_only=True)
    name = serializers.SerializerMethodField()

    class Meta:
        model = HouseholdMember
        fields = ["id", "email", "name", "role", "joined_at"]

    def get_name(self, obj):
        return obj.user.full_name or obj.user.email
