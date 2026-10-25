from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import services
from .models import Agreement, Charge, ElectricityBill, Payment, PaymentAllocation, RecurringCharge

TODAY = date(2026, 10, 25)
BASE = "/api/property-management"
D = Decimal


@override_settings(MEDIA_ROOT="test_media_pm")
class PMTestCase(TestCase):
    def setUp(self):
        patcher = mock.patch("django.utils.timezone.localdate", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        User = get_user_model()
        self.user = User.objects.create_user(email="owner@example.com", password="pass12345!")
        self.other = User.objects.create_user(email="other@example.com", password="pass12345!")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    # -- helpers ---------------------------------------------------------
    def post(self, path, data=None, status=201, fmt="json"):
        res = self.client.post(f"{BASE}{path}", data or {}, format=fmt)
        self.assertEqual(res.status_code, status, res.data if hasattr(res, "data") else res)
        return res.data

    def get(self, path, status=200):
        res = self.client.get(f"{BASE}{path}")
        self.assertEqual(res.status_code, status, getattr(res, "data", res))
        return res.data

    def make_property(self, name="Mahim House"):
        return self.post("/properties/", {"name": name, "address": "Mahim, Mumbai"})

    def make_floor(self, prop, name, level=0):
        return self.post("/floors/", {"property": prop["id"], "name": name, "level": level})

    def make_unit(self, prop, name, spaces=None):
        return self.post("/units/", {"property": prop["id"], "name": name, "spaces": spaces or []})

    def make_tenant(self, name="Tenant A", mobile="9876543210"):
        return self.post("/tenants/", {"full_name": name, "mobile": mobile})

    def make_agreement(self, prop, unit, tenant, rent="9000", start="2026-10-01", **extra):
        return self.post(
            "/agreements/",
            {"property": prop["id"], "unit": unit["id"], "tenant": tenant["id"], "monthly_rent": rent,
             "rent_due_day": 5, "start_date": start, "end_date": "2027-09-30", "security_deposit_amount": "20000", **extra},
        )

    def setup_basic(self, rent="9000"):
        self.prop = self.make_property()
        self.ground = self.make_floor(self.prop, "Ground", 0)
        self.unit = self.make_unit(self.prop, "G-1", [{"floor": self.ground["id"]}])
        self.tenant = self.make_tenant()
        self.agreement = self.make_agreement(self.prop, self.unit, self.tenant, rent=rent)
        return self.agreement

    def collection_row(self, agreement_id, month="2026-10"):
        data = self.get(f"/rent-collection/?month={month}")
        return next(r for r in data["rows"] if r["agreement_id"] == agreement_id)


class PropertyStructureTests(PMTestCase):
    def test_multiple_properties_and_units_across_floors(self):
        p1 = self.make_property("Mahim House")
        p2 = self.make_property("Shop Complex")
        g = self.make_floor(p1, "Ground", 0)
        f1 = self.make_floor(p1, "First", 1)
        g_a = self.post("/floor-sections/", {"floor": g["id"], "name": "Front"})
        f1_half = self.post("/floor-sections/", {"floor": f1["id"], "name": "East half"})
        unit = self.make_unit(p1, "Combined", [{"floor": g["id"], "section": g_a["id"]}, {"floor": f1["id"], "section": f1_half["id"]}])
        self.assertEqual(len(unit["spaces"]), 2)
        self.assertEqual(len(self.get("/properties/")["results"]), 2)
        self.assertEqual(self.get(f"/properties/{p2['id']}/")["unit_count"], 0)

    def test_section_must_belong_to_floor(self):
        p = self.make_property()
        g = self.make_floor(p, "Ground")
        f1 = self.make_floor(p, "First", 1)
        sec = self.post("/floor-sections/", {"floor": f1["id"], "name": "A"})
        self.post("/units/", {"property": p["id"], "name": "X", "spaces": [{"floor": g["id"], "section": sec["id"]}]}, status=400)

    def test_overlapping_active_occupancy_is_rejected(self):
        p = self.make_property()
        g = self.make_floor(p, "Ground")
        sec = self.post("/floor-sections/", {"floor": g["id"], "name": "Front"})
        whole = self.make_unit(p, "Whole ground", [{"floor": g["id"]}])
        part = self.make_unit(p, "Front shop", [{"floor": g["id"], "section": sec["id"]}])
        t1, t2 = self.make_tenant("A"), self.make_tenant("B", "9123456780")
        self.make_agreement(p, whole, t1)
        res = self.client.post(f"{BASE}/agreements/", {
            "property": p["id"], "unit": part["id"], "tenant": t2["id"], "monthly_rent": "5000", "start_date": "2026-10-01",
        }, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "occupancy_overlap")
        # Same unit twice is also rejected.
        res = self.client.post(f"{BASE}/agreements/", {
            "property": p["id"], "unit": whole["id"], "tenant": t2["id"], "monthly_rent": "5000", "start_date": "2026-10-01",
        }, format="json")
        self.assertEqual(res.status_code, 400)

    def test_unit_with_agreement_cannot_be_deleted(self):
        self.setup_basic()
        res = self.client.delete(f"{BASE}/units/{self.unit['id']}/")
        self.assertEqual(res.status_code, 400)
        res = self.client.delete(f"{BASE}/tenants/{self.tenant['id']}/")
        self.assertEqual(res.status_code, 400)


class PermissionTests(PMTestCase):
    def test_other_user_cannot_see_or_use_objects(self):
        self.setup_basic()
        other = APIClient()
        other.force_authenticate(self.other)
        self.assertEqual(other.get(f"{BASE}/properties/{self.prop['id']}/").status_code, 404)
        self.assertEqual(other.get(f"{BASE}/tenants/").data["results"], [])
        res = other.post(f"{BASE}/rent-payments/", {"agreement": self.agreement["id"], "amount": "100"}, format="json")
        self.assertEqual(res.status_code, 400)  # agreement not found for this user
        res = other.post(f"{BASE}/units/", {"property": self.prop["id"], "name": "Hijack"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(other.get(f"{BASE}/dashboard/").data["properties"], 0)

    def test_unauthenticated_is_rejected(self):
        self.assertEqual(APIClient().get(f"{BASE}/properties/").status_code, 401)

    def test_identity_number_masked_in_list(self):
        t = self.post("/tenants/", {"full_name": "X", "mobile": "9876543210", "id_type": "Aadhaar", "id_number": "123412341234"})
        self.assertEqual(t["id_number"], "123412341234")
        listed = self.get("/tenants/")["results"][0]
        self.assertTrue(listed["id_number"].endswith("1234"))
        self.assertNotIn("12341234", listed["id_number"])


class RentPaymentTests(PMTestCase):
    def test_full_and_partial_payments(self):
        self.setup_basic()
        row = self.collection_row(self.agreement["id"])
        self.assertEqual(row["amount_due"], D("9000.00"))
        self.assertEqual(row["status"], "overdue")  # due on the 5th, today is the 25th

        first = self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "4000", "charge_ids": row["open_charge_ids"]})
        row = self.collection_row(self.agreement["id"])
        self.assertEqual(row["paid"], D("4000.00"))
        self.assertEqual(row["balance"], D("5000.00"))  # default collection amount
        second = self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": str(row["balance"])})
        row = self.collection_row(self.agreement["id"])
        self.assertEqual(row["status"], "paid")
        self.assertEqual(row["balance"], D("0.00"))
        self.assertEqual(Payment.objects.count(), 2)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(Payment.objects.get(pk=first["id"]).amount, D("4000.00"))

    def test_partially_paid_status_before_due_date(self):
        self.setup_basic()
        Agreement.objects.filter(pk=self.agreement["id"]).update(rent_due_day=28)
        Charge.objects.all().delete()
        row = self.collection_row(self.agreement["id"])
        self.assertEqual(row["status"], "unpaid")
        self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "1000"})
        self.assertEqual(self.collection_row(self.agreement["id"])["status"], "partially_paid")

    def test_backdated_payment_and_future_date_rejected(self):
        self.setup_basic()
        p = self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "9000", "payment_date": "2026-10-02"})
        self.assertEqual(p["payment_date"], "2026-10-02")
        res = self.client.post(f"{BASE}/rent-payments/", {"agreement": self.agreement["id"], "amount": "1", "payment_date": "2026-12-01"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_payment_date_defaults_to_local_today(self):
        self.setup_basic()
        p = self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "100"})
        self.assertEqual(p["payment_date"], "2026-10-25")

    def test_negative_and_zero_payments_rejected(self):
        self.setup_basic()
        for amount in ("-5", "0"):
            res = self.client.post(f"{BASE}/rent-payments/", {"agreement": self.agreement["id"], "amount": amount}, format="json")
            self.assertEqual(res.status_code, 400)

    def test_overpayment_requires_confirmation_and_becomes_credit(self):
        self.setup_basic()
        self.collection_row(self.agreement["id"])
        res = self.client.post(f"{BASE}/rent-payments/", {"agreement": self.agreement["id"], "amount": "10000"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "overpayment_confirmation_required")
        self.assertEqual(res.data["excess"], "1000.00")
        self.assertEqual(Payment.objects.count(), 0)
        p = self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "10000", "excess_handling": "advance_credit"})
        self.assertEqual(p["unapplied_amount"], "1000.00")
        self.assertEqual(p["agreement_balance"]["advance_credit"], D("1000.00"))
        self.assertEqual(p["agreement_balance"]["net_balance"], D("-1000.00"))

        # Next month's charge can be settled from the credit.
        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 11, 3)):
            services.generate_charges(self.user, date(2026, 11, 1))
            services.apply_credit(Agreement.objects.get(pk=self.agreement["id"]))
            summary = services.balance_summary([self.agreement["id"]])
        self.assertEqual(summary["advance_credit"], D("0.00"))
        self.assertEqual(summary["total_outstanding"], D("8000.00"))

    def test_duplicate_submission_is_idempotent(self):
        self.setup_basic()
        body = {"agreement": self.agreement["id"], "amount": "9000", "idempotency_key": "abc-123"}
        first = self.post("/rent-payments/", body)
        res = self.client.post(f"{BASE}/rent-payments/", body, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["duplicate"])
        self.assertEqual(res.data["id"], first["id"])
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(PaymentAllocation.objects.count(), 1)
        res = self.client.post(f"{BASE}/rent-payments/", {**body, "amount": "5"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "idempotency_conflict")

    def test_payment_cannot_settle_same_charge_twice(self):
        self.setup_basic()
        row = self.collection_row(self.agreement["id"])
        self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "9000", "charge_ids": row["open_charge_ids"]})
        res = self.client.post(f"{BASE}/rent-payments/", {"agreement": self.agreement["id"], "amount": "10", "charge_ids": row["open_charge_ids"]}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "charge_settled")

    def test_reversal_restores_balance_and_keeps_record(self):
        self.setup_basic()
        p = self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "9000"})
        self.post(f"/rent-payments/{p['id']}/reverse/", {}, status=400)  # reason required
        r = self.post(f"/rent-payments/{p['id']}/reverse/", {"reason": "Entered twice"}, status=200)
        self.assertTrue(r["is_reversed"])
        self.assertEqual(self.collection_row(self.agreement["id"])["balance"], D("9000.00"))
        self.assertEqual(Payment.objects.count(), 1)
        self.post(f"/rent-payments/{p['id']}/reverse/", {"reason": "again"}, status=400)

    def test_payments_are_not_editable_or_deletable(self):
        self.setup_basic()
        p = self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "100"})
        self.assertEqual(self.client.patch(f"{BASE}/rent-payments/{p['id']}/", {"amount": "1"}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(f"{BASE}/rent-payments/{p['id']}/").status_code, 405)

    def test_adjustment_with_reason(self):
        self.setup_basic()
        row = self.collection_row(self.agreement["id"])
        cid = row["open_charge_ids"][0]
        self.post(f"/charges/{cid}/adjust/", {"amount": "-500", "reason": ""}, status=400)
        self.post(f"/charges/{cid}/adjust/", {"amount": "-500", "reason": "Discount for repairs"})
        self.assertEqual(self.collection_row(self.agreement["id"])["balance"], D("8500.00"))
        self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "8500"})
        self.post(f"/charges/{cid}/adjust/", {"amount": "-100", "reason": "x"}, status=400)  # below paid

    def test_month_history_summary(self):
        self.setup_basic()
        unit2 = self.make_unit(self.prop, "G-2")
        t2 = self.make_tenant("Tenant B", "9000000001")
        a2 = self.make_agreement(self.prop, unit2, t2, rent="8000")
        unit3 = self.make_unit(self.prop, "G-3")
        t3 = self.make_tenant("Tenant C", "9000000002")
        self.make_agreement(self.prop, unit3, t3, rent="7000")
        self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "9000"})
        self.post("/rent-payments/", {"agreement": a2["id"], "amount": "3000"})
        data = self.get("/rent-collection/?month=2026-10")
        s = data["summary"]
        self.assertEqual(s["expected"], D("24000.00"))
        self.assertEqual(s["collected"], D("12000.00"))
        self.assertEqual(s["remaining"], D("12000.00"))
        self.assertEqual((s["paid"], s["overdue"]), (1, 2))
        report = self.get("/reports/monthly/?month=2026-10")
        self.assertEqual(len(report["rent"]["paid_tenants"]), 1)
        self.assertEqual(len(report["rent"]["overdue_tenants"]), 2)


