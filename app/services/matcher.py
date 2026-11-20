import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional
import httpx
from app.config import settings
from app.database import db_session


class RateLimitError(Exception):
    pass


CORE_ROLE_PATTERNS = [
    r"\bse[řr]izova[čc]\w*\b",
    r"\bmechanik\w*\b",
    r"\b[úu]dr[žz]b[áa][řr]\w*\b",
    r"\b[úu]dr[žz]b\w*\b",
    r"\bdiagnostik\w*\b",
    r"\bmechatronik\w*\b",
    r"\belektrik[áa][řr]\w*\b",
    r"\belektro\s*[úu]dr[žz]b\w*\b",
    r"\bcnc\b",
]

LOW_FIT_ROLE_RE = re.compile(
    r"\b(?:skladn[íi]k\w*|manipulant\w*|pokladn[íi]\w*|ukl[íi]ze[čc]\w*|kur[ýy]r\w*|řidič\w*\s+sk\.\s*c)\b",
    flags=re.IGNORECASE,
)


def _log(msg: str) -> None:
    print(f"[matcher] {msg}", file=sys.stdout, flush=True)


def _sanitize_error_text(exc: Exception, gemini_key: str = "") -> str:
    msg = str(exc)
    if gemini_key:
        msg = msg.replace(gemini_key, "***")
    msg = re.sub(r"(key=)[^&\s]+", r"\1***", msg, flags=re.IGNORECASE)
    return msg[:300]


def _candidate_models() -> List[str]:
    models: List[str] = []
    primary = (getattr(settings, "llm_model", "") or os.getenv("LLM_MODEL", "")).strip()
    if primary:
        models.append(primary)
    extra = os.getenv("LLM_FALLBACK_MODELS", "")
    for item in extra.split(","):
        m = item.strip()
        if m and m not in models:
            models.append(m)
    return models or ["google/gemma-4-31b-it:free", "google/gemma-4-26b-a4b-it:free", "openrouter/free"]


def _extract_json_obj(raw_text: str) -> Dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    candidate = match.group(0) if match else text
    data = json.loads(candidate)
    return data if isinstance(data, dict) else {}


