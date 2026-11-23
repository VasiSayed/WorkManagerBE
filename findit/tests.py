import io
import shutil
import tempfile
import threading
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import UserModuleAccess

from . import services
from .models import Asset, AssetHistory, AssetImage, HouseholdMember

TMP_ROOT = tempfile.mkdtemp(prefix="findit-test-")
BASE = "/api/findit"


def photo(name="box.jpg", size=(2400, 1800), fmt="JPEG", exif_gps=False):
    buf = io.BytesIO()
    img = Image.new("RGB", size, (200, 30, 30))
    kwargs = {}
    if exif_gps:
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (19.0, 2.0, 3.0)}  # GPS info block
        kwargs["exif"] = exif.tobytes()
    img.save(buf, fmt, **kwargs)
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


@override_settings(FINDIT_PRIVATE_ROOT=TMP_ROOT, FINDIT_SCAN_BASE_URL="https://app.example")
class FindItTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def setUp(self):
        User = get_user_model()
        self.me = User.objects.create_user(email="me@example.com", password="pass12345!", full_name="Vasi")
        self.mom = User.objects.create_user(email="mom@example.com", password="pass12345!", full_name="Mom")
        self.stranger = User.objects.create_user(email="x@example.com", password="pass12345!")
        for u in (self.me, self.stranger):
            UserModuleAccess.objects.create(user=u, module="findit")
        self.client = self.as_user(self.me)

    def as_user(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def call(self, method, path, data=None, status=200, client=None, fmt="json"):
        client = client or self.client
        res = getattr(client, method)(f"{BASE}{path}", data, format=fmt) if data is not None else getattr(client, method)(f"{BASE}{path}")
        self.assertEqual(res.status_code, status, getattr(res, "data", res))
        return res.data if hasattr(res, "data") else res

    def bulk(self, n, client=None):
        return self.call("post", "/assets/bulk/", {"count": n}, 201, client)

    def first(self, ref="VHS1"):
        return Asset.objects.get(reference=ref)


class ModuleAccessTests(FindItTestCase):
    """Module access is enforced by the API, not only hidden in the app (JWT path, like the real app)."""

    def jwt_client(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
        return client

    def test_module_not_granted_is_blocked(self):
        mom = self.jwt_client(self.mom)
        res = mom.get(f"{BASE}/assets/summary/")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "module_not_granted")
        self.assertEqual(mom.get("/api/property-management/properties/").status_code, 403)
        self.assertEqual(mom.get("/api/expenses/").status_code, 403)
        me = self.jwt_client(self.me)
        self.assertEqual(me.get(f"{BASE}/assets/summary/").status_code, 200)

    def test_me_lists_modules_and_admin_manages_access(self):
        me = self.jwt_client(self.me)
        self.assertEqual(me.get("/api/auth/me/").json()["modules"], ["findit"])
        self.assertEqual(me.get("/api/auth/users/").status_code, 403)  # not an admin
        self.me.is_staff = True
        self.me.save()
        data = me.get("/api/auth/users/").json()
        self.assertIn("findit", [m["key"] for m in data["modules"]])
        res = me.put(f"/api/auth/users/{self.mom.id}/modules/", {"modules": ["findit", "expense"]}, format="json")
        self.assertEqual(res.json()["modules"], ["expense", "findit"])
        self.assertEqual(self.jwt_client(self.mom).get(f"{BASE}/assets/summary/").status_code, 200)
        bad = me.put(f"/api/auth/users/{self.mom.id}/modules/", {"modules": ["nope"]}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_new_signup_starts_with_no_modules(self):
        res = APIClient().post("/api/auth/register/", {"email": "new@example.com", "password": "Strong-pass-123",
                                                       "confirm_password": "Strong-pass-123"}, format="json")
        self.assertEqual(res.status_code, 201)
        new = get_user_model().objects.get(email="new@example.com")
        self.assertEqual(self.jwt_client(new).get("/api/auth/me/").json()["modules"], [])


class BulkAndNumberingTests(FindItTestCase):
    def test_bulk_creates_empty_assets_with_sequential_numbers(self):
        res = self.bulk(50)
        self.assertEqual((res["count"], res["first"], res["last"]), (50, "VHS1", "VHS50"))
        res = self.bulk(50)
        self.assertEqual((res["first"], res["last"]), ("VHS51", "VHS100"))
        assets = Asset.objects.order_by("number")
        self.assertEqual(assets.count(), 100)
        self.assertEqual(len(set(assets.values_list("uuid", flat=True))), 100)
        a = assets.first()
        self.assertEqual((a.status, a.name, a.purpose, a.location, a.latitude), ("empty", "", None, "", None))
        self.assertEqual(AssetHistory.objects.filter(action="created").count(), 100)

    def test_one_series_across_households_and_never_reused(self):
        self.bulk(3)
        stranger = self.as_user(self.stranger)
        self.assertEqual(self.bulk(2, stranger)["first"], "VHS4")
        services.reset_asset(self.first("VHS5"), self.me)
        Asset.objects.filter(reference="VHS5").update(status="archived")
        self.assertEqual(self.bulk(1)["first"], "VHS6")

    def test_bulk_limits(self):
        self.call("post", "/assets/bulk/", {"count": 0}, 400)
        self.call("post", "/assets/bulk/", {"count": 501}, 400)
        self.call("post", "/assets/bulk/", {"count": "abc"}, 400)

    def test_cannot_create_one_by_one_or_delete(self):
        self.bulk(1)
        a = self.first()
        self.assertEqual(self.client.post(f"{BASE}/assets/", {}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(f"{BASE}/assets/{a.uuid}/").status_code, 405)


class ConcurrentNumberingTests(TransactionTestCase):
    """Two bulk creations at the same moment never get the same numbers."""

    def test_parallel_bulk(self):
        from django.db import connection

        User = get_user_model()
        users = [User.objects.create_user(email=f"u{i}@example.com", password="pass12345!") for i in range(2)]
        households = [services.household_for(u) for u in users]
        errors, done = [], []

        def run(hh, user):
            try:
                for _ in range(5):
                    services.bulk_create_assets(hh, 20, user)
                    done.append(1)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(h, u)) for h, u in zip(households, users)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        numbers = list(Asset.objects.values_list("number", flat=True))
        # The in-memory test database may refuse a writer ("table is locked"); a real SQLite file
        # makes it wait instead. Either way no number is ever handed out twice.
        self.assertEqual(len(numbers), 20 * len(done))
        self.assertEqual(len(numbers), len(set(numbers)))


class RegistrationTests(FindItTestCase):
    def setUp(self):
        super().setUp()
        self.bulk(3)
        self.a = self.first()

    def patch(self, data, status=200, asset=None):
        asset = asset or self.a
        return self.call("patch", f"/assets/{asset.uuid}/", data, status)

    def test_scan_open_edit_and_mark_ready(self):
        detail = self.call("get", f"/assets/{self.a.uuid}/")
        self.assertEqual((detail["reference"], detail["status"]), ("VHS1", "empty"))
        self.assertEqual(detail["qr_data"], f"https://app.example/q/{self.a.uuid.hex}")
        res = self.patch({"name": "Marriage Saree Box", "purpose_name": "Marriage Items",
                          "location": "Bedroom → Wardrobe → Top Shelf", "description": "3 sarees"})
        self.assertEqual((res["status"], res["purpose_name"]), ("draft", "Marriage Items"))
        res = self.call("post", f"/assets/{self.a.uuid}/status/", {"status": "ready"})
        self.assertEqual(res["status"], "ready")
        # One save = one grouped history entry with a row per field.
        saves = AssetHistory.objects.filter(asset=self.a, action="updated").values("change_id").distinct().count()
        self.assertEqual(saves, 1)
        loc = AssetHistory.objects.get(asset=self.a, field="location")
        self.assertEqual((loc.old_value, loc.new_value), ("", "Bedroom → Wardrobe → Top Shelf"))

    def test_ready_needs_a_name_and_identity_never_changes(self):
        self.call("post", f"/assets/{self.a.uuid}/status/", {"status": "ready"}, 400)
        before = (self.a.uuid, self.a.reference)
        self.patch({"name": "Box", "location": "Hall"})
        self.patch({"name": "Box 2"})
        self.a.refresh_from_db()
        self.assertEqual((self.a.uuid, self.a.reference), before)
        self.patch({"name": "Box 2"})  # no change -> no history row
        self.assertEqual(AssetHistory.objects.filter(asset=self.a, field="name").count(), 2)

    def test_gps_and_maps_link(self):
        res = self.patch({"latitude": "19.0760999", "longitude": "72.8776559"})
        self.assertEqual(res["maps_url"], "https://www.google.com/maps?q=19.076100,72.877656")
        self.patch({"latitude": "19.07"}, status=400)  # must come together
        res = self.patch({"latitude": None, "longitude": None})
        self.assertIsNone(res["maps_url"])

    def test_stored_inside_path_and_no_loops(self):
        bag, box = self.first("VHS2"), self.first("VHS3")
        self.patch({"name": "Big box", "location": "Bedroom → Wardrobe"}, asset=box)
        self.patch({"name": "Small bag", "stored_inside": box.id}, asset=bag)
        self.patch({"name": "Folder", "stored_inside": bag.id})
        res = self.call("get", f"/assets/{self.a.uuid}/")
        self.assertEqual([p["reference"] for p in res["storage_path"]], ["VHS2", "VHS3"])
        self.assertEqual(res["storage_path"][-1]["location"], "Bedroom → Wardrobe")
        self.assertEqual([c["reference"] for c in self.call("get", f"/assets/{box.uuid}/")["contents"]], ["VHS2"])
        loop = self.patch({"stored_inside": self.a.id}, status=400, asset=box)
        self.assertEqual(loop["code"], "circular")
        self.patch({"stored_inside": self.a.id}, status=400)

    def test_other_households_cannot_see_or_use(self):
        stranger = self.as_user(self.stranger)
        self.call("get", f"/assets/{self.a.uuid}/", status=404, client=stranger)
        self.call("patch", f"/assets/{self.a.uuid}/", {"name": "x"}, 404, client=stranger)
        self.call("get", "/assets/resolve/?q=VHS1", status=404, client=stranger)
        self.bulk(1, stranger)
        theirs = Asset.objects.get(reference="VHS4")
        self.patch({"stored_inside": theirs.id}, status=400)
        self.call("get", "/assets/not-a-uuid/", status=404)

    def test_location_suggestions(self):
        self.patch({"location": "Bedroom → Wardrobe"})
        self.patch({"location": "Bedroom → Wardrobe"}, asset=self.first("VHS2"))
        self.patch({"location": "Kitchen"}, asset=self.first("VHS3"))
        self.assertEqual(self.call("get", "/assets/locations/"), ["Bedroom → Wardrobe", "Kitchen"])
        self.assertEqual(self.call("get", "/assets/locations/?q=kit"), ["Kitchen"])


class PhotoTests(FindItTestCase):
    def setUp(self):
        super().setUp()
        self.bulk(1)
        self.a = self.first()

    def test_upload_resizes_strips_location_and_serves_privately(self):
        res = self.call("post", f"/assets/{self.a.uuid}/images/", {"images": [photo(exif_gps=True), photo("2.png", fmt="PNG")]},
                        201, fmt="multipart")
        self.assertEqual(len(res["images"]), 2)
        self.assertEqual(res["status"], "draft")  # a photo is enough to leave Empty
        img = AssetImage.objects.first()
        self.assertLessEqual(max(img.width, img.height), 1600)
        stored = Image.open(img.image.path)
        self.assertNotIn(0x8825, stored.getexif())
        self.assertLessEqual(max(Image.open(img.thumbnail.path).size), 360)
        # Signed link works without a token; a tampered one does not.
        url = res["images"][0]["thumb_url"].replace("http://testserver", "")
        self.assertEqual(APIClient().get(url).status_code, 200)
        self.assertEqual(APIClient().get(url[:-6] + "abcdef/").status_code, 403)
        # Not under MEDIA (public) folder.
        self.assertTrue(img.image.path.startswith(TMP_ROOT))

    def test_rejects_non_images_and_removes(self):
        bad = SimpleUploadedFile("evil.jpg", b"<script>alert(1)</script>", content_type="image/jpeg")
        self.call("post", f"/assets/{self.a.uuid}/images/", {"images": [bad]}, 400, fmt="multipart")
        res = self.call("post", f"/assets/{self.a.uuid}/images/", {"images": [photo()]}, 201, fmt="multipart")
        path = AssetImage.objects.get().image.path
        res = self.call("delete", f"/assets/{self.a.uuid}/images/{res['images'][0]['id']}/")
        self.assertEqual((res["images"], res["status"]), ([], "empty"))
        self.assertFalse(AssetImage.objects.exists())
        import os
        self.assertFalse(os.path.exists(path))
        actions = list(AssetHistory.objects.filter(asset=self.a).values_list("action", flat=True))
        self.assertIn("image_added", actions)
        self.assertIn("image_removed", actions)


class ResetArchiveTests(FindItTestCase):
    def test_reset_clears_details_keeps_identity_and_history(self):
        self.bulk(2)
        a, outer = self.first(), self.first("VHS2")
        self.call("patch", f"/assets/{a.uuid}/", {"name": "Winter clothes", "purpose_name": "Winter", "location": "Loft",
                                                  "description": "Blankets", "latitude": 19.1, "longitude": 72.8,
                                                  "stored_inside": outer.id})
        self.call("post", f"/assets/{a.uuid}/images/", {"images": [photo()]}, 201, fmt="multipart")
        self.call("post", f"/assets/{a.uuid}/status/", {"status": "ready"})
        self.call("post", f"/assets/{a.uuid}/reset/", {}, 400)  # needs confirmation
        res = self.call("post", f"/assets/{a.uuid}/reset/", {"confirm": True})
        self.assertEqual((res["name"], res["purpose"], res["location"], res["description"], res["latitude"], res["images"],
                          res["stored_inside"], res["status"]), ("", None, "", "", None, [], None, "empty"))
        self.assertEqual((res["uuid"], res["reference"]), (str(a.uuid), "VHS1"))
        reset_rows = AssetHistory.objects.filter(asset=a, action="reset")
        self.assertEqual(reset_rows.get(field="name").old_value, "Winter clothes")
        self.assertEqual(reset_rows.get(field="location").old_value, "Loft")
        self.assertTrue(AssetHistory.objects.filter(asset=a, action="created").exists())
        history = self.call("get", f"/assets/{a.uuid}/history/")
        self.assertGreater(len(history), 8)

    def test_archive_restore_and_lists_hide_archived(self):
        self.bulk(2)
        a = self.first()
        self.call("patch", f"/assets/{a.uuid}/", {"name": "Old box"})
        self.call("post", f"/assets/{a.uuid}/status/", {"status": "archived"})
        self.assertEqual(self.call("get", "/assets/")["count"], 1)
        self.assertEqual(self.call("get", "/assets/?status=archived")["count"], 1)
        res = self.call("post", f"/assets/{a.uuid}/status/", {"status": "draft"})
        self.assertEqual(res["status"], "draft")
        self.assertEqual(list(AssetHistory.objects.filter(asset=a, field="status").values_list("action", flat=True)),
                         ["restored", "archived", "status"])


class SearchTests(FindItTestCase):
    def setUp(self):
        super().setUp()
        self.bulk(60)
        edits = {
            "VHS1": {"name": "Marriage Saree Box", "purpose_name": "Marriage Items", "location": "Bedroom → Wardrobe",
                     "description": "Red bridal saree and jewellery accessories"},
            "VHS2": {"name": "Winter Clothes", "purpose_name": "Winter Clothes", "location": "Loft"},
            "VHS3": {"name": "Cables", "location": "Bedroom → Desk", "description": "Phone chargers"},
        }
        for ref, data in edits.items():
            self.call("patch", f"/assets/{self.first(ref).uuid}/", data)

    def refs(self, q):
        return [r["reference"] for r in self.call("get", f"/assets/?q={q}&page_size=100")["results"]]

    def test_search_fields_and_partial_words(self):
        self.assertEqual(self.refs("marriage saree"), ["VHS1"])
        self.assertEqual(self.refs("sar"), ["VHS1"])
        self.assertEqual(self.refs("winter"), ["VHS2"])
        self.assertEqual(sorted(self.refs("bedroom")), ["VHS1", "VHS3"])
        self.assertEqual(self.refs("charger"), ["VHS3"])
        self.assertEqual(self.refs("jewellery"), ["VHS1"])

    def test_reference_and_uuid_lookup(self):
        self.assertEqual(self.refs("VHS52"), ["VHS52"])
        self.assertEqual(self.refs("vhs 2"), ["VHS2"])
        a = self.first("VHS3")
        self.assertEqual(self.refs(str(a.uuid)), ["VHS3"])
        self.assertEqual(self.call("get", f"/assets/resolve/?q=https://app.example/q/{a.uuid.hex}")["reference"], "VHS3")
        self.assertEqual(self.call("get", "/assets/resolve/?q=VHS5")["status"], "empty")

    def test_filters_and_summary(self):
        winter = self.first("VHS2").purpose_id
        self.assertEqual(self.refs(f"&purpose={winter}"), ["VHS2"])
        self.assertEqual(self.call("get", "/assets/?status=empty")["count"], 57)
        s = self.call("get", "/assets/summary/")
        self.assertEqual((s["total"], s["by_status"]["draft"], s["by_status"]["empty"]), (60, 3, 57))


class LabelTests(FindItTestCase):
    def test_pdf_pages_and_copies(self):
        self.bulk(22)
        ids = list(Asset.objects.values_list("id", flat=True))
        res = self.client.post(f"{BASE}/labels/pdf/", {"ids": ids}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        pdf = res.content
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page "), 2)  # 21 + 1
        res = self.client.post(f"{BASE}/labels/pdf/", {"ids": ids[:3], "copies": 2}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Asset.objects.count(), 22)  # copies never create boxes

    def test_cannot_print_other_households_boxes(self):
        self.bulk(1)
        stranger = self.as_user(self.stranger)
        res = stranger.post(f"{BASE}/labels/pdf/", {"ids": [Asset.objects.get().id]}, format="json")
        self.assertEqual(res.status_code, 400)


class HouseholdTests(FindItTestCase):
    def test_family_shares_boxes_and_history(self):
        hh = self.call("get", "/household/")
        self.assertTrue(hh["is_owner"])
        self.call("post", "/household/members/", {"email": "nobody@example.com"}, 400)
        res = self.call("post", "/household/members/", {"email": "mom@example.com"}, 201)
        self.assertEqual(len(res["members"]), 2)
        self.assertTrue(UserModuleAccess.objects.filter(user=self.mom, module="findit").exists())
        self.bulk(1)
        a = self.first()
        mom = self.as_user(self.mom)
        self.call("patch", f"/assets/{a.uuid}/", {"name": "Mom's box"}, client=mom)
        history = self.call("get", f"/assets/{a.uuid}/history/")
        self.assertEqual(history[0]["user_name"], "Mom")
        # Members cannot add others; owner cannot be removed.
        self.call("post", "/household/members/", {"email": "x@example.com"}, 403, client=mom)
        owner = HouseholdMember.objects.get(user=self.me)
        self.call("delete", f"/household/members/{owner.id}/", status=400)

    def test_cannot_take_someone_with_their_own_boxes(self):
        stranger = self.as_user(self.stranger)
        self.bulk(1, stranger)
        self.call("post", "/household/members/", {"email": "x@example.com"}, 400)
        # Someone who only opened FindIt once (no boxes) can join.
        services.household_for(self.mom)
        self.call("post", "/household/members/", {"email": "mom@example.com"}, 201)