class AgreementLifecycleTests(PMTestCase):
    def test_rent_revision_does_not_change_past_charges(self):
        self.setup_basic()
        self.collection_row(self.agreement["id"])  # generates October at 9000
        rev = self.post(f"/agreements/{self.agreement['id']}/rent-revisions/",
                        {"new_rent": "9500", "effective_date": "2026-11-01", "reason": "Annual increase"})
        self.assertEqual(rev["previous_rent"], "9000.00")
        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 11, 10)):
            services.generate_charges(self.user, date(2026, 11, 1))
        rents = dict(Charge.objects.filter(charge_type="rent").values_list("period", "amount"))
        self.assertEqual(rents[date(2026, 10, 1)], D("9000.00"))
        self.assertEqual(rents[date(2026, 11, 1)], D("9500.00"))
        self.post(f"/agreements/{self.agreement['id']}/rent-revisions/",
                  {"new_rent": "9900", "effective_date": "2026-11-15"}, status=400)  # same month

    def test_renewal_creates_new_agreement_and_preserves_old(self):
        self.setup_basic()
        self.post("/security-deposits/", {"agreement": self.agreement["id"], "txn_type": "received", "amount": "20000", "txn_date": "2026-10-01"})
        self.collection_row(self.agreement["id"])
        new = self.post(f"/agreements/{self.agreement['id']}/renew/", {"start_date": "2026-10-15", "monthly_rent": "10000"})
        old = self.get(f"/agreements/{self.agreement['id']}/")
        self.assertEqual(old["status"], "renewed")
        self.assertEqual(new["renewed_from"], self.agreement["id"])
        # October was already billed on the old agreement, so the new one starts in November.
        self.assertEqual(new["billing_start_date"], "2026-11-01")
        self.assertEqual(services.deposit_balance([new["id"]])["balance"], D("20000.00"))
        self.assertEqual(services.deposit_balance([old["id"]])["balance"], D("0.00"))
        renewed = self.get("/agreements/?expiry=renewed")["results"]
        self.assertEqual([a["id"] for a in renewed], [old["id"]])

    def test_move_out_preserves_history_and_allows_new_tenant(self):
        self.setup_basic()
        self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "9000"})
        ended = self.post(f"/agreements/{self.agreement['id']}/end/", {"move_out_date": "2026-10-31"}, status=200)
        self.assertEqual(ended["status"], "ended")
        self.assertEqual(self.get(f"/tenants/{self.tenant['id']}/")["status"], "former")
        t2 = self.make_tenant("New Tenant", "9000000009")
        a2 = self.make_agreement(self.prop, self.unit, t2, rent="9500", start="2026-11-01", billing_start_date="2026-11-01")
        self.assertNotEqual(a2["id"], self.agreement["id"])
        summary = self.get(f"/tenants/{self.tenant['id']}/summary/")
        self.assertEqual(len(summary["previous_agreements"]), 1)
        self.assertEqual(len(summary["payments"]), 1)
        self.assertEqual(summary["balance"]["total_outstanding"], D("0.00"))

    def test_expiry_tracking_and_reminders_without_duplicates(self):
        self.setup_basic()
        Agreement.objects.filter(pk=self.agreement["id"]).update(end_date=date(2026, 11, 20))
        self.assertEqual(self.get(f"/agreements/{self.agreement['id']}/")["expiry_state"], "expiring_soon")
        self.assertEqual(self.get("/reminders/")["results"], [])  # disabled by default
        self.client.put(f"{BASE}/reminder-settings/", {"enabled": True, "days_before": 30}, format="json")
        self.get("/reminders/")
        self.get("/dashboard/")
        reminders = self.get("/reminders/")["results"]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["days_before"], 30)
        Agreement.objects.filter(pk=self.agreement["id"]).update(end_date=date(2026, 10, 1))
        self.assertEqual(self.get(f"/agreements/{self.agreement['id']}/")["expiry_state"], "expired")
        self.assertEqual(len(self.get("/agreements/?expiry=expired")["results"]), 1)

    def test_agreement_with_history_cannot_be_deleted(self):
        self.setup_basic()
        self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "100"})
        res = self.client.delete(f"{BASE}/agreements/{self.agreement['id']}/")
        self.assertEqual(res.status_code, 400)

    def test_billing_start_skips_months_before_app_use(self):
        p = self.make_property()
        u = self.make_unit(p, "U1")
        t = self.make_tenant()
        a = self.make_agreement(p, u, t, start="2025-04-01")
        self.assertEqual(a["billing_start_date"], "2026-10-01")
        self.get("/dashboard/")
        self.assertEqual(Charge.objects.filter(charge_type="rent").count(), 1)


