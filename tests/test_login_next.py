"""Returning to the page a person asked for after sign-in, without open redirects."""
from __future__ import annotations

import pytest
from fastapi import FastAPI


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/view/BCP-1?page=3", "/view/BCP-1?page=3"),
        ("/search", "/search"),
        ("//evil.example/x", None),
        ("https://evil.example/", None),
        ("/\\evil.example", None),
        ("/view\n/x", None),
        ("", None),
        (None, None),
    ],
)
def test_safe_next(value, expected):
    from bcp_project.security import safe_next

    assert safe_next(value) == expected


@pytest.mark.asyncio
async def test_login_page_remembers_safe_next():
    from starlette.requests import Request

    from bcp_project.routers.auth import LOGIN_NEXT_COOKIE, login_page

    def request(query: bytes) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/login",
                "query_string": query,
                "headers": [],
                "client": ("10.0.0.1", 1),
                "server": ("testserver", 80),
                "scheme": "http",
                "root_path": "",
                "app": FastAPI(),
            }
        )

    good = await login_page(request(b"next=%2Fview%2FBCP-1%3Fpage%3D2"))
    assert f"{LOGIN_NEXT_COOKIE}=" in good.headers.get("set-cookie", "")
    bad = await login_page(request(b"next=%2F%2Fevil.example"))
    assert LOGIN_NEXT_COOKIE not in bad.headers.get("set-cookie", "")
