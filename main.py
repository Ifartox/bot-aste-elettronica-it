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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

FILE_ARCHIVIO = "it_attrezzature_viste.txt"
FILE_ULTIMO_INVIO = "ultimo_invio_it.txt"

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
})

# Blacklist tassativa: zero veicoli, zero ingombri logistici pesanti
BLACKLIST_CATEGORICA = [
    # Veicoli e ruote
    "autovettura", "autocarro", "fiat", "iveco", "volkswagen", "audi", "bmw", "mercedes",
    "furgone", "scooter", "motociclo", "moto", "piaggio", "targa", "telaio", "rimorchio",
    "trattore", "semirimorchio", "autoveicolo",
    # Macchinari pesanti e ingombri industriali
    "scaffalatur", "tornio", "pressa", "fresatrice", "ponteggio", "gru", "container",
    "caldaia", "silo", "cisterna", "fusto", "tessitura", "filatoio", "rame", "rottam",
    "scrivania", "tavolo riunioni", "armadio", "sedia", "poltrona"
]


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
    with open(FILE_ARCHIVIO, "a", encoding="utf-8") as f:
        for ch in chiavi:
            c = str(ch).replace("\n", "").replace("\r", "").strip()
            if c:
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
        print(f"[STATO] Errore timestamp: {e}")


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
        if r.status_code != 200:
            payload.pop("parse_mode", None)
            requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"[TELEGRAM ECCEZIONE]: {e}")
    time.sleep(0.5)


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


def scansiona_catalogo_mobili(citta="firenze", max_pagine=3):
    lotti = []
    ids_rilevati = set()
    base_url = f"https://www.astalegale.net/Mobili?luoghi={citta.lower()}"
    print(f"\n[CATALOGO {citta.upper()}] Connessione a: {base_url}")

    for pagina in range(1, max_pagine + 1):
        url = f"{base_url}&page={pagina}" if pagina > 1 else base_url
        try:
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            trovati = 0

            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/Aste/Detail/" in href:
                    parte_dopo = href.split("/Aste/Detail/")[1]
                    asta_id = parte_dopo.split("-")[0].strip()

                    if asta_id in ids_rilevati:
                        continue
                    ids_rilevati.add(asta_id)

                    titolo_pulito = parte_dopo.replace("-", " ")
                    titolo_str = " ".join(titolo_pulito.split())[:140]
                    titolo_lower = titolo_str.lower()

                    if any(b in titolo_lower for b in BLACKLIST_CATEGORICA):
                        continue

                    link_completo = href if href.startswith("http") else f"https://www.astalegale.net{href}"

                    lotti.append({
                        "id": asta_id,
                        "comune": citta.capitalize(),
                        "titolo": titolo_str,
                        "link": link_completo
                    })
                    trovati += 1

            print(f"[{citta.upper()}] Pagina {pagina}: individuati {trovati} lotti non veicolari.")
            if trovati == 0:
                break
            time.sleep(0.8)
        except Exception as e:
            print(f"[ERRORE Scansione {citta} pag {pagina}]: {e}")
            break

    return lotti


def estrai_dati_scheda(lotto):
    dati = {
        "offerta_minima": "N.D.",
        "prezzo_num": 99999999,
        "data_asta": "Vedi scheda",
        "stato_procedura": "ATTIVA",
        "testo_perizia": ""
    }
    try:
        r = session.get(lotto["link"], timeout=12)
        if r.status_code != 200:
            return dati

        soup = BeautifulSoup(r.text, "html.parser")
        testo_pulito = soup.get_text(separator=" ", strip=True)
        testo_lower = testo_pulito.lower()

        if any(w in testo_lower for w in ["sospesa", "sospeso", "revocata"]):
            dati["stato_procedura"] = "SOSPESA"

        m_off = re.search(r"Offerta\s+minima[^\d€]{0,25}€?\s*([\d\.]+(?:,\d{2})?)", testo_pulito, re.IGNORECASE)
        if not m_off:
            m_off = re.search(r"Prezzo\s+base[^\d€]{0,25}€?\s*([\d\.]+(?:,\d{2})?)", testo_pulito, re.IGNORECASE)

        if m_off:
            cifra = m_off.group(1).strip()
            dati["offerta_minima"] = f"€ {cifra}"
            n = cifra.split(",")[0].replace(".", "").replace(" ", "")
            if n.isdigit():
                dati["prezzo_num"] = int(n)

        m_data = re.search(r"(?:data\s+vendita|data\s+asta|termine)[^\d]{0,25}(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})", testo_pulito, re.IGNORECASE)
        if m_data:
            dati["data_asta"] = m_data.group(1).replace("-", "/").replace(".", "/").strip()

        blocchi = []
        chiavi_rilevanti = [
            "pc", "computer", "notebook", "laptop", "macbook", "apple", "server", "monitor",
            "stampante", "plotter", "fotocamera", "attrezzatur", "trapano", "avvitatore", "hilti",
            "bosch", "dewalt", "makita", "elettroutensil", "saldatrice", "laser", "caffè", "strumento",
            "descrizione", "bene", "lotto", "marca", "modello", "quantità"
        ]
        for el in soup.find_all(["p", "tr", "td", "li", "dd"]):
            txt = " ".join(el.get_text(" ", strip=True).split())
            if 15 <= len(txt) <= 800 and any(k in txt.lower() for k in chiavi_rilevanti):
                if not any(txt in b for b in blocchi):
                    blocchi.append(txt)

        dati["testo_perizia"] = "\n".join(blocchi[:10]) if blocchi else testo_pulito[:2500]

    except Exception as e:
        print(f"[ERRORE Lettura Bene {lotto['id']}]: {e}")

    return dati


