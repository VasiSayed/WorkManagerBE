"""App modules a user can be given access to.

The frontend shows only a user's modules. The backend also blocks the API of modules whose endpoints
stand alone (ENFORCED_PREFIXES). Work, HR, People and Settings share endpoints with each other
(e.g. the task form reads projects and employees), and their data is private to each user anyway,
so for those the module list only controls what the app shows.
"""

MODULES = [
    ("work", "Work"),
    ("hr", "HR"),
    ("expense", "Expense"),
    ("rental", "Rental"),
    ("findit", "FindIt"),
    ("people", "People"),
    ("settings", "Settings"),
]
MODULE_KEYS = [key for key, _ in MODULES]

# API path prefix -> module that must be granted to use it.
ENFORCED_PREFIXES = [
    ("/api/findit/", "findit"),
    ("/api/property-management/", "rental"),
    ("/api/expenses/", "expense"),
    ("/api/expense-categories/", "expense"),
    ("/api/expense-contacts/", "expense"),
    ("/api/expense-tags/", "expense"),
]


def is_admin(user):
    """Admins manage who gets which module and always have every module."""
    return bool(user and user.is_authenticated and (user.is_superuser or user.is_staff))


def user_modules(user):
    if not user or not user.is_authenticated:
        return []
    if is_admin(user):
        return list(MODULE_KEYS)
    granted = set(user.module_access.values_list("module", flat=True))
    return [key for key in MODULE_KEYS if key in granted]


def module_for_path(path):
    for prefix, module in ENFORCED_PREFIXES:
        if path.startswith(prefix):
            return module
    return None
