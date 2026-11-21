import hashlib
import json
import re
import sys
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import httpx
from app.database import db_session

AGENCY_KEYWORDS = [
    "manpower",
    "randstad",
    "adecco",
    "grafton",
    "trenkwalder",
    "gi group",
    "hofmann",
    "předvýběr",
    "predvyber",
    "dekra",
    "work service",
    "agentura práce",
    "personální agentura",
    "jobs contact",
    "advantage consulting",
    "start people",
    "express people",
    "maxin",
    "unique",
    "kelly services",
    "hays",
    "fides",
    "bohemia work",
    "job center",
]

JOB_TITLE_HINTS = [
    "údržb",
    "mechanik",
    "elektrik",
    "seřizovač",
    "serizovac",
    "technik",
    "diagnostik",
    "operátor",
    "operator",
    "zámečník",
    "zamecnik",
    "svářeč",
    "svarec",
    "údržbář",
    "udrzbar",
    "provoz",
    "stroj",
    "vedoucí směny",
    "mistr",
    "skladník",
    "trenér",
    "trener",
]

CAREER_PATH_HINTS = [
    "/job/",
    "/jobs/",
    "/pozice/",
    "/volna-mista/",
    "/volná-místa/",
    "/nabidka-prace/",
    "/nabídka-práce/",
]

NON_JOB_TITLE_SUBSTRINGS = [
    "spolupráce se škol",
    "spoluprace se skol",
    "spolupráce se student",
    "spoluprace se student",
    "jak to vidí",
    "jak to vidi",
    "jak se stát spokojeným",
    "jak se stat spokojenym",
    "proč pracovat",
    "proc pracovat",
    "benefity",
    "o nás",
    "o nas",
    "aktuality",
    "kariéra v ",
    "kariera v ",
    "volná místa",
    "volna mista",
    "cookies",
    "ochrana osobních údajů",
    "gdpr",
    "kontakt",
    "přihlásit",
    "login",
    "více informací",
]

NON_JOB_URL_ENDINGS = (
    "/kariera",
    "/kariera/",
    "/kariera-v-zdb",
    "/kariera-v-zdb/",
    "/volna-mista",
    "/volna-mista/",
    "/volná-místa",
    "/volná-místa/",
)

FALLBACK_CAREER_PATHS = (
    "/cs/kariera/",
    "/kariera/",
    "/cs/o-spolecnosti/kariera/",
    "/cs/spolecnost/kariera/",
    "/cs/",
    "/",
)


def _log(msg: str) -> None:
    print(f"[scraper] {msg}", file=sys.stdout, flush=True)


def _is_agency(text: str) -> bool:
    low = (text or "").lower()
    return any(kw in low for kw in AGENCY_KEYWORDS)


def _looks_like_job(title: str, href: str, full_url: str = "") -> bool:
    t = (title or "").strip().lower()
    h = (href or "").strip().lower()
    u = (full_url or href or "").strip().lower().rstrip("/")
    if len(t) < 4 or len(t) > 140:
        return False
    if any(skip in t for skip in NON_JOB_TITLE_SUBSTRINGS):
        return False
    if any(u.endswith(end.rstrip("/")) for end in NON_JOB_URL_ENDINGS):
        return False
    if any(k in t for k in JOB_TITLE_HINTS):
        return True
    if any(p in h for p in CAREER_PATH_HINTS) and len(t.split()) >= 2:
        return True
    return False


