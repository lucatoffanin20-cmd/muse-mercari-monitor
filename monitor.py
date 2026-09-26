"""
Monitor Mercari per annunci "Muse" - versione a esecuzione singola con
supporto a più ricerche indipendenti, pensata per GitHub Actions.

Ogni ricerca ha una sua "prima esecuzione silenziosa" (per non ricevere
un'ondata di notifiche quando se ne aggiunge una nuova in futuro): al
primo giro segna tutto come già visto senza notificare, dal giro dopo
notifica solo i nuovi annunci.

Se una ricerca fallisce (es. Mercari temporaneamente irraggiungibile),
viene mandato un avviso su Telegram invece di fallire in silenzio.

Variabili d'ambiente richieste:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mercapi import Mercapi
from mercapi.requests import SearchRequestData

# ----------------------------------------------------------------------------
# CONFIGURAZIONE - una ricerca per riga. "categories" puo' essere None se
# per quel tipo di prodotto Mercari non ha una categoria dedicata (es. le
# musicassette): in quel caso il filtro lo fa solo il testo della query.
# ----------------------------------------------------------------------------

SEARCHES = [
    {
        "label": "Muse CD",
        "query": "Muse",
        "categories": [75],  # "CD" (tutte le sottocategorie: occidentale, giapponese, ecc.)
    },
    {
        "label": "Muse Cassette",
        "query": "Muse cassette",  # niente categoria dedicata su Mercari: filtra il testo
        "categories": None,
    },
    {
        "label": "Muse Promo",
        "query": "Muse promo",
        "categories": None,
    },
]

# pausa tra una ricerca e l'altra nella stessa esecuzione, per non
# bombardare Mercari con più richieste ravvicinate tutte insieme
PAUSE_BETWEEN_SEARCHES_SECONDS = 3

EXCLUDE_KEYWORD = ""  # parole da escludere lato server, valide per tutte le ricerche
TITLE_BLACKLIST: list[str] = []  # es. ["nintendo switch", "profumo"]

STATE_FILE = Path(__file__).parent / "seen_items.json"
STATUS_FILE = Path(__file__).parent / "last_check.txt"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


# ----------------------------------------------------------------------------
# PERSISTENZA (il file viene poi ricommittato nel repo dal workflow)
# ----------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        raw = STATE_FILE.read_text().strip()
        if raw and raw != "[]":
            data = json.loads(raw)
            if isinstance(data, list):  # compatibilita' col vecchio formato
                return {"seen_ids": data, "initialized_labels": [s["label"] for s in SEARCHES]}
            return data
    return {"seen_ids": [], "initialized_labels": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------

async def send_telegram_message(client: httpx.AsyncClient, text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    resp = await client.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"Invio Telegram fallito ({resp.status_code}): {resp.text}")


def format_message(item, label: str) -> str:
    price = "prezzo non impostato" if item.is_no_price else f"¥{item.price:,}"
    item_url = f"https://jp.mercari.com/item/{item.id_}"
    return (
        f"🎵 <b>{item.name}</b>\n[{label}] {price}\n{item_url}\n\n"
        f"Per comprarlo: incolla questo link nella barra di ricerca di ZenMarket."
    )


# ----------------------------------------------------------------------------
# RICERCA
# ----------------------------------------------------------------------------

async def run_search(mercapi: Mercapi, profile: dict):
    kwargs = {}
    if profile.get("categories"):
        kwargs["categories"] = profile["categories"]

    results = await mercapi.search(
        profile["query"],
        status=[SearchRequestData.Status.STATUS_ON_SALE],
        sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
        sort_order=SearchRequestData.SortOrder.ORDER_DESC,
        exclude=EXCLUDE_KEYWORD,
        **kwargs,
    )
    return [
        item for item in results.items
        if not any(w.lower() in item.name.lower() for w in TITLE_BLACKLIST)
    ]


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

async def main():
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    initialized_labels = set(state.get("initialized_labels", []))

    mercapi = Mercapi()

    async with httpx.AsyncClient() as client:
        for i, profile in enumerate(SEARCHES):
            label = profile["label"]
            is_first_run_for_this_search = label not in initialized_labels

            if i > 0:
                await asyncio.sleep(PAUSE_BETWEEN_SEARCHES_SECONDS)

            try:
                items = await run_search(mercapi, profile)
            except Exception as exc:
                print(f"Errore nella ricerca '{label}': {exc}")
                await send_telegram_message(
                    client, f"⚠️ La ricerca '{label}' è fallita in questo giro: {exc}"
                )
                continue

            new_items = [item for item in items if item.id_ not in seen_ids]

            for item in new_items:
                seen_ids.add(item.id_)
                if is_first_run_for_this_search:
                    continue
                print(f"Nuovo annuncio [{label}]: {item.name}")
                await send_telegram_message(client, format_message(item, label))

            if is_first_run_for_this_search:
                initialized_labels.add(label)
                print(f"Prima esecuzione per '{label}': {len(new_items)} annunci segnati come già visti.")

    save_state({"seen_ids": sorted(seen_ids), "initialized_labels": sorted(initialized_labels)})
    STATUS_FILE.write_text(datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    asyncio.run(main())