class MultiUnitAgreementTests(PMTestCase):
    def setUp(self):
        super().setUp()
        self.prop = self.make_property()
        self.ground = self.make_floor(self.prop, "Ground", 0)
        self.first = self.make_floor(self.prop, "First", 1)
        self.g1 = self.make_unit(self.prop, "G-1", [{"floor": self.ground["id"]}])
        self.f1 = self.make_unit(self.prop, "F-1", [{"floor": self.first["id"]}])
        self.tenant = self.make_tenant()

    def test_one_agreement_covers_several_units(self):
        a = self.make_agreement(self.prop, self.g1, self.tenant, rent="15000", additional_units=[self.f1["id"]])
        self.assertEqual(a["additional_units"], [self.f1["id"]])
        self.assertEqual([u["name"] for u in a["units"]], ["G-1", "F-1"])
        self.assertEqual(a["unit_name"], "G-1 + F-1")
        # One rent charge for the whole agreement, not one per unit.
        row = self.collection_row(a["id"])
        self.assertEqual(row["unit_name"], "G-1 + F-1")
        self.assertEqual(D(row["amount_due"]), D("15000"))
        # Both units show as occupied by this tenant.
        units = {u["name"]: u for u in self.get(f"/units/?property={self.prop['id']}")["results"]}
        self.assertEqual(units["F-1"]["current_agreement"]["id"], a["id"])
        self.assertEqual(self.get(f"/properties/{self.prop['id']}/")["occupied_count"], 2)
        self.assertEqual(self.get("/dashboard/?month=2026-10")["occupied_units"], 2)
        self.assertEqual(len(self.get(f"/agreements/?unit={self.f1['id']}")["results"]), 1)

    def test_additional_unit_cannot_be_double_let(self):
        self.make_agreement(self.prop, self.g1, self.tenant, additional_units=[self.f1["id"]])
        other = self.make_tenant("Tenant B", "9876543211")
        res = self.client.post(f"{BASE}/agreements/", {
            "property": self.prop["id"], "unit": self.f1["id"], "tenant": other["id"], "monthly_rent": "5000",
            "start_date": "2026-10-01"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "occupancy_overlap")

    def test_validation_and_adding_unit_later(self):
        res = self.client.post(f"{BASE}/agreements/", {
            "property": self.prop["id"], "unit": self.g1["id"], "tenant": self.tenant["id"], "monthly_rent": "5000",
            "start_date": "2026-10-01", "additional_units": [self.g1["id"]]}, format="json")
        self.assertEqual(res.status_code, 400)
        other_prop = self.make_property("Elsewhere")
        far = self.make_unit(other_prop, "X-1")
        res = self.client.post(f"{BASE}/agreements/", {
            "property": self.prop["id"], "unit": self.g1["id"], "tenant": self.tenant["id"], "monthly_rent": "5000",
            "start_date": "2026-10-01", "additional_units": [far["id"]]}, format="json")
        self.assertEqual(res.status_code, 400)
        a = self.make_agreement(self.prop, self.g1, self.tenant)
        res = self.client.patch(f"{BASE}/agreements/{a['id']}/", {"additional_units": [self.f1["id"]]}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["unit_name"], "G-1 + F-1")

    def test_renewal_keeps_all_units(self):
        a = self.make_agreement(self.prop, self.g1, self.tenant, additional_units=[self.f1["id"]])
        new = self.post(f"/agreements/{a['id']}/renew/", {"start_date": "2027-10-01"})
        self.assertEqual(new["additional_units"], [self.f1["id"]])

    def test_unit_scoped_fixed_charge_applies_to_additional_unit(self):
        a = self.make_agreement(self.prop, self.g1, self.tenant, additional_units=[self.f1["id"]])
        self.post("/recurring-charges/", {"name": "Water", "amount": "200", "start_date": "2026-10-01", "unit": self.f1["id"]})
        row = self.collection_row(a["id"])
        self.assertEqual(D(row["amount_due"]), D("9200"))


class RecurringChargeTests(PMTestCase):
    def test_fixed_charges_generated_once_and_included_in_monthly_due(self):
        self.setup_basic()
        self.post("/recurring-charges/", {"name": "Water", "amount": "300", "start_date": "2026-01-01", "agreement": self.agreement["id"]})
        services.generate_charges(self.user)
        services.generate_charges(self.user)
        self.get("/rent-collection/?month=2026-10")
        self.assertEqual(Charge.objects.filter(charge_type="fixed").count(), 1)
        row = self.collection_row(self.agreement["id"])
        self.assertEqual(row["amount_due"], D("9300.00"))

    def test_property_scope_and_revision_keeps_history(self):
        self.setup_basic()
        rc = self.post("/recurring-charges/", {"name": "Maintenance", "amount": "200", "start_date": "2026-10-01", "property": self.prop["id"]})
        services.generate_charges(self.user)
        self.client.patch(f"{BASE}/recurring-charges/{rc['id']}/", {"amount": "250"}, format="json")
        self.assertEqual(RecurringCharge.objects.get(pk=rc["id"]).amount, D("200.00"))  # locked after generation
        new = self.post(f"/recurring-charges/{rc['id']}/revise/", {"amount": "250", "effective_date": "2026-11-01"})
        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 11, 2)):
            services.generate_charges(self.user, date(2026, 11, 1))
        amounts = dict(Charge.objects.filter(charge_type="fixed").values_list("period", "amount"))
        self.assertEqual(amounts, {date(2026, 10, 1): D("200.00"), date(2026, 11, 1): D("250.00")})
        self.assertEqual(new["revised_from"], rc["id"])

    def test_disabled_and_quarterly(self):
        self.setup_basic()
        self.post("/recurring-charges/", {"name": "Off", "amount": "50", "start_date": "2026-10-01", "agreement": self.agreement["id"], "is_enabled": False})
        self.post("/recurring-charges/", {"name": "Quarterly", "amount": "900", "frequency": 3, "start_date": "2026-09-01", "agreement": self.agreement["id"]})
        services.generate_charges(self.user)
        self.assertFalse(Charge.objects.filter(charge_type="fixed").exists())  # Oct is not a quarter month from Sep

    def test_scope_validation(self):
        self.setup_basic()
        self.post("/recurring-charges/", {"name": "x", "amount": "1", "start_date": "2026-10-01"}, status=400)
        self.post("/recurring-charges/", {"name": "x", "amount": "1", "start_date": "2026-10-01",
                                          "property": self.prop["id"], "unit": self.unit["id"]}, status=400)

    def test_one_time_charge(self):
        self.setup_basic()
        c = self.post("/charges/", {"agreement": self.agreement["id"], "description": "Key copy", "amount": "150", "due_date": "2026-10-30"})
        self.assertEqual(c["charge_type"], "one_time")
        self.assertEqual(services.balance_summary([self.agreement["id"]])["one_time"], D("150.00"))


