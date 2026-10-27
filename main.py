import os
import re
import html
import json
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

# Configurazione Credenziali
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# File di persistenza
FILE_ARCHIVIO = "terreni_visti.txt"
FILE_ULTIMO_INVIO = "ultimo_invio_terreni.txt"
FILE_STORICO_MD = "storico_aste_proposte.md"

# Parametri operativi e limiti di sicurezza (Anti-Timeout / Anti-Ban / Risparmio Token)
BUDGET_MIN = 500
BUDGET_MAX = 50000
MAX_PAGINE_PER_COMUNE = 4          # Evita centinaia di chiamate HTTP inutili
MAX_SCHEDE_DETTAGLIO_PER_RUN = 25  # Max schede aperte per singolo avvio
MAX_CANDIDATI_AI_PER_RUN = 16      # Max lotti inviati a Gemini per singolo avvio
LIMITE_NOTIFICHE_TELEGRAM = 8      # Max messaggi su Telegram (tutti gli altri vanno sul file .md)
MODELLO_GEMINI = "gemini-2.5-flash"

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
})

# Area Geografica: Provincia di Firenze + Comuni confinanti (Siena, Prato, Pisa)
COMUNI_TARGET = [
    # Città Metropolitana di Firenze
    "firenze", "scandicci", "lastra a signa", "signa", "campi bisenzio", 
    "sesto fiorentino", "fiesole", "impruneta", "bagno a ripoli", 
    "san casciano in val di pesa", "montespertoli", "barberino tavarnelle", 
    "borgo san lorenzo", "calenzano", "capraia e limite", "castelfiorentino", 
    "cerreto guidi", "certaldo", "dicomano", "empoli", "figline e incisa valdarno", 
    "firenzuola", "fucecchio", "gambassi terme", "greve in chianti", "londa", 
    "marradi", "montaione", "montelupo fiorentino", "palazzuolo sul senio", 
    "pelago", "pontassieve", "reggello", "rignano sull'arno", "rufina", 
    "san godenzo", "scarperia e san piero", "vaglia", "vicchio",
    # Confine Siena
    "san gimignano", "poggibonsi", "castellina in chianti", "radda in chianti",
    # Confine Prato
    "prato", "carmignano", "poggio a caiano", "vaiano", "cantagallo",
    # Confine Pisa
    "san miniato", "castelfranco di sotto", "santa maria a monte", "volterra", "palaia"
]

PAROLE_CHIAVE_TERRENO = [
    "terreno", "terreni", "agricol", "suolo", "appezzament", "seminativ", 
    "bosc", "ulivet", "olivet", "vignet", "pascolo", "incolto", "edificabil", 
    "rurale", "coltura", "fondo rustico", "castagneto", "orto"
]

BLACKLIST_CATEGORICA = [
    "appartamento", "villa", "villino", "negozio", "ufficio", "capannone", 
    "fabbricato industriale", "albergo", "hotel", "autorimessa", "garage", 
    "posto auto", "box auto", "quota di 1/", "nuda proprietà", "usufrutto"
]


# ==========================================
# FUNZIONI DI UTILITÀ E GESTIONE STATO
# ==========================================

def safe_html(val, default="N.D."):
    if val is None or str(val).strip().lower() in ["none", "null", ""]:
        return default
    return html.escape(str(val).strip())


def carica_visti():
    if not os.path.exists(FILE_ARCHIVIO):
        return set()
    with open(FILE_ARCHIVIO, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def segna_visti(chiavi):
    if not chiavi:
        return
    visti_attuali = carica_visti()
    nuove = [str(c).strip() for c in chiavi if str(c).strip() and str(c).strip() not in visti_attuali]
    if nuove:
        with open(FILE_ARCHIVIO, "a", encoding="utf-8") as f:
            for c in nuove:
                f.write(f"{c}\n")


def leggi_data_ultimo_invio():
    if os.path.exists(FILE_ULTIMO_INVIO):
        try:
            with open(FILE_ULTIMO_INVIO, "r", encoding="utf-8") as f:
                data_str = f.read().strip()
                if data_str:
                    return datetime.fromisoformat(data_str)
        except Exception:
            pass
    return None


def aggiorna_data_ultimo_invio():
    try:
        with open(FILE_ULTIMO_INVIO, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat())
    except Exception as e:
        print(f"[STATO] Errore salvataggio timestamp: {e}")


def invia_telegram_html(messaggio):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERRORE] Credenziali Telegram mancanti.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": messaggio,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 429:
            retry_after = r.json().get("parameters", {}).get("retry_after", 5)
            print(f"[TELEGRAM FLOOD] Attesa di {retry_after} secondi...")
            time.sleep(retry_after + 1)
            requests.post(url, json=payload, timeout=15)
        elif r.status_code != 200:
            payload.pop("parse_mode", None)
            requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"[TELEGRAM ECCEZIONE]: {e}")
    time.sleep(1.0)


