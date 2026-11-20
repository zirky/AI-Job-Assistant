STRUKTURNÍ LAICKÝ NÁVOD: SPUŠTĚNÍ A PROVOZ AI ASISTENTA PRO HLEDÁNÍ PRÁCE

1. CO POTŘEBUJETE PŘED ZAHÁJENÍM
- Počítač nebo domácí server (např. Linux/Ubuntu) a přístup k internetu.
- Pro běh v Dockeru: nainstalovaný Docker a Docker Compose.
- Pro běh bez Dockeru: nainstalovaný Python 3.10 nebo novější.
- API klíč pro AI (aby systém uměl vyhodnocovat shodu nabídek s vaším CV).
- E-mailový účet podporující SMTP (např. Seznam.cz), pokud chcete dostávat souhrnné výpisy do e-mailu.

2. JAK ZÍSKAT API KLÍČ PRO AI (LAICKY)
Možnost A – OpenRouter (vhodné pro rychlé vyzkoušení a bezplatné/levné modely):
1) Založte si účet na webu openrouter.ai.
2) Přihlaste se a v sekci API Keys klikněte na vytvoření nového klíče (Create Key).
3) Vygenerovaný klíč si zkopírujte (vypadá jako dlouhý řetězec znaků). Pozor: klíč nikomu neukazujte ani ho nenahrávejte na veřejné weby.

Možnost B – Google AI Studio (Gemini API):
1) Otevřete Google AI Studio (aistudio.google.com) a přihlaste se svým Google účtem.
2) V levém nebo horním menu zvolte "Get API key" a vytvořte klíč pro svůj projekt.
3) Klíč si zkopírujte.
Poznámka k limitům (zdarma): Bezplatné tarify mají omezení na počet požadavků za minutu (RPM) i za den (RPD). Při testování proto neklikat na hromadné vyhodnocování desetkrát během jedné minuty, jinak může API dočasně vrátit hlášku o překročení limitu.

3. NASTAVENÍ SOUBORU .env
V hlavní složce projektu si zkopírujte vzorový soubor .env.example na soubor .env a doplňte své údaje:
- LLM_API_KEY=... (váš API klíč)
- LLM_API_BASE=... (např. adresa OpenRouteru nebo zvoleného API)
- LLM_MODEL=... (název vybraného modelu)

Pokud chcete e-mailové souhrny (např. přes Seznam.cz, kdy je odesílač i příjemce stejná adresa):
- SMTP_HOST=smtp.seznam.cz
- SMTP_PORT=587
- SMTP_USER=vas.ucet@seznam.cz
- SMTP_PASSWORD=vase_heslo_nebo_heslo_pro_aplikaci
- SMTP_FROM=vas.ucet@seznam.cz
- SMTP_TLS=true

4. SPUŠTĚNÍ PŘES DOCKER (DOPORUČENO PRO SERVER)
1) Otevřete terminál ve složce projektu.
2) Zkontrolujte, že máte připravený soubor .env.
3) Spusťte sestavení a start kontejneru:
   docker compose up -d --build
4) Ověřte, že aplikace běží:
   docker logs --tail 30 job-assistant
5) V prohlížeči otevřete adresu aplikace (např. http://ADRESA_SERVERU:8080).
6) Kdykoliv změníte kód nebo šablony, je potřeba kontejner přestavět příkazem:
   docker compose up -d --build

5. SPUŠTĚNÍ BEZ DOCKERU (PŘÍMO V PYTHONU)
1) Otevřete terminál ve složce projektu.
2) Vytvořte virtuální prostředí:
   python3 -m venv .venv
3) Aktivujte virtuální prostředí:
   - Na Linuxu / macOS: source .venv/bin/activate
   - Na Windows (PowerShell): .venv\Scripts\Activate.ps1
4) Nainstalujte potřebné knihovny:
   pip install -r requirements.txt
5) Spusťte aplikaci:
   uvicorn app.main:app --host 0.0.0.0 --port 8080
6) V prohlížeči otevřete http://localhost:8080.

6. BĚŽNÝ PROVOZ A KROK ZA KROKEM V APLIKACI
1) Otevřete záložku "Profil, dojezd a CV" (/settings):
   - Zadejte bydliště (např. Bohumín), maximální čas a vzdálenost dojezdu, případně minimální plat.
   - Vložte text svého životopisu BEZ osobních kontaktů (nepište tam telefon, e-mail ani přesnou domácí adresu).
   - Vyberte portály, nastavte čas denního výpisu, případně zapněte e-mailové notifikace a zadejte cílový e-mail a minimální skóre shody.
   - Klikněte na "Uložit nastavení".
   - Pokud máte nastavený e-mail, můžete ověřit odeslání tlačítkem pro testovací výpis.
2) Otevřete záložku "Firmy" (/companies):
   - Přidejte vlastní firmy nebo nechte systém navrhnout firmy v okolí a spravujte jejich aktivitu.
3) Otevřete přehled nabídek (/):
   - Sledujte nové nabídky, jejich shodu s CV a dojezd, označujte si stavy (zajímavá, odesláno apod.) a v případě potřeby spusťte ruční kontrolu.
4) Co dělat, když něco nefunguje:
   - Zkontrolujte logy (při běhu v Dockeru příkazem docker logs --tail 50 job-assistant, při běhu v Pythonu přímo v okně terminálu).
   - Pokud se neukládá nové chování v Dockeru, ověřte, že proběhl rebuild (docker compose up -d --build) a že je správně vyplněný soubor .env.
