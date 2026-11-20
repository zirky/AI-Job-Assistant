
import json

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





async def suggest_companies_from_profile(profile: Dict[str, Any]) -> List[Dict[str, str]]:

    if not settings.llm_api_key or settings.llm_api_key == "vas_api_klic_zde":

        _log("Chybí LLM_API_KEY.")

        return []



    home_location = (profile.get("home_location") or "").strip()

    max_commute_min = profile.get("max_commute_min") or 30

    max_commute_km = profile.get("max_commute_km") or 20

    cv_text = (profile.get("cv_text") or "").strip()

    negative_patterns = (profile.get("negative_patterns") or "").strip()



    if not home_location or not cv_text:

        _log("Chybí home_location nebo cv_text.")

        return []



    prompt = f"""

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



    headers = {

        "Authorization": f"Bearer {settings.llm_api_key}",

        "Content-Type": "application/json",

        "HTTP-Referer": "http://localhost:8095",

        "X-Title": "AI Job Assistant",

    }

    payload = {

        "model": settings.llm_model,

        "messages": [

            {"role": "system", "content": "Odpovídej pouze validním JSON polem."},

            {"role": "user", "content": prompt},

        ],

        "temperature": 0.2,

    }



    url = f"{settings.llm_api_base.rstrip('/')}/chat/completions"

    _log(f"Volám {url} s modelem {settings.llm_model} pro {home_location} (limit {max_commute_min} min / {max_commute_km} km)...")

    try:

        async with httpx.AsyncClient(timeout=45.0) as client:

            resp = await client.post(url, headers=headers, json=payload)

            _log(f"HTTP status z LLM: {resp.status_code}")

            resp.raise_for_status()

            data = resp.json()

            choices = data.get("choices") or []

            if not choices:

                _log(f"Prázdné choices v odpovědi LLM: {str(data)[:500]}")

                return []

            message = choices[0].get("message") or {}

            content = message.get("content") or ""

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

                cleaned.append({

                    "name": name,

                    "city": str(item.get("city") or "").strip(),

                    "career_url": career_url,

                    "notes": str(item.get("notes") or "").strip(),

                })

            _log(f"Počet platných návrhů: {len(cleaned)}")

            return cleaned

    except Exception as exc:

        _log(f"Chyba při volání LLM: {type(exc).__name__}: {exc}")

        return []