def audit_beni_batch(batch_lotti):
    if not client or not batch_lotti:
        return {}

    payload_ai = [
        {
            "id": l["id"],
            "comune": l["comune"],
            "titolo": l["titolo"],
            "offerta_minima": l["offerta_minima"],
            "descrizione": l["testo_perizia"][:2500]
        }
        for l in batch_lotti
    ]

    prompt = f"""
    Sei un perito commerciale esperto in aste giudiziarie di beni mobili, IT e attrezzature professionali.
    Valuta questo gruppo di lotti per un acquirente privato con BUDGET MASSIMO DI 1.000 € e NESSUN MAGAZZINO DI STOCCAGGIO:
    {json.dumps(payload_ai, ensure_ascii=False, indent=2)}

    CRITERI DI SELEZIONE RIGIDI:
    1. ZERO VEICOLI: Se un lotto si rivela essere un'auto, moto, scooter, autocarro o rimorchio, assegna 'SCARTATO'.
    2. STOCCAGGIO COMPATTO (FONDAMENTALE): Ammetti solo 'PORTATILE' (lotti di computer, laptop, monitor, tablet, fotocamere, piccoli elettroutensili da valigetta come trapani o saldatrici portatili, macchine caffè banco). Scarta come 'INGOMBRANTE' tutto ciò che richiede furgoni, scaffali industriali o stoccaggio ingombrante.
    3. VALUTAZIONE ECONOMICA: Stima il valore realistico di realizzo usato (es. su Subito o eBay). Calcola il profitto netto togliendo l'offerta minima e un 15% di oneri d'asta.
    4. VERDETTO: 'ACQUISTO CONSIGLIATO' solo se il lotto è PORTATILE, richiesto sul mercato e conveniente. Altrimenti 'SCARTATO'.

    Rispondi RIGOROSAMENTE in JSON conforme a questo schema:
    {{
      "risultati": [
        {{
          "id": "ID_LOTTO",
          "categoria": "IT / Elettronica / Attrezzatura / Altro",
          "trasportabilita": "PORTATILE oppure INGOMBRANTE",
          "valore_usato_stimato": 450,
          "profitto_stimato": 180,
          "rivendibilita": "ALTA oppure MEDIA",
          "verdetto": "ACQUISTO CONSIGLIATO oppure SCARTATO",
          "giudizio": "Sintetica spiegazione commerciale del lotto"
        }}
      ]
    }}
    """

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        testo_resp = response.text.strip()
        if testo_resp.startswith("```"):
            testo_resp = re.sub(r"^```(?:json)?", "", testo_resp)
            testo_resp = re.sub(r"```$", "", testo_resp).strip()

        dati = json.loads(testo_resp)
        return {item["id"]: item for item in dati.get("risultati", [])}
    except Exception as e:
        print(f"[AI BATCH BENI ERRORE]: {e}")
        return {}


