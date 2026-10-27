from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import ExpenseTag

D = Decimal


class ExpenseLedgerTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(email="me@example.com", password="pass12345!")
        self.other = User.objects.create_user(email="other@example.com", password="pass12345!")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def post(self, path, data, status=201):
        res = self.client.post(f"/api{path}", data, format="json")
        self.assertEqual(res.status_code, status, getattr(res, "data", res))
        return res.data

    def get(self, path):
        res = self.client.get(f"/api{path}")
        self.assertEqual(res.status_code, 200, getattr(res, "data", res))
        return res.data

    def entry(self, kind, amount, date, **extra):
        return self.post("/expenses/", {"transaction_type": kind, "amount": amount, "date": date, **extra})

    def test_categories_with_sub_categories(self):
        home = self.post("/expense-categories/", {"name": "Home"})
        bills = self.post("/expense-categories/", {"name": "Bills", "parent": home["id"]})
        self.assertEqual(bills["parent_name"], "Home")
        # Same sub-category name under another category is fine, but not twice under one.
        car = self.post("/expense-categories/", {"name": "Car"})
        self.post("/expense-categories/", {"name": "Bills", "parent": car["id"]})
        self.post("/expense-categories/", {"name": "bills", "parent": home["id"]}, status=400)
        # Only two levels.
        self.post("/expense-categories/", {"name": "Water", "parent": bills["id"]}, status=400)
        top = self.get("/expense-categories/?top=true")
        self.assertEqual(sorted(c["name"] for c in top), ["Car", "Home"])

    def test_sub_category_sets_main_category(self):
        home = self.post("/expense-categories/", {"name": "Home"})
        bills = self.post("/expense-categories/", {"name": "Bills", "parent": home["id"]})
        car = self.post("/expense-categories/", {"name": "Car"})
        e = self.entry("given", "500", "2026-10-01", sub_category=bills["id"])
        self.assertEqual((e["category"], e["sub_category"]), (home["id"], bills["id"]))
        # Picking a sub-category in the category box is stored properly too.
        e2 = self.entry("given", "50", "2026-10-02", category=bills["id"])
        self.assertEqual((e2["category"], e2["sub_category"]), (home["id"], bills["id"]))
        res = self.client.post("/api/expenses/", {"transaction_type": "given", "amount": "10", "category": car["id"],
                                                   "sub_category": bills["id"]}, format="json")
        self.assertEqual(res.status_code, 400)
        # Filtering by the main category includes its sub-categories.
        self.assertEqual(self.get(f"/expenses/?category={home['id']}")["count"], 2)
        self.assertEqual(self.get(f"/expenses/?sub_category={bills['id']}")["count"], 2)

    def test_contacts_are_private_and_show_totals(self):
        ravi = self.post("/expense-contacts/", {"name": "Ravi", "phone": "98765"})
        self.post("/expense-contacts/", {"name": "ravi"}, status=400)
        self.entry("given", "1000", "2026-10-01", contact=ravi["id"])
        self.entry("received", "400", "2026-10-05", contact=ravi["id"])
        contacts = self.get("/expense-contacts/")
        self.assertEqual((contacts[0]["given"], contacts[0]["received"], contacts[0]["entries"]), ("1000.00", "400.00", 2))
        other = APIClient()
        other.force_authenticate(self.other)
        self.assertEqual(other.get("/api/expense-contacts/").data, [])
        res = other.post("/api/expenses/", {"transaction_type": "given", "amount": "5", "contact": ravi["id"]}, format="json")
        self.assertEqual(res.status_code, 400)
        # The old endpoint that listed every registered user is gone.
        self.assertEqual(self.client.get("/api/users/").status_code, 404)

    def test_tags_typed_freely_are_created_per_user(self):
        e = self.entry("given", "300", "2026-10-01", tag_names=["Trip", "goa"])
        self.assertEqual(sorted(t["name"] for t in e["tag_list"]), ["Trip", "goa"])
        e2 = self.entry("given", "200", "2026-10-02", tag_names=["trip"])
        self.assertEqual([t["name"] for t in e2["tag_list"]], ["Trip"])
        self.assertEqual(ExpenseTag.objects.filter(owner=self.user).count(), 2)
        trip = ExpenseTag.objects.get(owner=self.user, name="Trip")
        self.assertEqual(self.get(f"/expenses/?tag={trip.id}")["count"], 2)
        tags = {t["name"]: t["entries"] for t in self.get("/expense-tags/")}
        self.assertEqual(tags, {"Trip": 2, "goa": 1})

    def test_running_balance_filters_and_summary(self):
        food = self.post("/expense-categories/", {"name": "Food"})
        ravi = self.post("/expense-contacts/", {"name": "Ravi"})
        self.entry("received", "5000", "2026-09-30", name="Salary")
        self.entry("given", "1200", "2026-10-02", category=food["id"], name="Groceries")
        self.entry("given", "800", "2026-10-03", contact=ravi["id"], name="Loan to Ravi")
        self.entry("received", "300", "2026-10-10", contact=ravi["id"], name="Ravi paid back")

        rows = self.get("/expenses/")["results"]
        # Newest first, each with the balance after that entry.
        self.assertEqual([r["balance"] for r in rows], ["3300.00", "3000.00", "3800.00", "5000.00"])
        self.assertEqual(self.get("/expenses/?month=2026-10")["count"], 3)
        self.assertEqual(self.get("/expenses/?type=received")["count"], 2)
        self.assertEqual(self.get("/expenses/?min_amount=1000")["count"], 2)
        self.assertEqual(self.get("/expenses/?date_from=2026-10-03&date_to=2026-10-31")["count"], 2)
        self.assertEqual(self.get("/expenses/?search=ravi")["count"], 2)
        ravi_rows = self.get(f"/expenses/?contact={ravi['id']}")["results"]
        self.assertEqual([r["balance"] for r in ravi_rows], ["-500.00", "-800.00"])

        s = self.get("/expenses/summary/?month=2026-10")
        self.assertEqual((s["given"], s["received"], s["net"], s["count"]), ("2000.00", "300.00", "-1700.00", 3))
        by_contact = {c["name"]: c["net"] for c in s["by_contact"]}
        self.assertEqual(by_contact, {"Ravi": "-500.00", "": "-1200.00"})
        self.assertEqual({c["name"]: c["given"] for c in s["by_category"]}["Food"], "1200.00")

    def test_page_size_can_be_raised(self):
        for i in range(60):
            self.entry("given", "1", "2026-10-01")
        self.assertEqual(len(self.get("/expenses/")["results"]), 50)
        self.assertEqual(len(self.get("/expenses/?page_size=100")["results"]), 60)
