"""Navigation for each role: desktop sidebar groups, the phone bottom bar, the More sheet and page titles.

Route access is still enforced by each router; this module only decides what to show.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import t

ADMIN = "admin"
SECRETARY = "board_secretary"
MEMBER = "board_member"
UPLOADER = "uploader"
ALL = (ADMIN, SECRETARY, MEMBER, UPLOADER)


@dataclass(frozen=True)
class NavItem:
    key: str
    href: str
    icon: str
    roles: Tuple[str, ...]
    match: Tuple[str, ...] = ()
    exact: bool = False

    @property
    def label(self) -> str:
        return t(f"nav.{self.key}")


ITEMS: Dict[str, NavItem] = {
    item.key: item
    for item in (
        NavItem("home", "/", "home", ALL, exact=True),
        NavItem("search", "/search", "search", (ADMIN, SECRETARY, MEMBER), ("/search", "/view/", "/documents/")),
        NavItem("ask", "/ask", "chat", (ADMIN, SECRETARY, MEMBER)),
        NavItem("map", "/archive/map", "map", (ADMIN, SECRETARY, MEMBER)),
        NavItem("my_meetings", "/board/meetings", "calendar", ALL),
        NavItem("manage_meetings", "/meetings", "calendar-edit", (ADMIN, SECRETARY)),
        NavItem("decisions", "/board/decisions", "check-list", (ADMIN, SECRETARY, MEMBER)),
        NavItem("packages", "/board/packages", "file", (ADMIN, SECRETARY)),
        NavItem("upload", "/upload", "upload", (ADMIN, UPLOADER)),
        NavItem("my_uploads", "/uploads/mine", "inbox", (ADMIN, UPLOADER)),
        NavItem("review", "/review", "doc-check", (ADMIN, SECRETARY, UPLOADER)),
        NavItem("access_review", "/admin/access-requests", "key", (ADMIN, SECRETARY)),
        NavItem("people", "/admin/users", "people", (ADMIN,)),
        NavItem("retention", "/admin/retention", "archive-box", (ADMIN, SECRETARY)),
        NavItem("queues", "/admin/queues", "pulse", (ADMIN,)),
        NavItem("ai", "/admin/ai", "sparkle", (ADMIN,)),
        NavItem("search_log", "/admin/search-activity", "clock", (ADMIN,)),
        NavItem("audit_log", "/admin/audit", "shield", (ADMIN,)),
        NavItem("my_access", "/access-requests", "key", (MEMBER,)),
        NavItem("notifications", "/notifications", "bell", ALL),
        NavItem("appearance", "/appearance", "sun", ALL),
    )
}

GROUPS: Sequence[Tuple[Optional[str], Sequence[str]]] = (
    (None, ("home",)),
    ("group_find", ("search", "ask", "map")),
    ("group_meetings", ("my_meetings", "manage_meetings", "decisions", "packages")),
    ("group_documents", ("upload", "my_uploads", "review")),
    ("group_admin", ("access_review", "people", "retention", "queues", "ai", "search_log", "audit_log")),
    ("group_account", ("my_access", "notifications", "appearance")),
)

BOTTOM: Dict[str, Sequence[Tuple[str, Optional[str]]]] = {
    MEMBER: (("home", None), ("my_meetings", "meetings"), ("decisions", None), ("search", None)),
    UPLOADER: (("home", None), ("upload", None), ("my_uploads", None), ("my_meetings", "meetings")),
    SECRETARY: (("home", None), ("manage_meetings", "meetings"), ("review", "review_short"), ("search", None)),
    ADMIN: (("home", None), ("search", None), ("review", "review_short"), ("manage_meetings", "meetings")),
}

# Pages reached from a list (no bottom bar, back arrow in the header): (prefix, back href, title key)
CONTEXTUAL: Sequence[Tuple[str, str, str]] = (
    ("/meetings/new", "/meetings", "nav.schedule"),
    ("/meetings/", "/meetings", "nav.meeting"),
    ("/board/meetings/", "/board/meetings", "nav.meeting"),
)


def _role(user) -> Optional[str]:
    if not user:
        return None
    role = getattr(user, "role", None)
    return getattr(role, "value", role)


def _visible(item: NavItem, role: Optional[str]) -> bool:
    return bool(role) and role in item.roles


def sidebar(user, path: str) -> List[dict]:
    role = _role(user)
    groups = []
    for group_key, keys in GROUPS:
        items = [ITEMS[k] for k in keys if _visible(ITEMS[k], role)]
        if items:
            groups.append(
                {
                    "label": t(f"nav.{group_key}") if group_key else None,
                    "items": [_entry(i, path) for i in items],
                }
            )
    return groups


def _entry(item: NavItem, path: str, label_key: Optional[str] = None) -> dict:
    return {
        "key": item.key,
        "href": item.href,
        "icon": item.icon,
        "label": t(f"nav.{label_key}") if label_key else item.label,
        "active": _active_key(path) == item.key,
    }


def bottom(user, path: str) -> List[dict]:
    role = _role(user)
    return [_entry(ITEMS[k], path, short) for k, short in BOTTOM.get(role or "", ()) if _visible(ITEMS[k], role)]


def more_sheet(user, path: str) -> List[dict]:
    """Everything in the sidebar that is not already on the bottom bar."""
    role = _role(user)
    on_bar = {k for k, _ in BOTTOM.get(role or "", ())}
    groups = []
    for group in sidebar(user, path):
        items = [i for i in group["items"] if i["key"] not in on_bar]
        if items:
            groups.append({"label": group["label"], "items": items})
    return groups


def _active_key(path: str) -> Optional[str]:
    """The single nav item a path belongs to: the longest matching prefix wins."""
    best: Optional[Tuple[int, str]] = None
    for item in ITEMS.values():
        if item.exact:
            if path == item.href:
                return item.key
            continue
        for prefix in item.match or (item.href,):
            if path == prefix or path.startswith(prefix.rstrip("/") + "/") or (prefix.endswith("/") and path.startswith(prefix)):
                if best is None or len(prefix) > best[0]:
                    best = (len(prefix), item.key)
    return best[1] if best else None


def contextual(path: str) -> Optional[dict]:
    for prefix, back, title_key in CONTEXTUAL:
        if path.startswith(prefix) and "/attendance/" not in path and "/documents/" not in path:
            if prefix.endswith("/") and path.rstrip("/") == prefix.rstrip("/"):
                continue
            return {"back": back, "title": t(title_key)}
    return None


def page_title(path: str) -> str:
    key = _active_key(path)
    if key:
        return ITEMS[key].label
    if path.startswith("/review"):
        return ITEMS["review"].label
    return t("nav.home")


def can_search(user) -> bool:
    return _visible(ITEMS["search"], _role(user))


def shell(user, path: str) -> dict:
    """Everything base.html needs to draw the navigation for this request."""
    ctx = contextual(path)
    return {
        "sidebar": sidebar(user, path),
        "bottom": bottom(user, path),
        "more": more_sheet(user, path),
        "title": ctx["title"] if ctx else page_title(path),
        "back": ctx["back"] if ctx else None,
        "can_search": can_search(user),
    }