class ExpenseTests(PMTestCase):
    def test_recoverable_expense_flow(self):
        self.setup_basic()
        e = self.post("/expenses/", {"property": self.prop["id"], "agreement": self.agreement["id"], "title": "Plumbing repair",
                                     "amount": "800", "expense_date": "2026-10-20", "category": "plumbing",
                                     "is_recoverable": True, "recoverable_amount": "500"})
        self.assertEqual(e["recovery"]["status"], "pending")
        self.assertEqual(e["unit"], self.unit["id"])
        bal = services.balance_summary([self.agreement["id"]])
        self.assertEqual(bal["expense"], D("500.00"))
        expense_charge = Charge.objects.get(charge_type="expense")
        self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "200", "charge_ids": [expense_charge.id]})
        e = self.get(f"/expenses/{e['id']}/")
        self.assertEqual(e["recovery"]["status"], "partially_recovered")
        self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "300", "charge_ids": [expense_charge.id]})
        self.assertEqual(self.get(f"/expenses/{e['id']}/")["recovery"]["status"], "recovered")
        dash = self.get("/dashboard/?month=2026-10")
        self.assertEqual(dash["property_expenses"], D("800.00"))  # counted once, as the owner's cost
        self.assertEqual(dash["recoverable_expenses_outstanding"], D("0.00"))
        # Cannot reduce recoverable amount below what has been recovered.
        res = self.client.patch(f"{BASE}/expenses/{e['id']}/", {"recoverable_amount": "100"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_non_recoverable_expense_does_not_charge_tenant(self):
        self.setup_basic()
        self.post("/expenses/", {"property": self.prop["id"], "agreement": self.agreement["id"], "title": "Paint",
                                 "amount": "800", "expense_date": "2026-10-20"})
        self.assertFalse(Charge.objects.filter(charge_type="expense").exists())

    def test_recoverable_more_than_amount_rejected(self):
        self.setup_basic()
        self.post("/expenses/", {"property": self.prop["id"], "agreement": self.agreement["id"], "title": "x",
                                 "amount": "100", "expense_date": "2026-10-20", "is_recoverable": True, "recoverable_amount": "200"}, status=400)

    def test_receipt_upload_and_validation(self):
        self.setup_basic()
        good = SimpleUploadedFile("r.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        e = self.post("/expenses/", {"property": self.prop["id"], "title": "Lock", "amount": "300",
                                     "expense_date": "2026-10-20", "receipt": good}, fmt="multipart")
        self.assertTrue(e["receipt_url"].endswith(f"/expenses/{e['id']}/receipt/"))
        self.assertEqual(self.client.get(e["receipt_url"]).status_code, 200)
        other = APIClient()
        other.force_authenticate(self.other)
        self.assertEqual(other.get(e["receipt_url"]).status_code, 404)
        # Signed link works without auth, tampered link does not.
        self.assertEqual(APIClient().get(e["receipt_open_url"]).status_code, 200)
        self.assertEqual(APIClient().get(e["receipt_open_url"][:-3] + "xx/").status_code, 403)
        bad = SimpleUploadedFile("r.exe", b"MZ", content_type="application/octet-stream")
        self.post("/expenses/", {"property": self.prop["id"], "title": "Bad", "amount": "1",
                                 "expense_date": "2026-10-20", "receipt": bad}, fmt="multipart", status=400)


class ElectricityTests(PMTestCase):
    def setUp(self):
        super().setUp()
        self.prop = self.make_property()
        self.agreements = []
        for i, name in enumerate(["A", "B", "C"]):
            unit = self.make_unit(self.prop, f"U{i}")
            tenant = self.make_tenant(f"Tenant {name}", f"900000000{i}")
            self.agreements.append(self.make_agreement(self.prop, unit, tenant))
        self.ids = [a["id"] for a in self.agreements]

    def bill(self, amount="3000", **extra):
        return self.post("/electricity/bills/", {"property": self.prop["id"], "period": "2026-10-15", "bill_date": "2026-10-20",
                                                 "due_date": "2026-10-30", "amount": amount, **extra})

    def allocate(self, bill, status=200, **body):
        res = self.client.post(f"{BASE}/electricity/bills/{bill['id']}/allocate/", body, format="json")
        self.assertEqual(res.status_code, status, res.data)
        return res.data

    def amounts(self):
        return [D(c.amount) for c in Charge.objects.filter(charge_type="electricity").order_by("agreement_id")]

    def test_equal_split(self):
        b = self.bill()
        self.assertEqual(b["period"], "2026-10-01")
        self.allocate(b, method="equal", agreement_ids=self.ids)
        self.assertEqual(self.amounts(), [D("1000.00")] * 3)
        self.allocate(b, method="equal", agreement_ids=self.ids, status=400)  # no duplicate allocation

    def test_equal_split_only_selected_agreements_and_rounding(self):
        b = self.bill("1000")
        data = self.allocate(b, method="equal", agreement_ids=self.ids)
        amounts = self.amounts()
        self.assertEqual(sum(amounts), D("1000.00"))
        self.assertEqual(sorted(amounts), [D("333.33"), D("333.33"), D("333.34")])
        self.assertEqual(data["preview"]["total"], "1000.00")
        b2 = self.bill("500")
        self.allocate(b2, method="equal", agreement_ids=self.ids[:2])
        self.assertEqual(Charge.objects.filter(electricity_allocation__bill_id=b2["id"]).count(), 2)

    def test_meter_based_split(self):
        b = self.bill(total_units="300")
        lines = {str(self.ids[0]): {"units": "100"}, str(self.ids[1]): {"units": "150"}, str(self.ids[2]): {"units": "50"}}
        preview = self.allocate(b, method="meter", agreement_ids=self.ids, lines=lines, preview=True)
        self.assertEqual([l["amount"] for l in preview["lines"]], ["1000.00", "1500.00", "500.00"])
        self.assertFalse(ElectricityBill.objects.get(pk=b["id"]).allocations.exists())  # preview only
        self.allocate(b, method="meter", agreement_ids=self.ids, lines=lines)
        self.assertEqual(self.amounts(), [D("1000.00"), D("1500.00"), D("500.00")])

    def test_meter_units_mismatch_needs_confirmation(self):
        b = self.bill(total_units="400")
        lines = {str(i): {"units": "100"} for i in self.ids}
        data = self.allocate(b, method="meter", agreement_ids=self.ids, lines=lines, status=400)
        self.assertEqual(data["code"], "confirmation_required")
        self.allocate(b, method="meter", agreement_ids=self.ids, lines=lines, confirm=True)
        self.assertEqual(sum(self.amounts()), D("3000.00"))

    def test_zero_units_rejected(self):
        b = self.bill()
        lines = {str(i): {"units": "0"} for i in self.ids}
        data = self.allocate(b, method="meter", agreement_ids=self.ids, lines=lines, status=400)
        self.assertEqual(data["code"], "zero_units")

    def test_manual_allocation_validation(self):
        b = self.bill()
        over = {str(self.ids[0]): {"amount": "2000"}, str(self.ids[1]): {"amount": "2000"}}
        self.assertEqual(self.allocate(b, method="manual", agreement_ids=self.ids[:2], lines=over, status=400)["code"], "over_allocated")
        under = {str(self.ids[0]): {"amount": "800"}, str(self.ids[1]): {"amount": "1200"}, str(self.ids[2]): {"amount": "900"}}
        data = self.allocate(b, method="manual", agreement_ids=self.ids, lines=under, status=400)
        self.assertEqual(data["preview"]["difference"], "100.00")
        self.allocate(b, method="manual", agreement_ids=self.ids, lines=under, confirm=True)
        self.assertEqual(ElectricityBill.objects.get(pk=b["id"]).unallocated_amount, D("100.00"))

    def test_shared_meter_with_readings(self):
        meter = self.post("/electricity/meters/", {"property": self.prop["id"], "meter_number": "M-1", "name": "Shared", "meter_type": "shared"})
        solo = self.post("/electricity/meters/", {"property": self.prop["id"], "meter_number": "M-2", "name": "C's meter"})
        self.post("/electricity/meter-assignments/", {"meter": meter["id"], "agreement": self.ids[0], "start_date": "2026-10-01", "share_percent": "60"})
        self.post("/electricity/meter-assignments/", {"meter": meter["id"], "agreement": self.ids[1], "start_date": "2026-10-01"})
        self.post("/electricity/meter-assignments/", {"meter": solo["id"], "agreement": self.ids[2], "start_date": "2026-10-01"})
        self.post("/electricity/readings/", {"meter": meter["id"], "reading_date": "2026-09-30", "previous_reading": "1000", "current_reading": "1000"})
        r = self.post("/electricity/readings/", {"meter": meter["id"], "reading_date": "2026-10-31", "current_reading": "1200"})
        self.assertEqual(r["units_consumed"], "200.00")
        self.post("/electricity/readings/", {"meter": solo["id"], "reading_date": "2026-10-31", "previous_reading": "0", "current_reading": "100"})
        b = self.bill()
        # B has no share percent on the shared meter, so its units must be entered.
        data = self.allocate(b, method="meter", agreement_ids=self.ids, use_readings=True, preview=True, status=400)
        self.assertEqual(data["code"], "units_required")
        self.assertEqual(data["agreement_ids"], [self.ids[1]])
        lines = {str(self.ids[1]): {"units": "80"}}
        preview = self.allocate(b, method="meter", agreement_ids=self.ids, use_readings=True, lines=lines, preview=True)
        self.assertEqual([l["units"] for l in preview["lines"]], ["120.00", "80.00", "100.00"])
        self.allocate(b, method="meter", agreement_ids=self.ids, use_readings=True, lines=lines)
        self.assertEqual(self.amounts(), [D("1200.00"), D("800.00"), D("1000.00")])
        # One provider bill, three separate tenant charges.
        self.assertEqual(ElectricityBill.objects.count(), 1)

    def test_reading_validation_and_reset(self):
        meter = self.post("/electricity/meters/", {"property": self.prop["id"], "meter_number": "M-9", "name": "M"})
        self.post("/electricity/readings/", {"meter": meter["id"], "reading_date": "2026-09-30", "previous_reading": "500", "current_reading": "600"})
        self.post("/electricity/readings/", {"meter": meter["id"], "reading_date": "2026-10-31", "current_reading": "550"}, status=400)
        self.post("/electricity/readings/", {"meter": meter["id"], "reading_date": "2026-10-31", "current_reading": "20", "is_reset": True}, status=400)
        r = self.post("/electricity/readings/", {"meter": meter["id"], "reading_date": "2026-10-31", "current_reading": "20",
                                                 "is_reset": True, "reset_reason": "Meter replaced"})
        self.assertEqual(r["units_consumed"], "20.00")

    def test_allocation_correction_keeps_audit(self):
        b = self.bill()
        self.allocate(b, method="equal", agreement_ids=self.ids)
        alloc = self.get(f"/electricity/allocations/?bill={b['id']}")["results"][0]
        data = self.post(f"/electricity/allocations/{alloc['id']}/correct/", {"amount": "900", "reason": "Meter misread"}, status=200)
        self.assertEqual(data["allocation"]["amount"], "1000.00")  # original kept
        self.assertEqual(data["allocation"]["charge"]["net_amount"], "900.00")
        self.assertEqual(data["bill_allocated_total"], "2900.00")
        self.assertEqual(services.balance_summary([alloc["agreement"]])["electricity"], D("900.00"))

    def test_electricity_payment_with_rent(self):
        b = self.bill()
        self.allocate(b, method="equal", agreement_ids=self.ids)
        aid = self.ids[0]
        self.get("/rent-collection/?month=2026-10")
        self.post("/rent-payments/", {"agreement": aid, "amount": "10000"})
        bal = services.balance_summary([aid])
        self.assertEqual(bal["total_outstanding"], D("0.00"))


class UnifiedBalanceTests(PMTestCase):
    def test_example_from_spec(self):
        self.setup_basic()
        self.post("/recurring-charges/", {"name": "Water", "amount": "300", "start_date": "2026-10-01", "agreement": self.agreement["id"]})
        b = self.post("/electricity/bills/", {"property": self.prop["id"], "period": "2026-10-01", "bill_date": "2026-10-20", "amount": "1000"})
        self.client.post(f"{BASE}/electricity/bills/{b['id']}/allocate/", {"method": "equal", "agreement_ids": [self.agreement["id"]]}, format="json")
        self.post("/expenses/", {"property": self.prop["id"], "agreement": self.agreement["id"], "title": "Plumbing",
                                 "amount": "800", "expense_date": "2026-10-20", "is_recoverable": True})
        self.get("/rent-collection/?month=2026-10")
        rent_charge = Charge.objects.get(charge_type="rent")
        self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "9000", "charge_ids": [rent_charge.id]})
        summary = self.get(f"/tenants/{self.tenant['id']}/summary/")["balance"]
        self.assertEqual(summary["total_charges"], D("11100.00"))
        self.assertEqual(summary["total_paid"], D("9000.00"))
        self.assertEqual(summary["total_outstanding"], D("2100.00"))
        self.assertEqual((summary["rent"], summary["fixed"], summary["electricity"], summary["expense"]),
                         (D("0.00"), D("300.00"), D("1000.00"), D("800.00")))

    def test_default_allocation_order_oldest_then_rent_first(self):
        self.setup_basic()
        self.post("/recurring-charges/", {"name": "Water", "amount": "300", "start_date": "2026-10-01", "agreement": self.agreement["id"]})
        self.get("/rent-collection/?month=2026-10")
        p = self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "9100"})
        types = [(a["charge_type"], a["amount"]) for a in p["allocations"]]
        self.assertEqual(types, [("rent", "9000.00"), ("fixed", "100.00")])


