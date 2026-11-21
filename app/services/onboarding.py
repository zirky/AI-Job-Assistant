import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List
import httpx
from app.config import settings


def _log(msg: str) -> None:
    print(f"[onboarding] {msg}", file=sys.stdout, flush=True)


def _extract_json_array(raw_text: str) -> List[Dict[str, Any]]:
    text = (raw_text or "").strip()
    if not text:
        return []

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    except Exception:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\[", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start() :])
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
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

    default_fallbacks = [
        "google/gemma-3-27b-it:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen3-coder:free",
        "openrouter/free",
    ]
    for m in default_fallbacks:
        if m not in models:
            models.append(m)

    return models


async def _call_gemini_fallback(system_prompt: str, user_prompt: str) -> str:
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

    _log(f"Zkouším Gemini fallback ({gemini_model})...")
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(url, params={"key": api_key}, json=payload)
        _log(f"HTTP status z Gemini: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()


async def _call_openrouter_model(
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
    _log(f"Volám {url} s modelem {model}...")
    resp = await client.post(url, headers=headers, json=payload)
    _log(f"HTTP status z LLM ({model}): {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        _log(f"Prázdné choices u modelu {model}: {str(data)[:400]}")
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "").strip()


async def suggest_companies_from_profile(profile: Dict[str, Any]) -> List[Dict[str, str]]:
    has_llm_key = bool(settings.llm_api_key and settings.llm_api_key != "vas_api_klic_zde")
    has_gemini_key = bool(
        (getattr(settings, "gemini_api_key", None) or os.getenv("GEMINI_API_KEY") or "").strip()
    )

    if not has_llm_key and not has_gemini_key:
        _log("Chybí LLM_API_KEY i GEMINI_API_KEY.")
        return []

    home_location = (profile.get("home_location") or "").strip()
    max_commute_min = profile.get("max_commute_min") or 30
    max_commute_km = profile.get("max_commute_km") or 20
    cv_text = (profile.get("cv_text") or "").strip()
    negative_patterns = (profile.get("negative_patterns") or "").strip()

    if not home_location or not cv_text:
        _log("Chybí home_location nebo cv_text.")
        return []

    system_prompt = "Odpovídej pouze validním JSON polem."
    user_prompt = f"""
Navrhni až 12 reálných průmyslových, výrobních, energetických, dopravních nebo servisních firem v obci {home_location} a v jejím bezprostředním okolí.
Zaměř se na technické pozice: údržba, seřizování, diagnostika, mechanika, elektrika, provoz strojů.

Parametry kandidáta:
- Výchozí obec: {home_location} (zařaď přednostně i reálné provozy přímo v obci {home_location}, pokud existují)
- Cílový dojezd autem: max. cca {max_commute_min} minut a cca {max_commute_km} km
- Částečně nevhodné vzory: {negative_patterns or "neuvedeno"}
- CV (bez osobních údajů):
{cv_text[:2500]}

Pravidla:
- Pouze reálně existující firmy a jejich reálné sídlo/provoz v blízkém okolí {home_location}. Nevymýšlej fiktivní názvy ani neexistující pobočky.
- Vyhni se pracovním agenturám a firmám, kde už kandidát podle CV dříve působil.
- Do pole "city" uváděj vždy jen jednu konkrétní obec/město (nikdy lomítka jako "Město A / Město B").
- Pokud neznáš ověřenou URL kariérní stránky, dej "".
- Vrať POUZE čisté JSON pole bez markdownu:
[{{"name": "Název firmy", "city": "Obec", "career_url": "", "notes": "Stručný důvod"}}]
""".strip()

    _log(f"Hledám návrhy firem pro {home_location} (limit {max_commute_min} min / {max_commute_km} km)...")

    content = ""
    if has_llm_key:
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8095",
            "X-Title": "AI Job Assistant",
        }
        url = f"{settings.llm_api_base.rstrip('/')}/chat/completions"
        models = _candidate_models()

        async with httpx.AsyncClient(timeout=45.0) as client:
            for idx, model in enumerate(models):
                try:
                    raw = await _call_openrouter_model(
                        client, url, headers, model, system_prompt, user_prompt
                    )
                    if _extract_json_array(raw):
                        content = raw
                        _log(f"Úspěšná odpověď z modelu {model}.")
                        break
                    _log(f"Model {model} nevrátil použitelné JSON pole, zkouším další.")
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    _log(f"HTTP chyba u modelu {model} ({status}): {exc}")
                    if status in (429, 502, 503, 504) and idx < len(models) - 1:
                        await asyncio.sleep(2.0)
                    continue
                except Exception as exc:
                    _log(f"Chyba při volání modelu {model}: {type(exc).__name__}: {exc}")
                    continue

    if not content and has_gemini_key:
        try:
            raw_gem = await _call_gemini_fallback(system_prompt, user_prompt)
            if _extract_json_array(raw_gem):
                content = raw_gem
                _log("Úspěšná odpověď z Gemini fallbacku.")
        except Exception as exc:
            _log(f"Chyba při volání Gemini fallbacku: {type(exc).__name__}: {exc}")

    if not content:
        _log("Žádný model nevrátil platné návrhy firem.")
        return []

    _log(f"Odpověď LLM: {content[:1200]}")
    items = _extract_json_array(content)
    cleaned: List[Dict[str, str]] = []
    for item in items[:15]:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        career_url = str(item.get("career_url") or "").strip()
        if not career_url.startswith(("http://", "https://")):
            career_url = ""
        cleaned.append(
            {
                "name": name,
                "city": str(item.get("city") or "").strip(),
                "career_url": career_url,
                "notes": str(item.get("notes") or "").strip(),
            }
        )

    _log(f"Počet platných návrhů: {len(cleaned)}")
    return cleaned