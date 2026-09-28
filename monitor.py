"""
Monitor Mercari per annunci "Muse" - con più ricerche indipendenti,
titolo tradotto in italiano e foto dell'annuncio nella notifica.
Pensata per essere lanciata periodicamente da GitHub Actions.

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
from deep_translator import GoogleTranslator
from mercapi import Mercapi
from mercapi.requests import SearchRequestData

# ----------------------------------------------------------------------------
# CONFIGURAZIONE
# ----------------------------------------------------------------------------

SEARCHES = [
    {"label": "Muse CD", "query": "Muse", "categories": [75]},
    {"label": "Muse Cassette", "query": "Muse cassette", "categories": None},
    {"label": "Muse Promo", "query": "Muse promo", "categories": None},
]

PAUSE_BETWEEN_SEARCHES_SECONDS = 3

EXCLUDE_KEYWORD = ""
TITLE_BLACKLIST: list[str] = []

STATE_FILE = Path(__file__).parent / "seen_items.json"
STATUS_FILE = Path(__file__).parent / "last_check.txt"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

translator = GoogleTranslator(source="ja", target="it")


# ----------------------------------------------------------------------------
# PERSISTENZA
# ----------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        raw = STATE_FILE.read_text().strip()
        if raw and raw != "[]":
            data = json.loads(raw)
            if isinstance(data, list):
                return {"seen_ids": data, "initialized_labels": [s["label"] for s in SEARCHES]}
            return data
    return {"seen_ids": [], "initialized_labels": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


# ----------------------------------------------------------------------------
# TRADUZIONE (best-effort: se fallisce, usiamo il titolo originale)
# ----------------------------------------------------------------------------

def translate_title(text: str) -> str | None:
    try:
        translated = translator.translate(text)
        return translated if translated else None
    except Exception as exc:
        print(f"Traduzione fallita per '{text}': {exc}")
        return None


# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------

async def send_telegram_text(client: httpx.AsyncClient, text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    resp = await client.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"Invio Telegram (testo) fallito ({resp.status_code}): {resp.text}")


async def send_telegram_photo(client: httpx.AsyncClient, photo_url: str, caption: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    resp = await client.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"Invio Telegram (foto) fallito ({resp.status_code}): {resp.text} — riprovo come testo")
        await send_telegram_text(client, caption)


async def notify_new_item(client: httpx.AsyncClient, item, label: str) -> None:
    translated = translate_title(item.name)
    price = "prezzo non impostato" if item.is_no_price else f"¥{item.price:,}"
    item_url = f"https://jp.mercari.com/item/{item.id_}"

    title_block = f"🎵 <b>{translated}</b>\n<i>{item.name}</i>" if translated else f"🎵 <b>{item.name}</b>"
    caption = (
        f"{title_block}\n[{label}] {price}\n{item_url}\n\n"
        f"Per comprarlo: incolla questo link nella barra di ricerca di ZenMarket."
    )

    if item.thumbnails:
        await send_telegram_photo(client, item.thumbnails[0], caption)
    else:
        await send_telegram_text(client, caption)


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
                await send_telegram_text(
                    client, f"⚠️ La ricerca '{label}' è fallita in questo giro: {exc}"
                )
                continue

            new_items = [item for item in items if item.id_ not in seen_ids]

            for item in new_items:
                seen_ids.add(item.id_)
                if is_first_run_for_this_search:
                    continue
                print(f"Nuovo annuncio [{label}]: {item.name}")
                await notify_new_item(client, item, label)

            if is_first_run_for_this_search:
                initialized_labels.add(label)
                print(f"Prima esecuzione per '{label}': {len(new_items)} annunci segnati come già visti.")

    save_state({"seen_ids": sorted(seen_ids), "initialized_labels": sorted(initialized_labels)})
    STATUS_FILE.write_text(datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    asyncio.run(main())
