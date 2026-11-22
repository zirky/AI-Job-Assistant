import asyncio
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup, Tag
import httpx
from app.database import db_session
from app.services.distance import estimate_commute, geocode_location
from app.services.query_planner import get_search_terms_for_profile
from app.services.scraper import _is_agency, _looks_like_job

MPSV_DATA_URL = "https://data.mpsv.cz/od/soubory/volna-mista/volna-mista.json.gz"
MPSV_CACHE_PATH = "/tmp/mpsv_volna_mista.json"
MPSV_CACHE_TTL_SEC = 6 * 3600

OKRES_ID_MAP = {
    "Okres/3807": "Karviná",
    "Okres/3808": "Ostrava",
    "Okres/3806": "Frýdek-Místek",
    "Okres/3809": "Opava",
    "Okres/3805": "Bruntál",
    "Okres/3810": "Nový Jičín",
}

BOARD_NAMES = {"jobs.cz", "prace.cz", "jenprace.cz", "linkedin.com", "uradprace.cz"}

NOISE_COMPANY_PATTERNS = re.compile(
    r"^(?:"
    r"podívejte se na profil|profil firmy|zobrazit profil|více informací|zjistit více|"
    r"aktualizováno|přidáno|nové|top|doporučujeme|odpovědět|detail|zobrazit|"
    r"plný úvazek|zkrácený úvazek|brigáda|ičo|hpp|dpč|dpp|"
    r"jednosměnný|dvousměnný|třísměnný|nepřetržitý|směnný|"
    r"náborový příspěvek|benefity|mzda|plat|hodnocení|atmoskop|"
    r"moravskoslezský kraj|okres\b|česká republika"
    r")",
    flags=re.IGNORECASE,
)

HIDDEN_AGENCY_RE = re.compile(
    r"(?:pro\s+na[šs]eho\s+(?:v[ýy]znamn[ée]ho\s+)?klienta|"
    r"zprost[řr]edkov[áa]n[íi]\s+zam[ěe]stn[áa]n[íi]|"
    r"person[áa]ln[íi]\s+agentur|"
    r"agenturn[íi]\s+zam[ěe]stn[áa]n[íi]|"
    r"do\s+kmenov[ée]ho\s+stavu\s+klienta)",
    flags=re.IGNORECASE,
)

AGENCY_NAME_RE = re.compile(
    r"\b(?:recruitment|recruiting|staffing|personnel|person[áa]ln[íi]|"
    r"agentur\w*|zprost[řr]edkov\w*|manuvia|comac\s+jobs|randstad|manpower|"
    r"adecco|grafton|trenkwalder|jobs\s+contact|advantage\s+consulting)\b",
    flags=re.IGNORECASE,
)

DISTANT_REGION_RE = re.compile(
    r"\b(?:hl\.\s*m\.\s*praha|praha\s*[1-9]?|st[řr]edo[čc]esk[ýy]\s+kraj|nupaky|kladno|"
    r"karlovarsk[ýy]\s+kraj|doln[íi]\s+rychnov|cheb|sokolov|karlovy\s+vary|"
    r"[úu]steck[ýy]\s+kraj|libereck[ýy]\s+kraj|kr[áa]lov[ée]hradeck[ýy]\s+kraj|"
    r"pardubick[ýy]\s+kraj|jiho[čc]esk[ýy]\s+kraj|plze[ňn]sk[ýy]\s+kraj|vyso[čc]ina)\b",
    flags=re.IGNORECASE,
)

# PSČ mimo Moravu/Slezsko (začínající 1, 2, 3, 4, 5)
DISTANT_PSC_RE = re.compile(r"\b([1-5]\d{2}\s?\d{2})\b")


def _log(msg: str) -> None:
    print(f"[job_boards] {msg}", file=sys.stdout, flush=True)


def _sanitize_err(exc: Exception) -> str:
    msg = re.sub(r"([?&]key=)[^&\s]+", r"\1***", str(exc), flags=re.IGNORECASE)
    return msg[:300]


def _slugify_ascii(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    ascii_str = norm.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_str).strip("-")