def _parse_eval_payload(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not parsed or "match_score" not in parsed:
        return None
    try:
        score = max(0, min(100, int(float(parsed.get("match_score", 50)))))
    except (TypeError, ValueError):
        return None
    skills = parsed.get("skill_intersections")
    if not isinstance(skills, list):
        skills = []
    return {
        "match_score": score,
        "skill_intersections": [str(s) for s in skills[:12]],
        "pros": str(parsed.get("pros") or "").strip(),
        "cons": str(parsed.get("cons") or "").strip(),
        "cv_tips": str(parsed.get("cv_tips") or "").strip(),
    }


def _heuristic_evaluation(offer: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    cv_text = (profile.get("cv_text") or "").lower()
    neg_raw = profile.get("negative_patterns") or ""
    neg_items = [p.strip().lower() for p in re.split(r"[,\n;]+", neg_raw) if p.strip()]
    title_low = (offer.get("title") or "").lower()
    desc_low = (offer.get("description_clean") or "").lower()
    comp_low = (offer.get("company_name_raw") or "").lower()
    haystack = f"{title_low} {desc_low} {comp_low}"

    tokens = [
        w
        for w in set(re.findall(r"[a-záčďéěíňóřšťúůýž]{4,}", cv_text))
        if w not in {"praxe", "práce", "obor", "roku", "let"}
    ]
    intersections = [w for w in tokens if w in haystack][:12]

    core_hits = sum(1 for pat in CORE_ROLE_PATTERNS if re.search(pat, title_low))
    if core_hits == 0:
        core_hits = sum(1 for pat in CORE_ROLE_PATTERNS if re.search(pat, haystack))

    score = 40 + min(25, len(intersections) * 5) + min(20, core_hits * 10)
    pros_parts: List[str] = []
    cons_list: List[str] = []

    if core_hits > 0:
        pros_parts.append("Odpovídající technické zaměření pozice")
    elif LOW_FIT_ROLE_RE.search(title_low):
        score -= 15
        cons_list.append("Pozice mimo hlavní technické zaměření (sklad/manipulace/provoz)")

    if intersections:
        pros_parts.append(f"Průnik klíčových termínů z CV: {', '.join(intersections)}")

    if offer.get("is_agency"):
        score -= 25
        cons_list.append("Agenturní zaměstnávání / zprostředkování")

    for neg in neg_items:
        if neg in haystack:
            score -= 20
            cons_list.append(f"Shoda s nežádoucím vzorem: {neg}")

    max_min = profile.get("max_commute_min")
    max_km = profile.get("max_commute_km")
    c_min = offer.get("effective_commute_min") if offer.get("effective_commute_min") is not None else offer.get("commute_minutes")
    c_km = offer.get("effective_commute_km") if offer.get("effective_commute_km") is not None else offer.get("commute_km")

    if c_km is not None and float(c_km) <= 20.0:
        score += 5
        pros_parts.append(f"Blízký dojezd ({c_km} km)")
    if max_min and c_min is not None and c_min > max_min:
        score -= 15
        cons_list.append(f"Dojezd {c_min} min je nad limitem {max_min} min")
    if max_km and c_km is not None and float(c_km) > float(max_km):
        score -= 15
        cons_list.append(f"Vzdálenost {c_km} km je nad limitem {max_km} km")

    score = max(0, min(100, int(score)))
    pros = "; ".join(pros_parts) if pros_parts else "Základní technická nabídka v databázi."
    cons = "; ".join(cons_list) if cons_list else "Žádné zásadní konflikty s nastavenými pravidly."
    tips = "Zdůrazněte v CV konkrétní zkušenosti s údržbou, diagnostikou a seřizováním zařízení."
    return {
        "match_score": score,
        "skill_intersections": intersections,
        "pros": pros,
        "cons": cons,
        "cv_tips": tips,
    }


def _build_prompt(offer: Dict[str, Any], profile: Dict[str, Any]) -> str:
    c_min = offer.get("effective_commute_min") if offer.get("effective_commute_min") is not None else offer.get("commute_minutes")
    c_km = offer.get("effective_commute_km") if offer.get("effective_commute_km") is not None else offer.get("commute_km")
    c_city = offer.get("company_city") or ""

    return f"""
Vyhodnoť vhodnost pracovní nabídky pro kandidáta (0 až 100 bodů).
Drž se striktně poskytnutých podkladů – nevymýšlej si obor firmy, technologie ani provozní údaje, které nejsou v zadání. Pokud něco v popisu chybí, napiš to věcně bez dohadů.

Zásady hodnocení:
- Nejvyšší váhu má shoda role a dovedností s CV (zejména údržba, mechanika, seřizování, diagnostika, elektro/mechatronika).
- Zohledni dojezdovou vzdálenost a čas vůči limitu kandidáta (výrazně delší dojezd snižuje vhodnost).
- Agenturní nabídky ("Agentura: ano") penalizuj a uveď to v "cons".

Profil kandidáta:
- Bydliště: {profile.get("home_location") or "neuvedeno"}
- Max. dojezd: {profile.get("max_commute_min") or 30} min / {profile.get("max_commute_km") or 20} km
- Nevhodné vzory: {profile.get("negative_patterns") or "neuvedeno"}
- CV: {(profile.get("cv_text") or "")[:2500]}

Nabídka:
- Firma: {offer.get("company_name_raw") or ""}
- Lokalita firmy: {c_city or "neuvedeno"}
- Odhadovaný dojezd: {c_min if c_min is not None else "neuvedeno"} min / {c_km if c_km is not None else "neuvedeno"} km
- Pozice: {offer.get("title") or ""}
- URL: {offer.get("url") or ""}
- Agentura: {"ano" if offer.get("is_agency") else "ne"}
- Popis: {(offer.get("description_clean") or "")[:1200]}

Vrať POUZE validní JSON objekt bez markdownu:
{{
  "match_score": 75,
  "skill_intersections": ["údržba", "diagnostika"],
  "pros": "Stručná pozitiva",
  "cons": "Stručná rizika nebo nesoulad",
  "cv_tips": "Konkrétní tip pro CV / pohovor"
}}
""".strip()


async def _call_gemini_direct(prompt: str) -> Optional[Dict[str, Any]]:
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        return None
    model = (os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite") or "gemini-3.1-flash-lite").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "x-goog-api-key": gemini_key,
        "Content-Type": "application/json",
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    async with httpx.AsyncClient(timeout=40.0) as client:
        for attempt in range(3):
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 429:
                if attempt < 2:
                    await asyncio.sleep(3.0 * (attempt + 1))
                    continue
                raise RateLimitError("Gemini 429 Too Many Requests")
            if resp.status_code in (500, 502, 503, 504):
                if attempt < 2:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if candidates:
                parts = ((candidates[0].get("content") or {}).get("parts")) or []
                text = "\n".join(pt.get("text", "") for pt in parts if isinstance(pt, dict) and pt.get("text"))
                parsed = _parse_eval_payload(_extract_json_obj(text))
                if parsed:
                    return parsed
            return None
    return None


async def _llm_evaluation(offer: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    api_key = (getattr(settings, "llm_api_key", "") or os.getenv("LLM_API_KEY", "")).strip()
    if api_key == "vas_api_klic_zde":
        api_key = ""
    base_url = (getattr(settings, "llm_api_base", "") or os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key and not gemini_key:
        return _heuristic_evaluation(offer, profile)

    prompt = _build_prompt(offer, profile)
    hit_rate_limit = False

    if gemini_key:
        try:
            gem_res = await _call_gemini_direct(prompt)
            if gem_res:
                return gem_res
        except RateLimitError:
            hit_rate_limit = True
            _log("Gemini narazilo na limit (429), zkouším záložní modely.")
        except Exception as exc:
            _log(f"Přímé Gemini API selhalo ({type(exc).__name__}: {_sanitize_error_text(exc, gemini_key)}), zkouším OpenRouter.")

    if api_key:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8095",
            "X-Title": "AI Job Assistant",
        }
        url = f"{base_url}/chat/completions"

        for model_name in _candidate_models():
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": "Odpovídej výhradně validním JSON objektem."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            }
            try:
                async with httpx.AsyncClient(timeout=40.0) as client:
                    for attempt in range(2):
                        resp = await client.post(url, headers=headers, json=payload)
                        if resp.status_code == 429:
                            hit_rate_limit = True
                            if attempt == 0:
                                await asyncio.sleep(4.0)
                                continue
                            _log(f"Model {model_name} vrátil 429, zkouším další model v řetězu.")
                            break
                        if resp.status_code == 404:
                            _log(f"Model {model_name} vrátil 404, zkouším další model v řetězu.")
                            break
                        resp.raise_for_status()
                        content = resp.json()["choices"][0]["message"]["content"]
                        parsed = _parse_eval_payload(_extract_json_obj(content))
                        if parsed:
                            return parsed
                        _log(f"Model {model_name} nevrátil validní JSON, zkouším další model.")
                        break
            except Exception as exc:
                _log(f"Chyba u modelu {model_name} ({type(exc).__name__}: {_sanitize_error_text(exc)}), zkouším další model.")

    if hit_rate_limit:
        raise RateLimitError("Všechny dostupné modely vrátily 429 Too Many Requests.")

    _log(f"Fallback na heuristiku u nabídky '{offer.get('title')}'.")
    return _heuristic_evaluation(offer, profile)


async def evaluate_pending_offers(limit: int = 50) -> int:
    with db_session() as conn:
        profile_row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
        if not profile_row:
            return 0
        profile = dict(profile_row)
        pending = [
            dict(r)
            for r in conn.execute(
                """
                SELECT j.*,
                       c.city AS company_city,
                       COALESCE(j.commute_minutes, c.commute_minutes) AS effective_commute_min,
                       COALESCE(j.commute_km, c.commute_km) AS effective_commute_km
                FROM job_offers j
                LEFT JOIN companies c ON c.id = j.company_id
                LEFT JOIN job_evaluations e ON e.job_id = j.id
                WHERE j.status != 'hidden'
                  AND (e.job_id IS NULL OR e.evaluated_hash != j.content_hash)
                ORDER BY
                    CASE
                        WHEN COALESCE(j.commute_km, c.commute_km) IS NULL THEN 1
                        WHEN COALESCE(j.commute_km, c.commute_km) <= 35.0 THEN 0
                        ELSE 2
                    END ASC,
                    j.first_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]

    evaluated_count = 0
    for idx, offer in enumerate(pending):
        if idx > 0:
            await asyncio.sleep(3.5)
        try:
            res = await _llm_evaluation(offer, profile)
        except RateLimitError:
            _log("Dosažen rate limit (429), zbytek dávky odkládám na další běh.")
            break
        with db_session() as conn:
            conn.execute(
                """
                INSERT INTO job_evaluations (
                    job_id, match_score, skill_intersections, pros, cons, cv_tips, evaluated_hash, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(job_id) DO UPDATE SET
                    match_score = excluded.match_score,
                    skill_intersections = excluded.skill_intersections,
                    pros = excluded.pros,
                    cons = excluded.cons,
                    cv_tips = excluded.cv_tips,
                    evaluated_hash = excluded.evaluated_hash,
                    evaluated_at = CURRENT_TIMESTAMP
                """,
                (
                    offer["id"],
                    res["match_score"],
                    json.dumps(res["skill_intersections"], ensure_ascii=False),
                    res["pros"],
                    res["cons"],
                    res["cv_tips"],
                    offer["content_hash"],
                ),
            )
        evaluated_count += 1

    _log(f"Vyhodnoceno nabídek: {evaluated_count}")
    return evaluated_count