if __name__ == "__main__":
    print("=== AVVIO RADAR IT & ATTREZZATURE (BUDGET MAX 1.000€) ===")

    FILTRA_GIA_VISTE = True
    visti = carica_visti()

    lotti_fi = scansiona_catalogo_mobili("firenze", max_pagine=3)
    lotti_po = scansiona_catalogo_mobili("prato", max_pagine=3)
    totali = lotti_fi + lotti_po

    print(f"Lotti non veicolari individuati a catalogo: {len(totali)}")

    lotti_arricchiti = []
    for i, lotto in enumerate(totali, start=1):
        lotto.update(estrai_dati_scheda(lotto))
        ch_data = str(lotto.get("data_asta", "ND")).strip().replace(" ", "_")
        lotto["chiave_tracciamento"] = f"IT_{lotto['id']}_{ch_data}_{lotto['stato_procedura']}"
        lotti_arricchiti.append(lotto)
        time.sleep(0.6)

    # Filtro budget rigido: tra 30 € e 1.000 €
    candidati = []
    for l in lotti_arricchiti:
        if FILTRA_GIA_VISTE and l["chiave_tracciamento"] in visti:
            continue
        if l["stato_procedura"] == "SOSPESA":
            continue
        if 30 <= l["prezzo_num"] <= 1000 and e_data_valida_non_passata(l.get("data_asta", "")):
            candidati.append(l)

    print(f"Candidati all'audit forense/commerciale AI (<= 1.000€): {len(candidati)}")

    # Esecuzione audit con Gemini
    dizionario_audit = {}
    dimensione_batch = 4
    for i in range(0, len(candidati), dimensione_batch):
        batch = candidati[i:i + dimensione_batch]
        if batch:
            dizionario_audit.update(audit_beni_batch(batch))
            time.sleep(1.8)

    # Filtraggio: solo acquisti consigliati e portatili
    selezionati = []
    for l in candidati:
        aud = dizionario_audit.get(l["id"], {})
        l["audit"] = aud
        if aud.get("verdetto") == "ACQUISTO CONSIGLIATO" and aud.get("trasportabilita") == "PORTATILE":
            selezionati.append(l)

    selezionati = sorted(selezionati, key=lambda x: x["audit"].get("profitto_stimato", 0), reverse=True)[:3]

    if not selezionati:
        print("Nessun affare compatto entro i 1.000€ trovato oggi.")
        data_ultimo = leggi_data_ultimo_invio()
        adesso = datetime.now()

        if data_ultimo is None:
            msg_primo_avvio = (
                "🤖 <b>RADAR IT &amp; ATTREZZATURE AVVIATO</b>\n\n"
                "Il bot è collegato con successo e operativo.\n"
                "<i>Nessun lotto compatto idoneo (budget &le; 1.000 €) rilevato oggi a catalogo tra Firenze e Prato.</i>\n\n"
                "La scansione quotidiana è attiva: riceverai notifiche non appena verranno pubblicati beni conformi."
            )
            invia_telegram_html(msg_primo_avvio)
            aggiorna_data_ultimo_invio()
        else:
            giorni = (adesso - data_ultimo).days
            if giorni >= 7:
                msg_hb = (
                    "ℹ️ <b>RADAR IT &amp; ATTREZZATURE (BUDGET &le; 1.000 €)</b>\n\n"
                    "Il bot è regolarmente <b>attivo e operativo</b>.\n"
                    "<i>Nessun lotto compatto di elettronica o attrezzatura professionale rilevato negli ultimi 7 giorni tra Firenze e Prato.</i>\n\n"
                    "La scansione quotidiana prosegue in background."
                )
                invia_telegram_html(msg_hb)
                aggiorna_data_ultimo_invio()
    else:
        intro = (
            f"💻 <b>AFFARI IT &amp; ATTREZZATURE (BUDGET &le; 1.000 €)</b>\n\n"
            f"Individuati <b>{len(selezionati)}</b> lotti compatti a stoccaggio zero:"
        )
        invia_telegram_html(intro)

        for l in selezionati:
            aud = l["audit"]
            scheda = (
                f"🏷 <b>{safe_html(l['comune'].upper())}</b> — <a href=\"{safe_html(l['link'])}\">{safe_html(l['id'])}</a>\n\n"
                f"📦 <b>Bene:</b> {safe_html(l['titolo'])}\n"
                f"📂 <b>Categoria:</b> <code>{safe_html(aud.get('categoria', 'Generica'))}</code>\n"
                f"💰 <b>Offerta Minima:</b> <code>{safe_html(l['offerta_minima'])}</code> <i>(Budget rispettato)</i>\n"
                f"📈 <b>Valore Usato Stimato:</b> <code>~€ {aud.get('valore_usato_stimato', 'N.D.'):,}</code>\n"
                f"💵 <b>Margine Netto Potenziale:</b> <code>+€ {aud.get('profitto_stimato', 'N.D.'):,}</code>\n"
                f"🚗 <b>Trasporto:</b> <code>{safe_html(aud.get('trasportabilita'))}</code> | <b>Liquidità bene:</b> <code>{safe_html(aud.get('rivendibilita'))}</code>\n"
                f"📅 <b>Data Asta:</b> <code>{safe_html(l['data_asta'])}</code>\n\n"
                f"💡 <b>ANALISI ESPERTO:</b>\n<i>{safe_html(aud.get('giudizio'))}</i>"
            )
            invia_telegram_html(scheda)

        aggiorna_data_ultimo_invio()

    segna_visti([l["chiave_tracciamento"] for l in lotti_arricchiti if l.get("chiave_tracciamento")])
    print("=== MONITORAGGIO CONCLUSO ===")