def aggiorna_storico_markdown(nuovi_lotti):
    """Salva ed evidenzia tutte le aste idonee nel documento Markdown su GitHub."""
    header = (
        "# 🌿 Storico e Graduatoria Aste Terreni Proposte\n\n"
        "> Documento aggiornato automaticamente. Le aste sono ordinate per punteggio di qualità e scadenza gara.\n\n"
        "| Scadenza Gara | Score | Comune | Offerta Minima | Superficie | Prezzo/mq | Destinazione | Stato & Gravami | Scheda |\n"
        "| :--- | :---: | :--- | :--- | :--- | :--- | :--- | :--- | :---: |\n"
    )
    
    righe_esistenti = {}
    if os.path.exists(FILE_STORICO_MD):
        try:
            with open(FILE_STORICO_MD, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("|") and "---" not in line and "Scadenza Gara" not in line:
                        m_id = re.search(r"\[(\d+)\]", line)
                        if m_id:
                            righe_esistenti[m_id.group(1)] = line.strip()
        except Exception as e:
            print(f"[STORICO] Avviso lettura file esistente: {e}")

    for l in nuovi_lotti:
        deep = l.get("deep_audit", {})
        score = deep.get("score_totale", 0)
        dest = str(deep.get("destinazione_urbanistica", "Terreno"))[:30].replace("|", "/")
        sintesi_legale = f"{deep.get('stato_occupazione', 'N.D.')} - {deep.get('ipoteche_gravami', 'N.D.')}"[:45].replace("|", "/")
        
        sup_str = f"{l['mq']:,} mq".replace(",", ".") if l.get("mq") else "N.D."
        pmq_str = f"€ {l['prezzo_mq']:.2f}/mq" if l.get("prezzo_mq") else "N.D."
        
        riga = (
            f"| **{l.get('data_asta', 'N.D.')}** "
            f"| **{score}/100** "
            f"| {l['comune']} "
            f"| {l['offerta_minima']} "
            f"| {sup_str} "
            f"| {pmq_str} "
            f"| {dest} "
            f"| {sintesi_legale} "
            f"| [{l['id']}]({l['link']}) |"
        )
        righe_esistenti[str(l["id"])] = riga

    try:
        with open(FILE_STORICO_MD, "w", encoding="utf-8") as f:
            f.write(header)
            for riga in righe_esistenti.values():
                f.write(f"{riga}\n")
        print(f"[STORICO] File {FILE_STORICO_MD} aggiornato con {len(righe_esistenti)} aste totali.")
    except Exception as e:
        print(f"[ERRORE Scrittura Storico MD]: {e}")


# ==========================================
# FASE 1: SCRAPING CATALOGO E PARSING SCHEDA
# ==========================================

def scansiona_catalogo_comune(citta, stats):
    lotti = []
    ids_rilevati = set()
    citta_url = requests.utils.quote(citta.lower())
    base_url = f"https://www.astalegale.net/Immobili?luoghi={citta_url}"

    for pagina in range(1, MAX_PAGINE_PER_COMUNE + 1):
        url = f"{base_url}&page={pagina}" if pagina > 1 else base_url
        try:
            r = session.get(url, timeout=15)
            stats["pagine_http"] += 1
            if r.status_code != 200:
                print(f"[WARN {citta.upper()}] Pagina {pagina} ha restituito HTTP {r.status_code}")
                break

            soup = BeautifulSoup(r.text, "html.parser")
            links_asta = [a for a in soup.find_all("a", href=True) if "/Aste/Detail/" in a["href"]]
            
            # Se nella pagina non ci sono link di aste, il catalogo per questo comune è finito
            if not links_asta:
                break

            nuovi_id_in_pagina = 0
            for a in links_asta:
                href = a["href"]
                parte_dopo = href.split("/Aste/Detail/")[1]
                asta_id = parte_dopo.split("-")[0].strip().split("/")[0].strip()

                if not asta_id.isdigit() or asta_id in ids_rilevati:
                    continue
                ids_rilevati.add(asta_id)
                nuovi_id_in_pagina += 1
                stats["annunci_grezzi_visti"] += 1

                # Estrae sia il titolo dall'URL sia il testo del riquadro HTML intorno al link
                titolo_url = " ".join(parte_dopo.replace("-", " ").split())
                testo_link = " ".join(a.get_text(" ", strip=True).split())
                contesto_card = ""
                if a.parent and a.parent.parent:
                    contesto_card = " ".join(a.parent.parent.get_text(" ", strip=True).split())[:350]

                testo_combinato = f"{titolo_url} {testo_link} {contesto_card}".lower()

                # Verifica se è un terreno oppure se non è un fabbricato in blacklist
                ha_parola_terreno = any(pt in testo_combinato for pt in PAROLE_CHIAVE_TERRENO)
                ha_parola_blacklist = any(b in testo_combinato for b in BLACKLIST_CATEGORICA)

                # Accettiamo il lotto se cita esplicitamente un terreno (e non è un appartamento con giardino),
                # oppure se non ha parole in blacklist ed è sospetto (verrà verificato in Fase 1/2)
                if not ha_parola_terreno:
                    continue
                if ha_parola_blacklist and "terreno" not in titolo_url.lower():
                    continue

                titolo_finale = testo_link if len(testo_link) > 12 else titolo_url
                link_completo = href if href.startswith("http") else f"https://www.astalegale.net{href}"

                lotti.append({
                    "id": asta_id,
                    "comune": citta.title(),  # Corretto: .title() in minuscolo!
                    "titolo": titolo_finale[:140],
                    "link": link_completo
                })

            # Se la pagina non conteneva nessun nuovo ID rispetto alla precedente, ferma la paginazione
            if nuovi_id_in_pagina == 0:
                break

            time.sleep(0.35)
        except Exception as e:
            print(f"[ERRORE Scansione {citta} pag {pagina}]: {e}")
            break

    if lotti:
        print(f"[{citta.upper()}] Individuati {len(lotti)} potenziali terreni.")
    return lotti


def estrai_superficie_terreno(testo):
    """Estrae la superficie in mq gestendo sia i metri quadri che gli ettari (ha)."""
    m_ha = re.search(r"(?:ettari|ha)\s*[:\.]?\s*(\d{1,3}(?:[\.,]\d{1,4})?)", testo, re.IGNORECASE)
    if not m_ha:
        m_ha = re.search(r"(\d{1,3}(?:[\.,]\d{1,4})?)\s*(?:ettari|ha)\b", testo, re.IGNORECASE)
    if m_ha:
        try:
            val_ha = float(m_ha.group(1).replace(",", "."))
            if 0.05 <= val_ha <= 500:
                return int(val_ha * 10000)
        except ValueError:
            pass

    matches_mq = re.findall(r"(\d{1,3}(?:\.\d{3})+|\d{3,6})\s*(?:mq|m²|metri\s*quadr)", testo, re.IGNORECASE)
    matches_mq += re.findall(r"(?:superficie|consistenza|mq|m²)\s*[:\.]?\s*(\d{1,3}(?:\.\d{3})+|\d{3,6})\b", testo, re.IGNORECASE)
    
    valori_mq = []
    for m in matches_mq:
        pulito = m.replace(".", "").replace(" ", "")
        if pulito.isdigit():
            v = int(pulito)
            if 100 <= v <= 2000000:
                valori_mq.append(v)

    if valori_mq:
        return max(valori_mq)
    return None


def e_data_valida_non_passata(data_str):
    try:
        m = re.search(r"(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})", str(data_str))
        if not m:
            return True
        giorno, mese, anno = int(m.group(1)), int(m.group(2)), int(m.group(3))
        data_asta = datetime(anno, mese, giorno)
        return data_asta.date() >= datetime.now().date()
    except Exception:
        return True


def estrai_dati_scheda(lotto):
    dati = {
        "offerta_minima": "N.D.",
        "prezzo_base": "N.D.",
        "prezzo_num": 99999999,
        "data_asta": "Vedi scheda",
        "stato_procedura": "ATTIVA",
        "mq": None,
        "prezzo_mq": None,
        "testo_perizia": ""
    }
    try:
        r = session.get(lotto["link"], timeout=12)
        if r.status_code != 200:
            return dati

        soup = BeautifulSoup(r.text, "html.parser")
        testo_pulito = soup.get_text(separator=" ", strip=True)
        testo_lower = testo_pulito.lower()

        if any(w in testo_lower for w in ["esito: sospesa", "asta sospesa", "vendita sospesa", "procedura sospesa", "revocata"]):
            dati["stato_procedura"] = "SOSPESA"
            return dati

        m_off = re.search(r"Offerta\s+minima[^\d€]{0,25}€?\s*([\d\.]+(?:,\d{2})?)", testo_pulito, re.IGNORECASE)
        m_base = re.search(r"Prezzo\s+base[^\d€]{0,25}€?\s*([\d\.]+(?:,\d{2})?)", testo_pulito, re.IGNORECASE)

        if m_off:
            cifra = m_off.group(1).strip()
            n = cifra.split(",")[0].replace(".", "").replace(" ", "")
            if n.isdigit():
                dati["offerta_minima"] = f"€ {cifra}"
                dati["prezzo_num"] = int(n)

        if m_base:
            cifra_b = m_base.group(1).strip()
            n_b = cifra_b.split(",")[0].replace(".", "").replace(" ", "")
            if n_b.isdigit():
                dati["prezzo_base"] = f"€ {cifra_b}"
                if dati["prezzo_num"] == 99999999:
                    dati["prezzo_num"] = int(n_b)
                    dati["offerta_minima"] = f"€ {cifra_b}"

        m_data = re.search(r"(?:data\s+(?:dell[’'])?asta|data\s+vendita|udienza|termine\s+offerte)[^\d]{0,30}(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})", testo_pulito, re.IGNORECASE)
        if m_data:
            dati["data_asta"] = m_data.group(1).replace("-", "/").replace(".", "/").strip()
        else:
            tutte_date = re.findall(r"(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})", testo_pulito)
            for g, m, a in tutte_date:
                try:
                    dt = datetime(int(a), int(m), int(g))
                    if dt.date() >= datetime.now().date():
                        dati["data_asta"] = f"{g.zfill(2)}/{m.zfill(2)}/{a}"
                        break
                except ValueError:
                    continue

        mq = estrai_superficie_terreno(testo_pulito)
        if mq:
            dati["mq"] = mq
            if 0 < dati["prezzo_num"] < 99999999:
                dati["prezzo_mq"] = round(dati["prezzo_num"] / mq, 2)

        chiavi_terreno = [
            "terreno", "agricol", "edificabil", "boschiv", "ulivet", "seminativ", "vigneto",
            "ettari", "superficie", "destinazione", "urbanistic", "prg", "regolamento",
            "accesso", "strada", "interclus", "servitù", "ipotec", "pignorament", "gravam",
            "occupat", "libero", "affitt", "coltivator", "prelazione", "vincol", "idrogeologic",
            "paesaggistic", "piena proprietà", "quota", "abuso", "manufatto", "annesso"
        ]
        blocchi = []
        for el in soup.find_all(["p", "tr", "td", "li", "dd", "div"]):
            txt = " ".join(el.get_text(" ", strip=True).split())
            if 25 <= len(txt) <= 1500 and any(k in txt.lower() for k in chiavi_terreno):
                if not any(txt in b for b in blocchi):
                    blocchi.append(txt)

        dati["testo_perizia"] = "\n".join(blocchi[:18]) if blocchi else testo_pulito[:4500]

    except Exception as e:
        print(f"[ERRORE Lettura Scheda {lotto['id']}]: {e}")

    return dati


# ==========================================
# FASI 2 E 3: MOTORE AI CON RETRY E IMBUTO
# ==========================================

def chiama_gemini_json(prompt):
    """Wrapper con gestione automatica del Rate Limit (429) e parsing sicuro."""
    if not client:
        print("[AVVISO] Client Gemini non configurato (GEMINI_API_KEY mancante?).")
        return {}
    for tentativo in range(3):
        try:
            response = client.models.generate_content(
                model=MODELLO_GEMINI,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            testo = response.text.strip()
            if testo.startswith("```"):
                testo = re.sub(r"^```(?:json)?", "", testo)
                testo = re.sub(r"```$", "", testo).strip()
            return json.loads(testo)
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                attesa = 15 * (tentativo + 1)
                print(f"[GEMINI RATE LIMIT] Pausa di {attesa}s (tentativo {tentativo + 1}/3)...")
                time.sleep(attesa)
            else:
                print(f"[GEMINI ERRORE]: {e}")
                break
    return {}


def fase2_screening_rapido(batch_lotti):
    """Fase 2 dell'imbuto: scarta rapidamente falsi positivi, quote parziali o fabbricati."""
    payload = [
        {
            "id": l["id"],
            "comune": l["comune"],
            "titolo": l["titolo"],
            "prezzo": l["offerta_minima"],
            "estratto": l["testo_perizia"][:1200]
        }
        for l in batch_lotti
    ]
    prompt = f"""
    Sei un analista di aste immobiliari. Esegui uno screening rapido su questi lotti (Budget max {BUDGET_MAX}€):
    {json.dumps(payload, ensure_ascii=False)}

    REGOLE DI PASSAGGIO ("idoneo": true):
    1. Deve essere principalmente un TERRENO (agricolo, edificabile, uliveto, bosco, seminativo, pascolo, eventuale piccolo annesso rurale).
    2. Scarta ("idoneo": false) SOLO SE il lotto è chiaramente un appartamento, casa di civile abitazione, negozio, garage, oppure se è venduta solo una QUOTA INDIVISA minoritaria (es. 1/2, 1/6) o NUDA PROPRIETÀ.

    Rispondi in JSON:
    {{
      "screening": [
        {{ "id": "ID_LOTTO", "idoneo": true, "motivo_scarto": "" }}
      ]
    }}
    """
    dati = chiama_gemini_json(prompt)
    return {str(item["id"]): item.get("idoneo", True) for item in dati.get("screening", [])}


def fase3_deep_audit_forense(batch_lotti):
    """Fase 3 dell'imbuto: analisi profonda della gara, problematiche, gravami e graduatoria (0-100)."""
    payload = [
        {
            "id": l["id"],
            "comune": l["comune"],
            "titolo": l["titolo"],
            "offerta_minima": l["offerta_minima"],
            "prezzo_base": l["prezzo_base"],
            "mq_rilevati": l["mq"],
            "testo_completo_gara": l["testo_perizia"][:4200]
        }
        for l in batch_lotti
    ]
    prompt = f"""
    Sei un perito agrario e legale forense specializzato in aste giudiziarie in Toscana.
    Analizza in dettaglio queste schede di gara per terreni selezionati:
    {json.dumps(payload, ensure_ascii=False, indent=2)}

    ESEGUI UN DEEP READING PER RILEVARE:
    1. DIRITTO E QUOTE: Verifica se è Piena Proprietà (100%).
    2. STATO OCCUPAZIONE E CONTRATTI: Verifica se è Libero o Occupato (es. contratto d'affitto agrario opponibile, diritto di prelazione agraria).
    3. IPOTECHE, SERVITÙ E GRAVAMI: Segnala trascrizioni non cancellabili, servitù passive (elettrodotti, passaggio), sequestri o cause pendenti. Ricorda che le ipoteche volontarie/giudiziarie standard vengono cancellate dal decreto di trasferimento.
    4. ACCESSIBILITÀ E VINCOLI: Verifica se ha accesso da strada pubblica o se è un fondo intercluso. Segnala vincoli idrogeologici/paesaggistici o abusi edilizi.
    5. SUPERFICIE (MQ): Se 'mq_rilevati' è null, estrai i mq totali dal testo (1 ettaro = 10.000 mq).
    6. GRADUATORIA (SCORE 0-100) E VERDETTO:
       - Assegna un punteggio da 0 a 100 sommando: Convenienza prezzo/mq (max 40 pt) + Pulizia legale/Piena proprietà (max 30 pt) + Qualità terreno/Vicinanza a Firenze-Scandicci/Accessibilità (max 30 pt).
       - Imposta "verdetto": "CONSIGLIATO" per tutti i terreni in Piena Proprietà (anche se hanno vincoli paesaggistici standard o sono boschivi/agricoli semplici, purché il prezzo sia sensato). Usa "SCARTATO" solo per quote indivise, abusi insanabili costosissimi o blocchi legali gravi.

    Rispondi RIGOROSAMENTE in JSON:
    {{
      "analisi": [
        {{
          "id": "ID_LOTTO",
          "mq_stimati": 5000,
          "destinazione_urbanistica": "Agricolo / Uliveto / Edificabile / Boschivo",
          "diritto_reale": "Piena Proprietà 100%",
          "stato_occupazione": "Libero / Affitto agrario / Occupato",
          "ipoteche_gravami": "Ipoteche cancellabili con decreto / Servitù presenti / ...",
          "accessibilita_vincoli": "Accesso carrabile / Fondo intercluso / Vincolo paesaggistico",
          "rischio_legale": "BASSO oppure MEDIO oppure ALTO",
          "score_totale": 75,
          "verdetto": "CONSIGLIATO oppure SCARTATO",
          "relazione_dettagliata": "Analisi tecnica dettagliata su pro, contro, rischi e potenziale del terreno."
        }}
      ]
    }}
    """
    dati = chiama_gemini_json(prompt)
    return {str(item["id"]): item for item in dati.get("analisi", [])}


# ==========================================
# MAIN WORKFLOW A IMBUTO CON DIAGNOSTICA
# ==========================================

if __name__ == "__main__":
    print("=== AVVIO RADAR FORENSE ASTE TERRENI (TOSCANA ESTESA) ===")
    visti = carica_visti()

    stats = {
        "pagine_http": 0,
        "annunci_grezzi_visti": 0,
        "terreni_individuati": 0,
        "nuovi_non_in_memoria": 0,
        "schede_scaricate": 0,
        "in_budget_e_attivi": 0,
        "superato_screening_fase2": 0,
        "approvati_in_graduatoria": 0
    }

    # 1. SCANSIONE CATALOGO
    catalogo_terreni = []
    ids_globali = set()
    for comune in COMUNI_TARGET:
        lotti_c = scansiona_catalogo_comune(comune, stats)
        for l in lotti_c:
            if l["id"] not in ids_globali:
                ids_globali.add(l["id"])
                catalogo_terreni.append(l)

    stats["terreni_individuati"] = len(catalogo_terreni)
    nuovi_lotti = [l for l in catalogo_terreni if l["id"] not in visti]
    stats["nuovi_non_in_memoria"] = len(nuovi_lotti)

    print(f"\n[IMBUTO FASE 1] Pagine lette: {stats['pagine_http']} | Annunci visti: {stats['annunci_grezzi_visti']} | Terreni trovati: {stats['terreni_individuati']} | Nuovi: {stats['nuovi_non_in_memoria']}")

    # Estrazione scheda e filtro budget/data con cap anti-timeout
    candidati_fase2 = []
    lotti_processati_id = []

    for l in nuovi_lotti:
        if stats["schede_scaricate"] >= MAX_SCHEDE_DETTAGLIO_PER_RUN or len(candidati_fase2) >= MAX_CANDIDATI_AI_PER_RUN:
            print(f"[PROTEZIONE RUN] Raggiunto limite di {stats['schede_scaricate']} schede lette ({len(candidati_fase2)} in budget). I restanti verranno analizzati al prossimo avvio.")
            break

        l.update(estrai_dati_scheda(l))
        stats["schede_scaricate"] += 1
        lotti_processati_id.append(l["id"])

        if l["stato_procedura"] == "SOSPESA":
            print(f"  -> Lotto {l['id']} ({l['comune']}): Scartato perché SOSPESA")
            continue
        if not e_data_valida_non_passata(l.get("data_asta", "")):
            print(f"  -> Lotto {l['id']} ({l['comune']}): Scartato per data asta passata ({l.get('data_asta')})")
            continue
        if BUDGET_MIN <= l["prezzo_num"] <= BUDGET_MAX:
            candidati_fase2.append(l)
            print(f"  -> Lotto {l['id']} ({l['comune']}): OK IN BUDGET ({l['offerta_minima']}) - Data: {l['data_asta']}")
        else:
            print(f"  -> Lotto {l['id']} ({l['comune']}): Fuori budget ({l['offerta_minima']})")

        time.sleep(0.4)

    stats["in_budget_e_attivi"] = len(candidati_fase2)
    print(f"[IMBUTO FASE 2] Candidati in budget ({BUDGET_MIN}€ - {BUDGET_MAX}€) inviati allo screening: {len(candidati_fase2)}")

    # 2. SCREENING VELOCE AI (BATCH DA 4)
    candidati_fase3 = []
    for i in range(0, len(candidati_fase2), 4):
        batch = candidati_fase2[i:i + 4]
        esiti = fase2_screening_rapido(batch)
        for l in batch:
            if esiti.get(str(l["id"]), True):
                candidati_fase3.append(l)
            else:
                print(f"  -> Lotto {l['id']}: Scartato da Screening Fase 2 AI")
        time.sleep(1.5)

    stats["superato_screening_fase2"] = len(candidati_fase3)
    print(f"[IMBUTO FASE 3] Terreni qualificati per il Deep Reading forense: {len(candidati_fase3)}")

    # 3. DEEP READING FORENSE E SCORING (BATCH DA 2)
    graduatoria_finale = []
    for i in range(0, len(candidati_fase3), 2):
        batch = candidati_fase3[i:i + 2]
        analisi_profonda = fase3_deep_audit_forense(batch)
        for l in batch:
            deep = analisi_profonda.get(str(l["id"]), {})
            if not deep:
                continue
            l["deep_audit"] = deep
            
            mq_ai = deep.get("mq_stimati")
            if not l.get("mq") and isinstance(mq_ai, (int, float)) and mq_ai > 50:
                l["mq"] = int(mq_ai)
                l["prezzo_mq"] = round(l["prezzo_num"] / l["mq"], 2)

            score_ottenuto = int(deep.get("score_totale", 0))
            verdetto_ai = str(deep.get("verdetto", "")).upper()
            print(f"  -> Deep Audit Lotto {l['id']} ({l['comune']}): Score {score_ottenuto}/100 - Verdetto: {verdetto_ai}")

            if "CONSIGLIATO" in verdetto_ai and score_ottenuto >= 40:
                graduatoria_finale.append(l)
        time.sleep(2.0)

    # 4. ORDINAMENTO PER PUNTEGGIO DECRESCENTE E OUTPUT
    graduatoria_finale.sort(key=lambda x: (-int(x["deep_audit"].get("score_totale", 0)), x["prezzo_num"]))
    stats["approvati_in_graduatoria"] = len(graduatoria_finale)

    print(f"\n=== RIEPILOGO DIAGNOSTICO IMBUTO ===")
    for k, v in stats.items():
        print(f"  • {k}: {v}")

    if graduatoria_finale:
        aggiorna_storico_markdown(graduatoria_finale)

        da_inviare = graduatoria_finale[:LIMITE_NOTIFICHE_TELEGRAM]
        msg_intro = (
            f"🏆 <b>GRADUATORIA ASTE TERRENI (AUDIT FORENSE)</b>\n\n"
            f"Annunci esaminati: <b>{stats['annunci_grezzi_visti']}</b> | Terreni in budget: <b>{stats['in_budget_e_attivi']}</b>\n"
            f"Idonei in graduatoria: <b>{len(graduatoria_finale)}</b> (tutti salvati in <code>{FILE_STORICO_MD}</code>).\n\n"
            f"<i>Ecco le migliori opportunità ordinate per punteggio:</i>"
        )
        invia_telegram_html(msg_intro)

        for pos, l in enumerate(da_inviare, start=1):
            d = l["deep_audit"]
            score = d.get("score_totale", 0)
            rischio = str(d.get("rischio_legale", "MEDIO")).upper()
            badge_rischio = "🟢 BASSO" if "BASSO" in rischio else "🟡 MEDIO" if "MEDIO" in rischio else "🔴 ALTO"
            
            sup_str = f"{l['mq']:,} mq".replace(",", ".") if l.get("mq") else "N.D."
            pmq_str = f"€ {l['prezzo_mq']:.2f}/mq" if l.get("prezzo_mq") else "N.D."

            scheda = (
                f"#{pos} — ⭐ <b>SCORE: {score}/100</b> | 📍 <b>{safe_html(l['comune'].upper())}</b>\n"
                f"🔗 <b>Lotto ID:</b> <a href=\"{safe_html(l['link'])}\">{safe_html(l['id'])} — Apri Scheda Gara</a>\n\n"
                f"🌿 <b>Descrizione:</b> {safe_html(l['titolo'])}\n"
                f"🏞 <b>Destinazione:</b> <code>{safe_html(d.get('destinazione_urbanistica'))}</code>\n"
                f"📐 <b>Superficie:</b> <code>{sup_str}</code> | <b>Prezzo/mq:</b> <code>{pmq_str}</code>\n"
                f"💰 <b>Offerta Minima:</b> <code>{safe_html(l['offerta_minima'])}</code> (Base: {safe_html(l['prezzo_base'])})\n"
                f"⏳ <b>Scadenza / Data Gara:</b> <code>{safe_html(l['data_asta'])}</code>\n\n"
                f"⚖️ <b>AUDIT LEGALE &amp; PROBLEMATICHE:</b>\n"
                f"• <b>Rischio Complessivo:</b> {badge_rischio}\n"
                f"• <b>Diritto:</b> {safe_html(d.get('diritto_reale'))}\n"
                f"• <b>Occupazione:</b> {safe_html(d.get('stato_occupazione'))}\n"
                f"• <b>Ipoteche e Gravami:</b> {safe_html(d.get('ipoteche_gravami'))}\n"
                f"• <b>Accesso e Vincoli:</b> {safe_html(d.get('accessibilita_vincoli'))}\n\n"
                f"🔎 <b>RELAZIONE DETTAGLIATA:</b>\n<i>{safe_html(d.get('relazione_dettagliata'))}</i>"
            )
            invia_telegram_html(scheda)

        if len(graduatoria_finale) > LIMITE_NOTIFICHE_TELEGRAM:
            extra = len(graduatoria_finale) - LIMITE_NOTIFICHE_TELEGRAM
            invia_telegram_html(
                f"📂 <b>Altri {extra} terreni idonei</b> sono stati registrati nella tabella "
                f"<code>{FILE_STORICO_MD}</code> sulla tua repository GitHub con le relative date di fine gara."
            )

        aggiorna_data_ultimo_invio()
    else:
        # AUTO-DIAGNOSTICA ATTIVA QUANDO CI SONO ZERO RISULTATI
        print("[DIAGNOSTICA] Nessun terreno approvato in questo ciclo. Invio report diagnostico.")
        report_diagnostico = (
            "🛠 <b>REPORT DIAGNOSTICO SCANSIONE TERRENI</b>\n\n"
            "Nessun nuovo terreno ha superato l'intero imbuto oggi. Ecco i dati reali della scansione appena conclusa:\n\n"
            f"• <b>Pagine catalogo scaricate:</b> <code>{stats['pagine_http']}</code>\n"
            f"• <b>Annunci immobiliari visti:</b> <code>{stats['annunci_grezzi_visti']}</code>\n"
            f"• <b>Terreni individuati a catalogo:</b> <code>{stats['terreni_individuati']}</code>\n"
            f"• <b>Terreni nuovi (non in archivio):</b> <code>{stats['nuovi_non_in_memoria']}</code>\n"
            f"• <b>Schede di dettaglio aperte:</b> <code>{stats['schede_scaricate']}</code>\n"
            f"• <b>In budget (&le; {BUDGET_MAX:,}€) e attivi:</b> <code>{stats['in_budget_e_attivi']}</code>\n"
            f"• <b>Qualificati dopo Fase 2:</b> <code>{stats['superato_screening_fase2']}</code>\n"
            f"• <b>Approvati in Graduatoria Finale:</b> <code>{stats['approvati_in_graduatoria']}</code>\n\n"
            "<i>Se 'Annunci immobiliari visti' è &gt; 0, la connessione al portale funziona regolarmente.</i>"
        )
        invia_telegram_html(report_diagnostico)
        aggiorna_data_ultimo_invio()

    # Salva nell'archivio solo i lotti effettivamente scaricati e valutati
    segna_visti(lotti_processati_id)
    print("=== MONITORAGGIO CONCLUSO CON SUCCESSO ===")
