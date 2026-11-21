import asyncio
import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Tuple
import httpx
from app.config import settings

DEFAULT_SEARCH_TERMS = [
    "údržba",
    "mechanik",
    "seřizovač",
    "diagnostika",
]

_CACHE_TTL_SEC = 24 * 3600
_QUERY_CACHE: Dict[str, Tuple[float, List[str]]] = {}


def _log(msg: str) -> None:
    print(f"[query_planner] {msg}", file=sys.stdout, flush=True)


def _sanitize_err(exc: Exception) -> str:
    msg = re.sub(r"([?&]key=)[^&\s]+", r"\1***", str(exc), flags=re.IGNORECASE)
    return msg[:300]


def parse_keywords_list(raw: str) -> List[str]:
    if not raw:
        return []
    parts = re.split(r"[,\n;]+", raw)
    result: List[str] = []
    seen = set()
    for p in parts:
        cleaned = re.sub(r"\s+", " ", p).strip(" -•·\t")
        if not cleaned or len(cleaned) < 2 or len(cleaned) > 50:
            continue
        low = cleaned.lower()
        if low not in seen:
            seen.add(low)
            result.append(cleaned)
    return result


def _extract_string_array(raw_text: str) -> List[str]:
    text = (raw_text or "").strip()
    if not text:
        return []

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if isinstance(x, (str, int, float)) and str(x).strip()]
    except Exception:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\[", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start() :])
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if isinstance(x, (str, int, float)) and str(x).strip()]
        except Exception:
            continue

    return []


def _candidate_models() -> List[str]:
    models: List[str] = []
    primary = (getattr(settings, "llm_model", None) or "").strip()
    if primary:
        models.append(primary)

    extra = getattr(settings, "llm_fallback_models", None)
    if isinstance(extra, (list, tuple)):
        for m in extra:
            ms = str(m).strip()
            if ms and ms not in models:
                models.append(ms)
    elif isinstance(extra, str) and extra.strip():
        for part in re.split(r"[,\n;]+", extra):
            ms = part.strip()
            if ms and ms not in models:
                models.append(ms)

    for m in [
        "google/gemma-3-27b-it:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen3-coder:free",
        "openrouter/free",
    ]:
        if m not in models:
            models.append(m)

    return models


async def _call_openrouter(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }
    resp = await client.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return str(msg.get("content") or "").strip()