class DepositTests(PMTestCase):
    def test_settlement(self):
        self.setup_basic()
        self.post("/security-deposits/", {"agreement": self.agreement["id"], "txn_type": "received", "amount": "20000", "txn_date": "2026-10-01"})
        self.post("/security-deposits/", {"agreement": self.agreement["id"], "txn_type": "refunded", "amount": "1", "txn_date": "2026-10-01"}, status=400)
        self.get("/rent-collection/?month=2026-10")
        data = self.post("/security-deposits/settle/", {"agreement": self.agreement["id"], "apply_to_dues": "9000",
                                                       "deduction": "1000", "deduction_reason": "Broken window", "refund": "10000"})
        self.assertEqual(data["deposit"]["balance"], D("0.00"))
        self.assertEqual(data["balance"]["total_outstanding"], D("0.00"))
        self.post("/security-deposits/settle/", {"agreement": self.agreement["id"], "refund": "1"}, status=400)


class MoneyHelperTests(TestCase):
    def test_distribute_always_sums_exactly(self):
        for total, weights in [("100", [1, 1, 1]), ("3000", [100, 150, 50]), ("0.05", [1, 1, 1, 1]), ("999.99", [3, 7])]:
            parts = services.distribute(D(total), weights)
            self.assertEqual(sum(parts), D(total))

    def test_distribute_zero_weight(self):
        with self.assertRaises(services.ServiceError):
            services.distribute(D("10"), [0, 0])


class DashboardTests(PMTestCase):
    def test_dashboard_numbers(self):
        self.setup_basic()
        self.make_unit(self.prop, "Vacant")
        self.post("/rent-payments/", {"agreement": self.agreement["id"], "amount": "4000"})
        d = self.get(f"/dashboard/?month=2026-10&property={self.prop['id']}")
        self.assertEqual((d["properties"], d["units"], d["occupied_units"], d["vacant_units"]), (1, 2, 1, 1))
        self.assertEqual(d["expected_rent"], D("9000.00"))
        self.assertEqual(d["rent_collected"], D("4000.00"))
        self.assertEqual(d["rent_remaining"], D("5000.00"))
        self.assertEqual(d["tenant_status_counts"]["overdue"], 1)
        overview = self.get(f"/properties/{self.prop['id']}/overview/")
        self.assertEqual(len(overview["units"]), 2)
