"""Shared helpers for pm_export_data / pm_import_data.

Every row is exported with its primary key so relations stay intact. The owner is
written as an email and re-linked to the user with that email on the target server.
"""

from django.apps import apps
from django.db import models

APP_LABEL = "property_management"
FORMAT_VERSION = 1


def ordered_models():
    """App models in an order where every FK target is loaded before the rows that point to it."""
    pending = list(apps.get_app_config(APP_LABEL).get_models())
    ordered = []
    while pending:
        progressed = False
        for model in list(pending):
            deps = {
                f.related_model
                for f in model._meta.fields
                if isinstance(f, models.ForeignKey)
                and f.related_model._meta.app_label == APP_LABEL
                and f.related_model is not model
            }
            if deps <= set(ordered):
                ordered.append(model)
                pending.remove(model)
                progressed = True
        if not progressed:  # circular FKs (none today) — keep the remaining order
            ordered.extend(pending)
            break
    return ordered


def has_owner(model):
    return any(f.name == "owner" for f in model._meta.fields)


def owner_path(model, depth=0):
    """ORM lookup from a model to its owner, e.g. "owner" or "unit__owner"."""
    if has_owner(model):
        return "owner"
    if depth > 3:
        return None
    for f in model._meta.fields:
        if isinstance(f, models.ForeignKey) and f.related_model._meta.app_label == APP_LABEL:
            sub = owner_path(f.related_model, depth + 1)
            if sub:
                return f"{f.name}__{sub}"
    return None


def owned_queryset(model, user):
    path = owner_path(model)
    if path is None:
        raise RuntimeError(f"Cannot tell who owns {model.__name__} rows.")
    return model.objects.filter(**{path: user}).order_by("pk")