async def _call_gemini(system_prompt: str, user_prompt: str) -> str:
    api_key = (
        getattr(settings, "gemini_api_key", None)
        or os.getenv("GEMINI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return ""

    gemini_model = (
        getattr(settings, "gemini_model", None)
        or os.getenv("GEMINI_MODEL")
        or "gemini-2.5-flash"
    ).strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    async with httpx.AsyncClient(timeout=35.0) as client:
        resp = await client.post(url, params={"key": api_key}, json=payload)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()


def _merge_and_filter_terms(
    ai_terms: List[str],
    user_keywords: List[str],
    negative_patterns: str,
    max_terms: int = 8,
) -> List[str]:
    neg_terms = [n.strip().lower() for n in parse_keywords_list(negative_patterns) if len(n.strip()) >= 3]
    ordered: List[str] = []
    seen = set()

    def _add(term: str) -> None:
        t = re.sub(r"\s+", " ", (term or "")).strip(" -•·\"'.,;:/\\")
        if not t or len(t) < 2 or len(t) > 40:
            return
        if len(t.split()) > 3:
            return
        low = t.lower()
        if low in seen:
            return
        if any(neg in low for neg in neg_terms):
            return
        seen.add(low)
        ordered.append(t)

    for kw in user_keywords:
        _add(kw)

    for item in ai_terms:
        _add(item)
        if len(ordered) >= max_terms:
            break

    if not ordered:
        for fallback in DEFAULT_SEARCH_TERMS:
            _add(fallback)

    return ordered[:max_terms]


async def get_search_terms_for_profile(profile: Dict[str, Any], max_terms: int = 8) -> List[str]:
    user_keywords = parse_keywords_list(str(profile.get("search_keywords") or ""))
    cv_text = (profile.get("cv_text") or "").strip()
    negative_patterns = (profile.get("negative_patterns") or "").strip()

    cache_payload = json.dumps(
        {
            "user_keywords": user_keywords,
            "cv_text": cv_text[:2500],
            "negative_patterns": negative_patterns,
            "max_terms": max_terms,
        },
        ensure_ascii=False,
    )
    cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()

    cached = _QUERY_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    if not cv_text and not user_keywords:
        terms = _merge_and_filter_terms([], [], negative_patterns, max_terms=max_terms)
        _QUERY_CACHE[cache_key] = (time.time(), terms)
        return terms

    has_llm_key = bool(settings.llm_api_key and settings.llm_api_key != "vas_api_klic_zde")
    has_gemini_key = bool(
        (getattr(settings, "gemini_api_key", None) or os.getenv("GEMINI_API_KEY") or "").strip()
    )

    system_prompt = "Odpovídej pouze validním JSON polem krátkých českých vyhledávacích výrazů."
    user_prompt = f"""
Navrhni až {max_terms} krátkých vyhledávacích výrazů (1–2 slova, případně běžný název profese) pro vyhledávání pracovních inzerátů na pracovních portálech.
Vycházej z profilu kandidáta a zohledni, že různí zaměstnavatelé mohou stejnou roli pojmenovat odlišně (např. synonyma, příbuzné profese, provozní vs. servisní pojmenování).

Uživatelem zadaná klíčová slova / směry:
{", ".join(user_keywords) if user_keywords else "neuvedeno"}

Nevhodné vzory (nezařazovat ani odvozené varianty):
{negative_patterns or "neuvedeno"}

Výtah z CV kandidáta:
{cv_text[:2200] if cv_text else "neuvedeno"}

Pravidla:
- Vrať POUZE čisté JSON pole řetězců bez markdownu, např. ["údržba", "seřizovač", "provozní zámečník"].
- Výrazy musí být krátké, konkrétní a přirozené pro vyhledávání v českých inzerátech.
- Zařaď klíčová slova z profilu/CV i příbuzné profese.
""".strip()

    ai_terms: List[str] = []
    if has_llm_key:
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8095",
            "X-Title": "AI Job Assistant",
        }
        url = f"{settings.llm_api_base.rstrip('/')}/chat/completions"
        models = _candidate_models()

        async with httpx.AsyncClient(timeout=35.0) as client:
            for idx, model in enumerate(models):
                try:
                    raw = await _call_openrouter(client, url, headers, model, system_prompt, user_prompt)
                    parsed = _extract_string_array(raw)
                    if parsed:
                        ai_terms = parsed
                        break
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    _log(f"HTTP chyba při plánování dotazů ({model}, {status}): {_sanitize_err(exc)}")
                    if status in (429, 502, 503, 504) and idx < len(models) - 1:
                        await asyncio.sleep(1.5)
                    continue
                except Exception as exc:
                    _log(f"Chyba při plánování dotazů ({model}): {type(exc).__name__}: {_sanitize_err(exc)}")
                    continue

    if not ai_terms and has_gemini_key:
        try:
            raw_gem = await _call_gemini(system_prompt, user_prompt)
            ai_terms = _extract_string_array(raw_gem)
        except Exception as exc:
            _log(f"Chyba Gemini fallbacku při plánování dotazů: {type(exc).__name__}: {_sanitize_err(exc)}")

    final_terms = _merge_and_filter_terms(ai_terms, user_keywords, negative_patterns, max_terms=max_terms)
    _log(f"Hledané výrazy pro běh ({len(final_terms)}): {final_terms}")
    _QUERY_CACHE[cache_key] = (time.time(), final_terms)
    return final_terms