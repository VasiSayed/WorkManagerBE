import uuid as uuidlib

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.db.models import Count, Prefetch
from django.http import FileResponse, Http404, HttpResponse
from rest_framework import decorators, mixins, permissions, response, status, viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.views import APIView

from accounts.models import UserModuleAccess

from . import services
from .labels import labels_pdf
from .models import Asset, AssetHistory, AssetImage, Household, HouseholdMember, Purpose
from .serializers import (
    FILE_MAX_AGE,
    FILE_SALT,
    AssetDetailSerializer,
    AssetListSerializer,
    AssetUpdateSerializer,
    HistorySerializer,
    MemberSerializer,
    PurposeSerializer,
)

User = get_user_model()
MAX_LABELS = 2000


def error(exc):
    return response.Response({"detail": exc.message, "code": exc.code}, status=exc.status)


class HouseholdMixin:
    permission_classes = [permissions.IsAuthenticated]

    @property
    def household(self):
        if not hasattr(self, "_household"):
            self._household = services.household_for(self.request.user)
        return self._household

    def get_serializer_context(self):
        ctx = super().get_serializer_context() if hasattr(super(), "get_serializer_context") else {"request": self.request}
        ctx["household"] = self.household
        return ctx


class AssetPagination(PageNumberPagination):
    page_size = 30
    page_size_query_param = "page_size"
    max_page_size = 200


# ---------------------------------------------------------------------------
# Household & family members
# ---------------------------------------------------------------------------


class HouseholdView(HouseholdMixin, APIView):
    def payload(self):
        hh = self.household
        members = hh.members.select_related("user")
        return {
            "id": hh.id, "name": hh.name, "is_owner": services.is_owner(self.request.user, hh),
            "members": MemberSerializer(members, many=True).data,
        }

    def get(self, request):
        return response.Response(self.payload())

    def patch(self, request):
        if not services.is_owner(request.user, self.household):
            return response.Response({"detail": "Only the household owner can rename it."}, status=403)
        name = (request.data.get("name") or "").strip()[:120]
        if not name:
            return response.Response({"detail": "Name is required."}, status=400)
        self.household.name = name
        self.household.save(update_fields=["name", "updated_at"])
        return response.Response(self.payload())


class HouseholdMembersView(HouseholdMixin, APIView):
    def post(self, request):
        """Owner adds a family member by the email they registered with."""
        hh = self.household
        if not services.is_owner(request.user, hh):
            return response.Response({"detail": "Only the household owner can add family members."}, status=403)
        email = (request.data.get("email") or "").strip()
        user = User.objects.filter(email__iexact=email).first() if email else None
        if not user:
            return response.Response(
                {"detail": "No account with that email. Ask them to register first, then add them here."}, status=400
            )
        existing = HouseholdMember.objects.filter(user=user).select_related("household").first()
        with transaction.atomic():
            if existing:
                if existing.household_id == hh.id:
                    return response.Response({"detail": "Already in your household."}, status=400)
                other = existing.household
                # A household that someone opened once but never used can be left behind.
                if other.assets.exists() or other.members.count() > 1:
                    return response.Response({"detail": "That person already belongs to another household."}, status=400)
                existing.delete()
                other.purposes.all().delete()
                other.delete()
            HouseholdMember.objects.create(household=hh, user=user, role=HouseholdMember.Role.MEMBER)
            UserModuleAccess.objects.get_or_create(user=user, module="findit")
        return response.Response(HouseholdView.payload(self), status=201)

    def delete(self, request, member_id):
        hh = self.household
        member = HouseholdMember.objects.filter(household=hh, pk=member_id).first()
        if not member:
            raise Http404
        owner = services.is_owner(request.user, hh)
        if not owner and member.user_id != request.user.id:
            return response.Response({"detail": "Only the owner can remove family members."}, status=403)
        if member.role == HouseholdMember.Role.OWNER:
            return response.Response({"detail": "The owner cannot be removed."}, status=400)
        member.delete()
        return response.Response(status=204)


# ---------------------------------------------------------------------------
# Purposes
# ---------------------------------------------------------------------------


class PurposeViewSet(HouseholdMixin, viewsets.ModelViewSet):
    serializer_class = PurposeSerializer
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    pagination_class = None

    def get_queryset(self):
        qs = Purpose.objects.filter(household=self.household).annotate(assets=Count("assets"))
        if self.request.query_params.get("active") == "true":
            qs = qs.filter(is_active=True)
        q = self.request.query_params.get("q", "").strip()
        return qs.filter(name__icontains=q) if q else qs

    def perform_create(self, serializer):
        serializer.save(household=self.household)

    def perform_destroy(self, instance):
        with transaction.atomic():
            change_id = uuidlib.uuid4()
            AssetHistory.objects.bulk_create([
                AssetHistory(asset=a, change_id=change_id, action=AssetHistory.Action.UPDATED, field="purpose",
                             old_value=instance.name, new_value="", user=self.request.user)
                for a in instance.assets.all()
            ])
            if instance.image:
                instance.image.delete(save=False)
            instance.delete()


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


