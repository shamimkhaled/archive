"""Central Sonali Bank PLC brand constants for UI, emails, watermarks, and document IDs."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Brand:
    product_name: str = "Sonali Bank Archive System"
    org_name: str = "Sonali Bank PLC"
    org_name_bn: str = "সোনালী ব্যাংক পিএলসি"
    tagline_bn: str = "বিশ্বস্ত ও স্মার্ট"
    tagline_en: str = "Secure board governance, documents & archive"
    doc_id_prefix: str = "SB"
    logo_path: str = "/static/img/sonali-bank-logo.png"
    banner_path: str = "/static/img/sonali-bank-banner.png"
    login_background_path: str = "/static/img/sonali-login-background.png"
    seal_label: str = "Sonali Bank Archive System"
    email_from_name: str = "Sonali Bank Archive System"
    navy: str = "#1A1A54"
    gold: str = "#C5922F"
    pwa_short_name: str = "Sonali Archive"

    def doc_id_prefix_for_year(self, year: int | None = None) -> str:
        y = year if year is not None else datetime.utcnow().year
        return f"{self.doc_id_prefix}-{y}-"

    def seal(self, username: str, *, suffix: str = "Confidential", extra: str = "") -> str:
        stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        parts = [self.seal_label, username, stamp]
        if extra:
            parts.insert(-1, extra)
        parts.append(suffix)
        return " · ".join(parts)

    def seal_stream(self, username: str, *, suffix: str = "Confidential", extra: str = "") -> str:
        stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        parts = [self.seal_label, username, stamp]
        if extra:
            parts.insert(-1, extra)
        parts.append(suffix)
        return " · ".join(parts)

    def seal_download(self, username: str) -> str:
        stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        return f"{self.seal_label} · {username} · {stamp} · Downloaded · Confidential"

    def seal_meeting_doc(self, username: str) -> str:
        stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"{self.seal_label} · {username} · {stamp} · Meeting document · Confidential"
        )


BRAND = Brand()