def _extract_json_ld_jobs(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        candidates: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            if data.get("@type") == "JobPosting":
                candidates.append(data)
            elif isinstance(data.get("@graph"), list):
                candidates.extend([x for x in data["@graph"] if isinstance(x, dict) and x.get("@type") == "JobPosting"])
        elif isinstance(data, list):
            candidates.extend([x for x in data if isinstance(x, dict) and x.get("@type") == "JobPosting"])

        for job in candidates:
            title = str(job.get("title") or "").strip()
            if not title:
                continue
            job_url = str(job.get("url") or base_url).strip()
            job_url = urljoin(base_url, job_url)
            desc_raw = BeautifulSoup(str(job.get("description") or ""), "html.parser").get_text(" ", strip=True)
            found.append({
                "title": title[:160],
                "url": job_url[:500],
                "description": desc_raw[:1500],
            })
    return found


def _extract_html_links_jobs(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []
    seen_urls = set()
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen_urls or full_url.rstrip("/") == base_url.rstrip("/"):
            continue
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if _looks_like_job(title, href, full_url):
            seen_urls.add(full_url)
            parent_text = ""
            if a.parent:
                parent_text = re.sub(r"\s+", " ", a.parent.get_text(" ", strip=True)).strip()
            found.append({
                "title": title[:160],
                "url": full_url[:500],
                "description": parent_text[:1000],
            })
    return found[:25]


def _is_ssl_error(exc: Exception) -> bool:
    txt = repr(exc).lower()
    return "ssl" in txt or "certificate_verify_failed" in txt or "hostname mismatch" in txt


def _build_candidate_urls(base_url: str) -> List[str]:
    candidates = [base_url]
    parsed = urlparse(base_url)
    if parsed.scheme and parsed.netloc:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        for path in FALLBACK_CAREER_PATHS:
            cand = f"{origin}{path}"
            if cand not in candidates:
                candidates.append(cand)
    return candidates


async def _get_with_ssl_fallback(
    url: str,
    headers: Dict[str, str],
    company_name: str = "",
) -> Tuple[str, str]:
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, verify=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text, str(resp.url)
    except Exception as exc:
        if _is_ssl_error(exc):
            _log(f"SSL ověření selhalo pro '{company_name or url}', zkouším verify=False: {exc}")
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, verify=False) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.text, str(resp.url)
        raise


async def fetch_company_offers(company: Dict[str, Any]) -> List[Dict[str, str]]:
    url = (company.get("career_url") or "").strip()
    if not url:
        return []
    company_name = (company.get("name") or "").strip()
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.7",
    }

    html = ""
    final_url = url
    candidates = _build_candidate_urls(url)
    last_exc: Exception | None = None

    for idx, candidate_url in enumerate(candidates):
        try:
            html, final_url = await _get_with_ssl_fallback(candidate_url, headers, company_name=company_name)
            if idx > 0:
                _log(f"Fallback URL úspěšné pro '{company_name}': {candidate_url} -> {final_url}")
            last_exc = None
            break
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410) and idx < len(candidates) - 1:
                continue
            raise
        except Exception as exc:
            last_exc = exc
            raise

    if last_exc is not None and not html:
        raise last_exc

    soup = BeautifulSoup(html, "html.parser")
    offers = _extract_json_ld_jobs(soup, final_url)
    if not offers:
        offers = _extract_html_links_jobs(soup, final_url)
    return offers


async def run_scrape_cycle() -> Dict[str, int]:
    stats = {"companies_checked": 0, "new_offers": 0, "errors": 0}
    with db_session() as conn:
        companies = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM companies
                WHERE is_active = 1 AND is_blacklisted = 0
                  AND scraper_type != 'manual' AND TRIM(career_url) != ''
                ORDER BY commute_minutes ASC NULLS LAST, id ASC
                """
            ).fetchall()
        ]

    for comp in companies:
        stats["companies_checked"] += 1
        comp_id = comp["id"]
        comp_name = comp["name"]
        try:
            raw_offers = await fetch_company_offers(comp)
            added_for_comp = 0
            with db_session() as conn:
                for item in raw_offers:
                    title = item["title"].strip()
                    offer_url = item["url"].strip()
                    desc = item.get("description", "").strip()
                    if not _looks_like_job(title, offer_url, offer_url):
                        continue
                    content_hash = hashlib.sha256(
                        f"{title.lower()}|{offer_url.lower()}|{desc[:300].lower()}".encode("utf-8")
                    ).hexdigest()
                    existing = conn.execute("SELECT id FROM job_offers WHERE url = ?", (offer_url,)).fetchone()
                    if existing:
                        conn.execute(
                            "UPDATE job_offers SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (existing["id"],),
                        )
                        continue
                    agency_flag = 1 if (_is_agency(comp_name) or _is_agency(title) or _is_agency(desc)) else 0
                    conn.execute(
                        """
                        INSERT INTO job_offers (
                            company_id, company_name_raw, title, url, source_type,
                            is_agency, commute_minutes, commute_km, description_clean, content_hash, status
                        ) VALUES (?, ?, ?, ?, 'direct_company', ?, ?, ?, ?, ?, 'new')
                        """,
                        (
                            comp_id,
                            comp_name,
                            title,
                            offer_url,
                            agency_flag,
                            comp.get("commute_minutes"),
                            comp.get("commute_km"),
                            desc,
                            content_hash,
                        ),
                    )
                    stats["new_offers"] += 1
                    added_for_comp += 1
                conn.execute(
                    """
                    UPDATE companies
                    SET last_status = ?, last_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (f"OK ({added_for_comp} nových)", comp_id),
                )
        except Exception as exc:
            stats["errors"] += 1
            _log(f"Chyba při sběru pro '{comp_name}' ({comp.get('career_url')}): {type(exc).__name__}: {exc}")
            with db_session() as conn:
                conn.execute(
                    """
                    UPDATE companies
                    SET last_status = ?, last_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (f"Chyba: {type(exc).__name__}", comp_id),
                )

    _log(f"Dokončen sběr: {stats}")
    return stats