class AssetViewSet(HouseholdMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Boxes are created in bulk and never deleted (archive or reset instead). Looked up by UUID."""

    lookup_field = "uuid"
    pagination_class = AssetPagination
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_serializer_class(self):
        return AssetListSerializer if self.action == "list" else AssetDetailSerializer

    def get_queryset(self):
        qs = (
            Asset.objects.filter(household=self.household)
            .select_related("purpose", "stored_inside", "created_by", "updated_by")
            .prefetch_related(Prefetch("images", queryset=AssetImage.objects.order_by("created_at", "id")))
            .annotate(image_count=Count("images", distinct=True))
        )
        if self.action != "list":
            return qs
        p = self.request.query_params
        statuses = [s for s in p.get("status", "").split(",") if s in Asset.Status.values]
        if statuses:
            qs = qs.filter(status__in=statuses)
        elif p.get("include_archived") != "true":
            qs = qs.exclude(status=Asset.Status.ARCHIVED)
        if p.get("purpose") == "none":
            qs = qs.filter(purpose__isnull=True)
        elif p.get("purpose"):
            qs = qs.filter(purpose_id=p["purpose"])
        if p.get("has_photos") == "true":
            qs = qs.filter(image_count__gt=0)
        if p.get("ref_from"):
            qs = qs.filter(number__gte=_int(p["ref_from"]))
        if p.get("ref_to"):
            qs = qs.filter(number__lte=_int(p["ref_to"]))
        qs = services.search(qs, p.get("q"))
        ordering = {"newest": "-number", "oldest": "number", "updated": "-updated_at", "name": "name"}.get(p.get("sort"), "-number")
        return qs.order_by(ordering, "-number")

    def get_object(self):
        value = self.kwargs.get("uuid", "")
        try:
            key = uuidlib.UUID(str(value))
        except ValueError:
            raise Http404
        obj = self.get_queryset().filter(uuid=key).first()
        if obj is None:  # unknown, or another household's box: same answer, reveals nothing
            raise Http404
        return obj

    def detail_response(self, asset, code=200):
        asset = self.get_queryset().get(pk=asset.pk)
        return response.Response(AssetDetailSerializer(asset, context=self.get_serializer_context()).data, status=code)

    def partial_update(self, request, uuid=None):
        asset = self.get_object()
        data = AssetUpdateSerializer(data=request.data, context=self.get_serializer_context())
        data.is_valid(raise_exception=True)
        try:
            services.update_asset(asset, data.validated_data, request.user)
        except services.FindItError as exc:
            return error(exc)
        return self.detail_response(asset)

    @decorators.action(detail=False, methods=["post"])
    def bulk(self, request):
        try:
            created = services.bulk_create_assets(self.household, request.data.get("count"), request.user)
        except services.FindItError as exc:
            return error(exc)
        return response.Response({
            "count": len(created),
            "first": created[0].reference,
            "last": created[-1].reference,
            "ids": [a.id for a in created],
        }, status=201)

    @decorators.action(detail=True, methods=["post"], url_path="status")
    def set_status(self, request, uuid=None):
        asset = self.get_object()
        try:
            services.set_status(asset, request.data.get("status"), request.user)
        except services.FindItError as exc:
            return error(exc)
        return self.detail_response(asset)

    @decorators.action(detail=True, methods=["post"])
    def reset(self, request, uuid=None):
        asset = self.get_object()
        if request.data.get("confirm") is not True and request.data.get("confirm") != "true":
            return response.Response({"detail": "Send confirm=true to reset.", "code": "confirmation_required"}, status=400)
        services.reset_asset(asset, request.user)
        return self.detail_response(asset)

    @decorators.action(detail=True, methods=["post"])
    def images(self, request, uuid=None):
        asset = self.get_object()
        files = request.FILES.getlist("images") or request.FILES.getlist("image")
        if not files:
            return response.Response({"detail": "Choose at least one photo."}, status=400)
        try:
            for upload in files[:10]:
                services.add_image(asset, upload, request.user)
        except services.FindItError as exc:
            return error(exc)
        return self.detail_response(asset, 201)

    @decorators.action(detail=True, methods=["delete"], url_path=r"images/(?P<image_id>\d+)")
    def remove_image(self, request, uuid=None, image_id=None):
        asset = self.get_object()
        image = asset.images.filter(pk=image_id).first()
        if not image:
            raise Http404
        services.remove_image(image, request.user)
        return self.detail_response(asset)

    @decorators.action(detail=True, methods=["get"])
    def history(self, request, uuid=None):
        asset = self.get_object()
        rows = asset.history.select_related("user")[:500]
        return response.Response(HistorySerializer(rows, many=True).data)

    @decorators.action(detail=False, methods=["get"])
    def resolve(self, request):
        """A typed VHS number, UUID or scanned label link -> that box (or 404)."""
        asset = services.exact_match(Asset.objects.filter(household=self.household), request.query_params.get("q"))
        if asset is None:
            raise Http404
        return response.Response({"uuid": str(asset.uuid), "reference": asset.reference, "status": asset.status})

    @decorators.action(detail=False, methods=["get"])
    def summary(self, request):
        counts = dict(
            Asset.objects.filter(household=self.household).values_list("status").annotate(n=Count("id")).values_list("status", "n")
        )
        recent = self.get_queryset().exclude(status=Asset.Status.EMPTY).order_by("-updated_at")[:6]
        return response.Response({
            "total": sum(counts.values()),
            "by_status": {s: counts.get(s, 0) for s in Asset.Status.values},
            "recent": AssetListSerializer(recent, many=True, context=self.get_serializer_context()).data,
            "household": self.household.name,
        })

    @decorators.action(detail=False, methods=["get"])
    def locations(self, request):
        return response.Response(services.location_suggestions(self.household, request.query_params.get("q", "").strip()))

    @decorators.action(detail=False, methods=["get"])
    def containers(self, request):
        """Search boxes to pick a 'Stored inside' container."""
        qs = services.search(
            Asset.objects.filter(household=self.household).exclude(status=Asset.Status.ARCHIVED), request.query_params.get("q")
        )
        exclude = request.query_params.get("exclude")
        if exclude:
            try:
                qs = qs.exclude(uuid=uuidlib.UUID(exclude))
            except ValueError:
                pass
        rows = qs.order_by("-updated_at")[:20]
        return response.Response([
            {"id": a.id, "uuid": str(a.uuid), "reference": a.reference, "name": a.name, "location": a.location} for a in rows
        ])


def _int(value):
    try:
        return int(str(value).upper().replace("VHS", "").strip())
    except ValueError:
        return 0


class LabelsPdfView(HouseholdMixin, APIView):
    """POST {ids: [...], copies: 1-4} -> A4 PDF of QR labels in VHS order (copies sit next to each other)."""

    def post(self, request):
        ids = request.data.get("ids") or []
        try:
            ids = [int(i) for i in ids]
            copies = max(1, min(4, int(request.data.get("copies") or 1)))
        except (TypeError, ValueError):
            return response.Response({"detail": "Invalid selection."}, status=400)
        assets = list(Asset.objects.filter(household=self.household, pk__in=ids).order_by("number").values_list("uuid", "reference"))
        if not assets:
            return response.Response({"detail": "Select at least one box."}, status=400)
        labels = [a for a in assets for _ in range(copies)]
        if len(labels) > MAX_LABELS:
            return response.Response({"detail": f"At most {MAX_LABELS} labels per PDF."}, status=400)
        pdf = labels_pdf(labels, title=f"FindIt labels {assets[0][1]}–{assets[-1][1]}")
        resp = HttpResponse(pdf, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="findit-labels-{assets[0][1]}-{assets[-1][1]}.pdf"'
        resp["Cache-Control"] = "private, no-store"
        return resp


class FileView(APIView):
    """Private photos, opened through a short-lived signed link (works in <img> without a token)."""

    authentication_classes = []
    permission_classes = []

    def get(self, request, token):
        try:
            data = signing.loads(token, salt=FILE_SALT, max_age=FILE_MAX_AGE)
        except signing.BadSignature:
            return response.Response({"detail": "Link expired. Reload the page."}, status=403)
        kind, obj_id = data.get("k"), data.get("id")
        if kind in ("image", "thumb"):
            obj = AssetImage.objects.filter(pk=obj_id).first()
            field = (obj.thumbnail or obj.image) if obj and kind == "thumb" else (obj.image if obj else None)
        elif kind == "purpose":
            obj = Purpose.objects.filter(pk=obj_id).first()
            field = obj.image if obj else None
        else:
            field = None
        if not field:
            raise Http404
        try:
            handle = field.open("rb")
        except FileNotFoundError:
            raise Http404
        resp = FileResponse(handle, content_type="image/jpeg")
        resp["Cache-Control"] = "private, max-age=3000"
        resp["X-Content-Type-Options"] = "nosniff"
        return resp