def _norm_key(title: str, company: str) -> str:
    t = re.sub(r"[^a-z0-9áčďéěíňóřšťúůýž]+", " ", (title or "").lower()).strip()
    c = re.sub(r"[^a-z0-9áčďéěíňóřšťúůýž]+", " ", (company or "").lower()).strip()
    return f"{t[:80]}||{c[:50]}"


def _is_hidden_agency_text(text: str) -> bool:
    return bool(HIDDEN_AGENCY_RE.search(text or ""))


def _is_agency_offer(company_name: str, title: str, desc: str) -> bool:
    if _is_agency(company_name) or _is_agency(title) or _is_agency(desc):
        return True
    if _is_hidden_agency_text(desc) or _is_hidden_agency_text(title):
        return True
    if AGENCY_NAME_RE.search(company_name or ""):
        return True
    return False


def _extract_explicit_locality(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(
        r"(?:Lokalita|M[íi]sto\s+pracovi[šs]t[ěe]|M[íi]sto\s+v[ýy]konu\s+pr[áa]ce|Obec|M[ěe]sto)\s*:\s*"
        r"([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9][^\n|]{1,70}?)"
        r"(?=\s*\.\s*(?:Mzda|Plat|Sm[ěe]ny|Lokalita)\b|\s+(?:N[áa]zev\s+firmy|Zam[ěe]stnavatel|Spole[čc]nost|Typ\s+[úu]vazku|Mzda|Plat|Sm[ěe]ny|Aktualizov[áa]no|P[řr]id[áa]no)\b|$)",
        text,
    )
    if not m:
        return None
    cand = re.sub(r"\s*[-–].*$", "", m.group(1)).strip(" ,.")
    if len(cand) < 2 or cand.lower() in {"česká republika", "czechia", "celá čr", "moravskoslezský kraj"}:
        return None
    return cand


def _extract_place_fallback(text: str) -> Optional[str]:
    explicit = _extract_explicit_locality(text)
    if explicit:
        return explicit
    if not text:
        return None

    m_distant = DISTANT_REGION_RE.search(text)
    if m_distant:
        return m_distant.group(0).strip()

    m = re.search(
        r"\b(?:v|ve|do|z|ze|u|okres)\s+([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]+(?:\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]+)?)\b",
        text,
    )
    if not m:
        return None
    cand = m.group(1).strip()
    if cand.lower() in {"čr", "cr", "česku", "moravě", "slezsku", "práci", "provozu", "firmě"}:
        return None
    return cand


def _extract_json_ld_locality(soup: BeautifulSoup) -> Optional[str]:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        stack: List[Any] = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(cur)
                continue
            if not isinstance(cur, dict):
                continue
            if cur.get("@type") == "JobPosting":
                loc = cur.get("jobLocation")
                locs = loc if isinstance(loc, list) else [loc]
                for item in locs:
                    if not isinstance(item, dict):
                        continue
                    addr = item.get("address")
                    if isinstance(addr, str) and len(addr.strip()) >= 2:
                        return addr.strip()
                    if isinstance(addr, dict):
                        locality = str(addr.get("addressLocality") or "").strip()
                        postal = str(addr.get("postalCode") or "").strip()
                        if locality and locality.lower() not in {"česká republika", "czechia"}:
                            return f"{postal} {locality}".strip() if postal else locality
                        if postal:
                            return postal
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
    return None


def _extract_locality_from_company_or_url(company_name: str, url: str) -> Optional[str]:
    clean_comp = re.sub(
        r"\b(?:s\.?\s?r\.?\s?o\.?|a\.?\s?s\.?|spol\.?|k\.?\s?s\.?|v\.?\s?o\.?\s?s\.?|gmbh|czech|cz|group|holding)\b",
        "",
        company_name or "",
        flags=re.IGNORECASE,
    ).strip(" ,.-")
    words = [w for w in clean_comp.split() if len(w) >= 4 and w[0].isupper() and not w.isupper()]
    skip_words = {
        "proks", "plastic", "plasty", "siemens", "hilite", "mahle", "system",
        "electronic", "expert", "recruitment", "comac", "manuvia", "mark", "corporation",
    }
    for w in words:
        if w.lower() not in skip_words:
            return w

    m_url = re.search(r"/nabidky/([a-z0-9-]{3,40})(?:[/?#]|$)", (url or "").lower())
    if m_url and m_url.group(1) not in {"prace", "nabidka", "detail", "hledani"}:
        return m_url.group(1).replace("-", " ")
    return None


def _extract_best_locality(
    text: str,
    company_name: str = "",
    url: str = "",
    soup: Optional[BeautifulSoup] = None,
    allow_company_guess: bool = True,
) -> Optional[str]:
    if soup is not None:
        ld_loc = _extract_json_ld_locality(soup)
        if ld_loc:
            return ld_loc
        for el in soup.find_all(True):
            hint = " ".join(
                str(el.get(k) or "") for k in ("class", "itemprop", "data-test", "data-testid")
            ).lower()
            if any(k in hint for k in ("locality", "addresslocality", "location", "place", "city")):
                cand = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip(" ,.-|")
                cand = re.sub(r"\s*[-–]\s*.*$", "", cand).strip()
                if 2 <= len(cand) <= 45 and cand.lower() not in {"česká republika", "czechia", "celá čr", "moravskoslezský kraj"}:
                    return cand

    loc = _extract_place_fallback(text)
    if loc:
        return loc

    m_zip = re.search(r"\b([1-7]\d{2}\s?\d{2})\b", text or "")
    if m_zip:
        return m_zip.group(1)

    if allow_company_guess:
        return _extract_locality_from_company_or_url(company_name, url)
    return None


def _clean_company_candidate(raw_txt: str, title: str = "", strict: bool = False) -> Optional[str]:
    txt = re.sub(r"\s+", " ", (raw_txt or "")).strip(" |-•·,")
    if not txt:
        return None
    m = re.search(
        r"(?:N[áa]zev\s+firmy|Zam[ěe]stnavatel|Spole[čc]nost|Firma)\s*:\s*(.+?)"
        r"(?=\s+(?:Lokalita|M[íi]sto\s+pracovi[šs]t[ěe]|Typ\s+[úu]vazku|Mzda|Plat|Sm[ěe]ny|Aktualizov[áa]no)\s*:|$)",
        txt,
        flags=re.IGNORECASE,
    )
    if m:
        txt = m.group(1).strip(" |-•·,")

    low = txt.lower()
    if not txt or low == (title or "").lower() or low in BOARD_NAMES:
        return None
    if len(txt) < 2 or len(txt) > 85:
        return None
    if NOISE_COMPANY_PATTERNS.search(txt):
        return None
    if re.search(r"\b\d{2,3}\s?\d{3}\s*kč\b", low) or re.fullmatch(r"[\d\s.,/-]+", txt):
        return None
    if re.fullmatch(r"[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]+(?:\s*[-–]\s*[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]+)?", txt):
        if len(txt.split()) <= 2 and not txt.isupper():
            return None

    has_legal_or_brand = any(
        k in low
        for k in [
            "s.r.o.",
            "s. r. o.",
            "a.s.",
            "a. s.",
            "spol.",
            "k.s.",
            "v.o.s.",
            "gmbh",
            "group",
            "holding",
            "manpower",
            "randstad",
            "adecco",
            "grafton",
            "trenkwalder",
            "jobs contact",
            "advantage consulting",
            "antolin",
            "dendera",
            "preciz",
            "mobis",
            "hella",
            "lear",
            "hauser",
            "gerresheimer",
            "hp-pelzer",
            "rockwool",
            "bochemie",
            "hilite",
            "hyundai",
            "finidr",
            "třinecké železárny",
            "vítkovice",
            "bonatrans",
            "ždb",
            "lenzing",
            "continental",
            "brose",
            "varroc",
            "mahle",
            "siemens",
            "abb",
            "čez",
            "okd",
            "liberty",
        ]
    )
    if has_legal_or_brand:
        return txt

    if not strict:
        if txt.isupper() and 3 <= len(txt) <= 45 and len(txt.split()) <= 4:
            return txt
        if 2 <= len(txt.split()) <= 5 and txt[0].isupper() and not txt.endswith("?"):
            return txt

    return None


def _extract_company_from_card(parent: Optional[Tag], title: str) -> Optional[str]:
    if not parent:
        return None

    for el in parent.find_all(True):
        cls_attr = " ".join(el.get("class") or []).lower()
        itemprop = str(el.get("itemprop") or "").lower()
        data_test = str(el.get("data-test") or el.get("data-testid") or "").lower()
        hint = f"{cls_attr} {itemprop} {data_test}"
        if any(k in hint for k in ("company", "employer", "firm", "hiring", "author", "vendor")):
            cleaned = _clean_company_candidate(el.get_text(" ", strip=True), title, strict=False)
            if cleaned:
                return cleaned

    for cand in parent.find_all(["span", "div", "p", "strong", "a", "small"]):
        cleaned = _clean_company_candidate(cand.get_text(" ", strip=True), title, strict=True)
        if cleaned:
            return cleaned

    return None


def _build_board_urls(board: str, location: str, term: str) -> List[str]:
    loc_slug = _slugify_ascii(location) or "bohumin"
    loc_q = quote_plus(location)
    term_q = quote_plus(term)
    if board == "jobs.cz":
        return [
            f"https://www.jobs.cz/prace/?q%5B0%5D={term_q}",
        ]
    if board == "prace.cz":
        return [
            f"https://www.prace.cz/nabidky/?q={term_q}&lokalita={loc_q}",
        ]
    if board == "jenprace.cz":
        return [
            f"https://www.jenprace.cz/nabidky/{loc_slug}?query={term_q}",
            f"https://www.jenprace.cz/nabidky?query={term_q}&location={loc_q}",
        ]
    if board == "linkedin.com":
        return [
            f"https://www.linkedin.com/jobs/search/?keywords={term_q}&location={loc_q}",
        ]
    if board == "uradprace.cz":
        return [
            f"https://up.gov.cz/volna-mista-v-cr?query={term_q}&location={loc_q}",
        ]
    return []


def _download_mpsv_sync(tmp_download: str) -> None:
    with httpx.Client(timeout=180.0, follow_redirects=True, verify=False) as client:
        with client.stream("GET", MPSV_DATA_URL) as resp:
            resp.raise_for_status()
            with open(tmp_download, "wb") as out_f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    if chunk:
                        out_f.write(chunk)


async def _ensure_mpsv_dataset() -> Optional[str]:
    try:
        if os.path.exists(MPSV_CACHE_PATH):
            age = time.time() - os.path.getmtime(MPSV_CACHE_PATH)
            if age < MPSV_CACHE_TTL_SEC and os.path.getsize(MPSV_CACHE_PATH) > 100_000:
                return MPSV_CACHE_PATH
    except Exception:
        pass

    tmp_download = f"{MPSV_CACHE_PATH}.part"
    try:
        await asyncio.to_thread(_download_mpsv_sync, tmp_download)
        os.replace(tmp_download, MPSV_CACHE_PATH)
        return MPSV_CACHE_PATH
    except Exception as exc:
        _log(f"Nepodařilo se stáhnout otevřená data MPSV: {type(exc).__name__}: {_sanitize_err(exc)}")
        try:
            if os.path.exists(tmp_download):
                os.remove(tmp_download)
        except Exception:
            pass
        return MPSV_CACHE_PATH if os.path.exists(MPSV_CACHE_PATH) else None


def _iter_mpsv_polozky(file_path: str) -> Iterator[Dict[str, Any]]:
    started_array = False
    in_string = False
    escape = False
    depth = 0
    buf: List[str] = []
    tail = ""

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            for ch in chunk:
                if not started_array:
                    tail = (tail + ch)[-64:]
                    if '"polozky"' in tail and ch == "[":
                        started_array = True
                    continue

                if depth == 0:
                    if ch == "]":
                        return
                    if ch == "{":
                        depth = 1
                        in_string = False
                        escape = False
                        buf = [ch]
                    continue

                buf.append(ch)
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == '"':
                    in_string = not in_string
                    continue
                if not in_string:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            raw_obj = "".join(buf)
                            buf = []
                            try:
                                parsed = json.loads(raw_obj)
                                if isinstance(parsed, dict):
                                    yield parsed
                            except Exception:
                                continue


def _extract_mpsv_str(val: Any, lang: str = "cs") -> str:
    if isinstance(val, dict):
        if lang in val and val[lang]:
            return str(val[lang]).strip()
        for v in val.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(val, str):
        return val.strip()
    return ""


def _extract_mpsv_locality(row: Dict[str, Any]) -> str:
    mv_prace = row.get("mistoVykonuPrace") if isinstance(row.get("mistoVykonuPrace"), dict) else {}

    # 1. Kontrola okresů – pokud má nabídka zadané okresy a žádný není v našem okolí, rovnou ji vyřadíme
    okresy = mv_prace.get("okresy")
    allowed_okres_names: List[str] = []
    if isinstance(okresy, list) and okresy:
        for ok in okresy:
            if isinstance(ok, dict):
                ok_id = str(ok.get("id") or "").strip()
                if ok_id in OKRES_ID_MAP:
                    allowed_okres_names.append(OKRES_ID_MAP[ok_id])
        if not allowed_okres_names:
            return "__OUT_OF_REGION__"

    pracoviste = mv_prace.get("pracoviste")
    if isinstance(pracoviste, list):
        has_any_okres_in_prac = False
        has_allowed_okres_in_prac = False
        for p in pracoviste:
            if not isinstance(p, dict):
                continue
            addr = p.get("adresa") if isinstance(p.get("adresa"), dict) else {}
            okres_obj = addr.get("okres") if isinstance(addr.get("okres"), dict) else {}
            ok_id = str(okres_obj.get("id") or "").strip()
            if ok_id:
                has_any_okres_in_prac = True
                if ok_id in OKRES_ID_MAP:
                    has_allowed_okres_in_prac = True
                    allowed_okres_names.append(OKRES_ID_MAP[ok_id])

        if has_any_okres_in_prac and not has_allowed_okres_in_prac:
            return "__OUT_OF_REGION__"

        for p in pracoviste:
            if not isinstance(p, dict):
                continue
            addr = p.get("adresa") if isinstance(p.get("adresa"), dict) else {}
            psc = str(addr.get("psc") or "").strip()
            if psc and psc[0] in {"1", "2", "3", "4", "5"}:
                return "__OUT_OF_REGION__"

            obec_obj = addr.get("obec") if isinstance(addr.get("obec"), dict) else {}
            obec = str(obec_obj.get("nazev") or "").strip()
            ulice_obj = addr.get("ulice") if isinstance(addr.get("ulice"), dict) else {}
            ulice = str(ulice_obj.get("nazev") or "").strip()
            dodatek = str(p.get("dodatekAdresy") or addr.get("dodatekAdresy") or "").strip()
            p_nazev = str(p.get("nazev") or "").strip()

            m_psc_city = re.search(
                r"\b([67]\d{2}\s?\d{2})\s+([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž-]+(?:\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž-]+)?)",
                f"{dodatek} {p_nazev}",
            )
            if m_psc_city:
                return f"{m_psc_city.group(1)} {m_psc_city.group(2)}"

            if obec and psc:
                return f"{psc} {obec}"
            if obec:
                return obec
            if ulice and psc:
                return f"{ulice}, {psc}"

    if allowed_okres_names:
        return allowed_okres_names[0]

    return ""


async def _fetch_mpsv_offers(term: str, location: str, limit: int = 25) -> List[Dict[str, str]]:
    path = await _ensure_mpsv_dataset()
    if not path:
        return []

    term_low = (term or "").strip().lower()
    items: List[Dict[str, str]] = []

    for row in _iter_mpsv_polozky(path):
        title = _extract_mpsv_str(row.get("pozadovanaProfese"))
        if not title:
            continue

        lok_val = _extract_mpsv_locality(row)
        if lok_val == "__OUT_OF_REGION__":
            continue

        emp = row.get("zamestnavatel") if isinstance(row.get("zamestnavatel"), dict) else {}
        company_name = str(emp.get("nazev") or "").strip() or "uradprace.cz"
        info = _extract_mpsv_str(row.get("upresnujiciInformace"))

        mzda_od = row.get("mesicniMzdaOd")
        mzda_do = row.get("mesicniMzdaDo")
        mzda_txt = ""
        if mzda_od is not None and mzda_do is not None:
            mzda_txt = f"Mzda: {mzda_od}–{mzda_do} Kč. "
        elif mzda_od is not None:
            mzda_txt = f"Mzda od: {mzda_od} Kč. "

        lok_txt = f"Lokalita: {lok_val}. " if lok_val else ""
        desc = f"{lok_txt}{mzda_txt}{info}".strip() or title

        haystack = f"{title} {company_name} {lok_val} {desc}".lower()
        if term_low and term_low not in haystack:
            continue

        portal_id = row.get("portalId")
        raw_url = str(row.get("urlAdresa") or "").strip()
        if raw_url.startswith("http"):
            offer_url = raw_url
        elif portal_id:
            offer_url = f"https://up.gov.cz/volna-mista-v-cr#/volna-mista-detail/{portal_id}"
        else:
            ref_no = str(row.get("referencniCislo") or "").strip()
            offer_url = f"https://up.gov.cz/volna-mista-v-cr?query={quote_plus(ref_no or title)}"

        items.append({
            "board_name": "uradprace.cz",
            "company_name": company_name[:160],
            "title": title[:160],
            "url": offer_url[:500],
            "description": desc[:1500],
        })
        if len(items) >= limit:
            break

    return items


def _parse_json_ld(soup: BeautifulSoup, base_url: str, default_board: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
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
            offer_url = urljoin(base_url, str(job.get("url") or base_url).strip())
            org = job.get("hiringOrganization")
            company_name = ""
            if isinstance(org, dict):
                company_name = str(org.get("name") or "").strip()
            cleaned_org = _clean_company_candidate(company_name, title, strict=False) or company_name
            if cleaned_org.lower() in BOARD_NAMES:
                cleaned_org = ""
            desc = BeautifulSoup(str(job.get("description") or ""), "html.parser").get_text(" ", strip=True)
            items.append({
                "board_name": default_board,
                "company_name": cleaned_org or default_board,
                "title": title[:160],
                "url": offer_url[:500],
                "description": desc[:1500],
            })
    return items


def _parse_html_offers(soup: BeautifulSoup, base_url: str, default_board: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if not _looks_like_job(title, href, full_url):
            continue
        seen.add(full_url)

        container_text = ""
        parent = a.find_parent(["article", "li", "div"]) or a.parent
        if parent:
            container_text = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)).strip()

        company_guess = _extract_company_from_card(parent, title) or default_board

        items.append({
            "board_name": default_board,
            "company_name": company_guess,
            "title": title[:160],
            "url": full_url[:500],
            "description": container_text[:1200],
        })
        if len(items) >= 20:
            break

    return items


async def _fetch_html(url: str) -> Tuple[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.7",
    }
    verify_ssl = "uradprace.cz" not in url.lower() and "up.gov.cz" not in url.lower()
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, verify=verify_ssl) as client:
        for attempt in range(3):
            resp = await client.get(url, headers=headers)
            if resp.status_code in (500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.text, str(resp.url)
    return "", url


async def _fetch_board_search(board: str, url: str) -> List[Dict[str, str]]:
    html, final_url = await _fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    found = _parse_json_ld(soup, final_url, board)
    if not found:
        found = _parse_html_offers(soup, final_url, board)
    return found


async def _enrich_offer_detail(item: Dict[str, str], force_detail: bool = False) -> Dict[str, str]:
    """Doplní firmu nebo detailnější popis/lokalitu z detailu inzerátu."""
    needs_company = item.get("company_name", "").lower() in BOARD_NAMES
    if not needs_company and not force_detail:
        return item
    url = item.get("url", "")
    if not url:
        return item
    try:
        html, final_url = await _fetch_html(url)
    except Exception:
        return item

    soup = BeautifulSoup(html, "html.parser")
    ld_items = _parse_json_ld(soup, final_url, item["board_name"])
    if ld_items:
        ld_comp = ld_items[0].get("company_name", "")
        if ld_comp and ld_comp.lower() not in BOARD_NAMES:
            item["company_name"] = ld_comp
        ld_loc = _extract_json_ld_locality(soup)
        if ld_loc:
            item["description"] = f"Lokalita: {ld_loc}. {ld_items[0].get('description', '')}".strip()
        elif len(ld_items[0].get("description", "")) > len(item.get("description", "")):
            item["description"] = ld_items[0]["description"]
        return item

    body_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    cleaned = _clean_company_candidate(body_text, item["title"], strict=True)
    if cleaned:
        item["company_name"] = cleaned
    else:
        for el in soup.find_all(["h2", "h3", "span", "div", "p", "a"]):
            cls_hint = " ".join(el.get("class") or []).lower()
            if any(k in cls_hint for k in ("company", "employer", "firm", "author")):
                cand = _clean_company_candidate(el.get_text(" ", strip=True), item["title"], strict=False)
                if cand:
                    item["company_name"] = cand
                    break

    if len(body_text) > len(item.get("description", "")):
        item["description"] = body_text[:1500]
    return item


async def _resolve_offer_commute(
    offer_text: str,
    home_lat: Optional[float],
    home_lon: Optional[float],
    company_name: str = "",
    url: str = "",
    soup: Optional[BeautifulSoup] = None,
    allow_company_guess: bool = True,
) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    if home_lat is None or home_lon is None:
        return None, None, None
    if DISTANT_REGION_RE.search(offer_text or "") or DISTANT_PSC_RE.search(offer_text or ""):
        return None, None, None
    loc = _extract_best_locality(
        offer_text,
        company_name=company_name,
        url=url,
        soup=soup,
        allow_company_guess=allow_company_guess,
    )
    if not loc:
        return None, None, None
    coords = await geocode_location(loc)
    if not coords:
        return None, None, loc
    c_min, c_km = await estimate_commute(home_lat, home_lon, coords[0], coords[1])
    return c_min, c_km, loc


async def run_job_boards_cycle(force_boards: Optional[List[str]] = None) -> Dict[str, int]:
    stats = {"boards_queried": 0, "new_offers": 0, "skipped_duplicates": 0, "errors": 0}

    with db_session() as conn:
        profile_row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
        if not profile_row:
            return stats
        profile = dict(profile_row)

        if force_boards is not None:
            enabled_boards = force_boards
        else:
            enabled_boards = []
            if profile.get("use_jobs_cz"):
                enabled_boards.append("jobs.cz")
            if profile.get("use_prace_cz"):
                enabled_boards.append("prace.cz")
            if profile.get("use_jenprace_cz"):
                enabled_boards.append("jenprace.cz")
            if profile.get("use_linkedin"):
                enabled_boards.append("linkedin.com")
            if profile.get("use_uradprace_cz"):
                enabled_boards.append("uradprace.cz")

        if not enabled_boards:
            _log("Žádný pracovní portál není povolen.")
            return stats

        existing_urls = {r["url"].strip().lower() for r in conn.execute("SELECT url FROM job_offers").fetchall()}
        existing_hashes = {r["content_hash"] for r in conn.execute("SELECT content_hash FROM job_offers").fetchall()}
        existing_keys = {
            _norm_key(r["title"], r["company_name_raw"]): r["id"]
            for r in conn.execute("SELECT id, title, company_name_raw FROM job_offers").fetchall()
        }

    home_location = (profile.get("home_location") or "Bohumín").strip()
    home_lat = profile.get("home_lat")
    home_lon = profile.get("home_lon")
    max_min = profile.get("max_commute_min") or 30
    max_km = float(profile.get("max_commute_km") or 20)
    search_terms = await get_search_terms_for_profile(profile, max_terms=8)

    for board in enabled_boards:
        for term in search_terms:
            raw_items: List[Dict[str, str]] = []
            last_err: Optional[Exception] = None

            if board == "uradprace.cz":
                stats["boards_queried"] += 1
                try:
                    raw_items = await _fetch_mpsv_offers(term, home_location)
                except Exception as exc:
                    last_err = exc
            else:
                urls = _build_board_urls(board, home_location, term)
                for idx, url in enumerate(urls):
                    stats["boards_queried"] += 1
                    try:
                        raw_items = await _fetch_board_search(board, url)
                        last_err = None
                        break
                    except Exception as exc:
                        last_err = exc
                        if idx < len(urls) - 1:
                            continue

            if board == "linkedin.com":
                await asyncio.sleep(6.0)
            if last_err is not None and not raw_items:
                stats["errors"] += 1
                _log(f"Chyba u {board} ({term}): {type(last_err).__name__}: {_sanitize_err(last_err)}")
                if board == "linkedin.com" and isinstance(last_err, httpx.HTTPStatusError) and last_err.response.status_code == 429:
                    _log("LinkedIn vrátil 429 (rate limit), přeskakuji zbývající dotazy pro LinkedIn v tomto běhu.")
                    break
                continue

            for item in raw_items:
                title = item["title"].strip()
                offer_url = item["url"].strip()

                if "jenprace.cz/nabidky/" in offer_url.lower() and "/nabidka/" not in offer_url.lower():
                    continue
                if board != "uradprace.cz" and not _looks_like_job(title, offer_url, offer_url):
                    continue

                u_low = offer_url.lower()
                if u_low in existing_urls:
                    stats["skipped_duplicates"] += 1
                    continue

                if item.get("company_name", "").lower() in BOARD_NAMES:
                    item = await _enrich_offer_detail(item)

                company_name = (
                    _clean_company_candidate(item["company_name"], title, strict=False)
                    or item["company_name"].strip()
                    or board
                )
                desc = item.get("description", "").strip()

                combined_text = f"{title} {desc} {company_name} {offer_url}"

                content_hash = hashlib.sha256(
                    f"{title.lower()}|{company_name.lower()}|{desc[:250].lower()}".encode("utf-8")
                ).hexdigest()
                n_key = _norm_key(title, company_name)

                if content_hash in existing_hashes or n_key in existing_keys:
                    stats["skipped_duplicates"] += 1
                    continue

                c_min, c_km, resolved_loc = await _resolve_offer_commute(
                    combined_text,
                    home_lat,
                    home_lon,
                    company_name=company_name,
                    url=offer_url,
                    allow_company_guess=(board != "linkedin.com"),
                )

                if (not resolved_loc or c_km is None) and board in {"jobs.cz", "prace.cz", "jenprace.cz"}:
                    item = await _enrich_offer_detail(item, force_detail=True)
                    desc = item.get("description", "").strip()
                    combined_text = f"{title} {desc} {company_name} {offer_url}"
                    c_min, c_km, resolved_loc = await _resolve_offer_commute(
                        combined_text,
                        home_lat,
                        home_lon,
                        company_name=company_name,
                        url=offer_url,
                        allow_company_guess=False,
                    )

                if home_lat is not None and home_lon is not None:
                    if not resolved_loc or c_km is None:
                        continue
                if c_min is not None and c_min > max_min:
                    continue
                if c_km is not None and float(c_km) > max_km:
                    continue

                agency_flag = 1 if _is_agency_offer(company_name, title, desc) else 0
                full_desc = f"[Zdroj: {board}] {desc}".strip()

                with db_session() as conn:
                    cur = conn.execute(
                        """
                        INSERT INTO job_offers (
                            company_id, company_name_raw, title, url, source_type, board_name,
                            is_agency, commute_minutes, commute_km, description_clean, content_hash, status
                        ) VALUES (NULL, ?, ?, ?, 'job_board', ?, ?, ?, ?, ?, ?, 'new')
                        """,
                        (
                            company_name,
                            title,
                            offer_url,
                            board,
                            agency_flag,
                            c_min,
                            c_km,
                            full_desc[:1500],
                            content_hash,
                        ),
                    )
                    existing_urls.add(u_low)
                    existing_hashes.add(content_hash)
                    existing_keys[n_key] = cur.lastrowid
                    stats["new_offers"] += 1

    _log(f"Dokončen sběr z portálů: {stats}")
    return stats
