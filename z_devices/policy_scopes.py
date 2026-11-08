"""
v1.4 per-department and per-PC protection rules (brief section 1c).

The company Policy stays the one row it always was. A department (Member.role) or one PC can have its own values for
any Policy field (PolicyOverride.data = {model field: value}, only what differs from the scope above). A PC enforces
company <- its member's department <- itself. Every change bumps Policy.version, so every PC re-fetches.
"""
from __future__ import annotations

import copy
from typing import Any

from django.db import transaction

from z_auth.models import User

from . import blocklists
from .models import Device, Policy, PolicyOverride
from .work_rules import BadScope, parse_scope, scope_label

COMPANY = "company"


def _field_map() -> dict[str, tuple[str, str]]:
    from .services import _POLICY_BLOCKS

    return {model_field: (block, wire) for block, fields in _POLICY_BLOCKS for wire, model_field in fields.items()}


def _apply(policy: Policy, data: dict[str, Any]) -> Policy:
    fields = _field_map()
    for model_field, value in (data or {}).items():
        if model_field in fields:
            setattr(policy, model_field, value)
    return policy


def _overrides(owner: User) -> dict[tuple[str, str], dict[str, Any]]:
    return {(o.scope_kind, o.scope_key): o.data or {} for o in PolicyOverride.objects.filter(owner=owner)}


def chain(owner: User, kind: str, key: str) -> list[tuple[str, str]]:
    """The non-company scopes that apply at (kind, key), outermost first."""
    if kind == COMPANY:
        return []
    if kind == "department":
        return [("department", key)]
    device = Device.objects.filter(owner=owner, pk=int(key)).select_related("member").first()
    role = device.member.role if device and device.member_id else ""
    return ([("department", role)] if role else []) + [("device", key)]


def effective_policy(owner: User, kind: str, key: str, base: Policy | None = None) -> Policy:
    """An unsaved copy of the company Policy with the department / PC values laid over it."""
    from .services import get_policy

    base = base or get_policy(owner)
    policy = copy.copy(base)
    found = _overrides(owner)
    for scope in chain(owner, kind, key):
        _apply(policy, found.get(scope, {}))
    return policy


def policy_for_device_scope(device: Device, base: Policy | None = None) -> Policy:
    """What this PC enforces (before the plan / consent clamps): company <- department <- PC."""
    from .services import get_policy

    base = base or get_policy(device.owner)
    found = _overrides(device.owner) if PolicyOverride.objects.filter(owner_id=device.owner_id).exists() else {}
    if not found:
        return base
    policy = copy.copy(base)
    role = device.member.role if device.member_id and device.member else ""
    if role:
        _apply(policy, found.get(("department", role), {}))
    _apply(policy, found.get(("device", str(device.pk)), {}))
    return policy


def own_fields(owner: User, kind: str, key: str) -> list[str]:
    if kind == COMPANY:
        return []
    row = PolicyOverride.objects.filter(owner=owner, scope_kind=kind, scope_key=key).first()
    fields = _field_map()
    return sorted(f"{fields[f][0]}.{fields[f][1]}" for f in (row.data if row else {}) if f in fields)


def scope_extra(owner: User, kind: str, key: str) -> dict[str, Any]:
    own = own_fields(owner, kind, key)
    return {"scope": scope_label(kind, key), "own": kind == COMPANY or bool(own), "own_fields": own}


def _bump(owner: User) -> None:
    from .services import get_policy

    get_policy(owner)
    policy = Policy.objects.select_for_update().get(owner=owner)
    policy.version += 1
    policy.save(update_fields=["version", "updated_at"])


@transaction.atomic
def update_scope(owner: User, kind: str, key: str, data: dict[str, Any]) -> Policy:
    """PATCH policy/?scope=department:...|device:...: keep only the values that differ from the scope above."""
    from .services import check_policy_change

    check_policy_change(owner, data)
    parent_chain = chain(owner, kind, key)[:-1]
    parent = effective_policy(owner, COMPANY, "")
    found = _overrides(owner)
    for scope in parent_chain:
        _apply(parent, found.get(scope, {}))
    row = PolicyOverride.objects.select_for_update().filter(owner=owner, scope_kind=kind, scope_key=key).first()
    own = dict(row.data) if row else {}
    web = data.get("web")
    if web is not None:
        allow = blocklists.clean_allow(web.get("allow_domains", parent.web_allow_domains), web["custom_domains"])
        data = {**data, "web": {**web, "allow_domains": allow}}
    from .services import _POLICY_BLOCKS

    for block_key, field_map in _POLICY_BLOCKS:
        block = data.get(block_key)
        if block is None:
            continue
        for wire_key, model_field in field_map.items():
            if wire_key not in block:
                continue
            value = block[wire_key]
            if getattr(parent, model_field) == value:
                own.pop(model_field, None)
            else:
                own[model_field] = value
    before = row.data if row else {}
    if own != before:
        if own:
            PolicyOverride.objects.update_or_create(owner=owner, scope_kind=kind, scope_key=key, defaults={"data": own})
        elif row is not None:
            row.delete()
        _bump(owner)
    return effective_policy(owner, kind, key)


@transaction.atomic
def reset_scope(owner: User, kind: str, key: str) -> Policy:
    deleted, _ = PolicyOverride.objects.filter(owner=owner, scope_kind=kind, scope_key=key).delete()
    if deleted:
        _bump(owner)
    return effective_policy(owner, kind, key)


def drop_device(device: Device) -> None:
    """A removed PC's own rules go with it (called when a device row is deleted)."""
    PolicyOverride.objects.filter(owner_id=device.owner_id, scope_kind="device", scope_key=str(device.pk)).delete()


__all__ = ["BadScope", "parse_scope", "effective_policy", "policy_for_device_scope", "update_scope", "reset_scope"]
