import asyncio
import gc
import html
import json
import logging
import os
import random
import re
from collections import OrderedDict
from collections.abc import Collection
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

BRANDS = {
    "michael_kors": {
        "name": "Michael Kors",
        "url": "https://www.vinted.it/catalog?catalog[]=19&brand_ids[]=6005&page=1&order=newest_first&price_to={price}&currency=EUR"
    },
    "guess": {
        "name": "Guess",
        "url": "https://www.vinted.it/catalog?catalog[]=19&brand_ids[]=20&page=1&order=newest_first&price_to={price}&currency=EUR"
    },
    "liu_jo": {
        "name": "Liu Jo",
        "url": "https://www.vinted.it/catalog?catalog[]=19&brand_ids[]=2165&page=1&order=newest_first&price_to={price}&currency=EUR"
    },
    "armani": {
        "name": "Armani",
        "url": "https://www.vinted.it/catalog?catalog[]=19&brand_ids[]=5930015&page=1&order=newest_first&price_to={price}&currency=EUR"
    },
    "calvin_klein": {
        "name": "Calvin Klein",
        "url": "https://www.vinted.it/catalog?catalog[]=19&brand_ids[]=255&page=1&order=newest_first&price_to={price}&currency=EUR"
    },
    "trussardi": {
        "name": "Trussardi",
        "url": "https://www.vinted.it/catalog?catalog[]=19&brand_ids[]=8729&page=1&order=newest_first&price_to={price}&currency=EUR"
    }
}
VINTED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}
POLL_SECONDS = max(5, int(os.getenv("VINTED_POLL_SECONDS", "15")))
INITIAL_BATCH_LIMIT = 5
PRICE_CHANGE_BATCH_LIMIT = 10
MAX_SEEN_ITEMS = 500
DEFAULT_MAX_PRICE = os.getenv("DEFAULT_MAX_PRICE", "10").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SUPABASE_URL = os.getenv(
    "SUPABASE_URL", "https://rcmnyppytatzlnewoosn.supabase.co"
).strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_IMAGE_BUCKET = "product-images"
MAX_PRODUCT_IMAGE_SIZE = 10 * 1024 * 1024
PRODUCTS_PAGE_SIZE = 8
ALLOWED_PRODUCT_IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vinted-monitor")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def normalize_price(value: str) -> str | None:
    try:
        price = Decimal(value.strip().replace(",", "."))
    except InvalidOperation:
        return None
    if not price.is_finite() or price <= 0:
        return None
    return format(price.normalize(), "f")


PRICE_STATE_FILE = ROOT_DIR / "price_state.json"

def load_price_state() -> dict[str, str]:
    if PRICE_STATE_FILE.exists():
        try:
            with open(PRICE_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Errore caricamento price_state: %s", e)
    return {"value": DEFAULT_MAX_PRICE}

def save_price_state(state: dict[str, str]) -> None:
    try:
        with open(PRICE_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.error("Errore salvataggio price_state: %s", e)


def set_max_price(price_state: dict[str, str], price: str) -> None:
    if price_state["value"] != price:
        price_state["value"] = price
        price_state["batch_price"] = price
        save_price_state(price_state)


def catalog_url(max_price: str, brand_key: str = "michael_kors") -> str:
    url_template = BRANDS.get(brand_key, BRANDS["michael_kors"])["url"]
    parts = urlsplit(url_template.format(price="0"))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["price_to"] = max_price
    query["time"] = str(int(time.time()))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, safe="[]"), "")
    )


def item_id_from_url(url: str) -> str | None:
    match = re.search(r"/items/(\d+)", urlparse(url).path)
    return match.group(1) if match else None


def canonical_item_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def remember_item(seen_items: OrderedDict[str, None], item_id: str) -> None:
    seen_items[item_id] = None
    seen_items.move_to_end(item_id)
    if len(seen_items) > MAX_SEEN_ITEMS:
        seen_items.popitem(last=False)


class CatalogLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href and "/items/" in href:
            self.urls.append(urljoin("https://www.vinted.it", html.unescape(href)))


async def listing_item_urls(client: httpx.AsyncClient, max_price: str, brand_key: str = "michael_kors") -> list[str]:
    response = await client.get(catalog_url(max_price, brand_key), timeout=30)
    try:
        response.raise_for_status()
        page_text = response.text
    finally:
        await response.aclose()
    parser = CatalogLinkParser()
    parser.feed(page_text)
    del page_text
    unique_urls: list[str] = []
    found_ids: set[str] = set()
    for url in parser.urls:
        item_id = item_id_from_url(url)
        if item_id and item_id not in found_ids:
            found_ids.add(item_id)
            unique_urls.append(url)
    return unique_urls


def select_new_item_urls(
    item_urls: list[str],
    seen_items: Collection[str],
    limit: int | None = None,
) -> tuple[list[str], str | None]:
    selected_urls: list[str] = []
    for item_url in item_urls:
        item_id = item_id_from_url(item_url)
        if item_id is None:
            continue
        if item_id in seen_items:
            return selected_urls, item_id
        selected_urls.append(item_url)
        if limit is not None and len(selected_urls) >= limit:
            break
    return selected_urls, None


async def read_item(client: httpx.AsyncClient, item_url: str) -> dict[str, str]:
    response = await client.get(item_url, timeout=30)
    try:
        response.raise_for_status()
        page_text = response.text
        item_final_url = str(response.url)
    finally:
        await response.aclose()
    match = re.search(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        page_text,
        re.DOTALL | re.IGNORECASE,
    )
    del page_text
    if not match:
        raise RuntimeError("Dati prodotto JSON-LD assenti nella pagina Vinted")
    product = json.loads(match.group(1))
    offers = product.get("offers", {})
    raw_price = normalize_price(str(offers.get("price", "")))
    if not raw_price:
        raise RuntimeError("Prezzo non trovato nei dati prodotto Vinted")
    price = f"{Decimal(raw_price):.2f}".replace(".", ",") + " €"
    image_url = product.get("image")
    if isinstance(image_url, list):
        image_url = image_url[0] if image_url else ""
    if not image_url:
        raise RuntimeError("URL immagine assente nei dati prodotto Vinted")
    return {
        "title": html.unescape(str(product.get("name", "Inserzione"))).strip(),
        "price": price,
        "description": html.unescape(str(product.get("description", "Descrizione non presente"))).strip(),
        "image_url": str(image_url),
        "item_url": canonical_item_url(str(offers.get("url") or item_final_url)),
    }


async def telegram_api_call(
    client: httpx.AsyncClient,
    method: str,
    data: dict[str, str],
    files: dict | None = None,
) -> dict:
    try:
        response = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
            data=data,
            files=files,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise RuntimeError(
            f"Richiesta Telegram {method} fallita ({type(error).__name__})"
        ) from None
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram ha rifiutato {method}: {result.get('description', 'errore sconosciuto')}")
    return result


async def telegram_send_text(
    client: httpx.AsyncClient, chat_id: str, text: str, reply_markup: dict | None = None
) -> dict:
    data: dict[str, str] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    return await telegram_api_call(client, "sendMessage", data)


def supabase_headers() -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "Configura SUPABASE_SERVICE_ROLE_KEY (e SUPABASE_URL, se diverso) nell'ambiente"
        )
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }


async def supabase_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: list[tuple[str, str]] | None = None,
    json_body: dict | None = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict | list | None:
    request_headers = supabase_headers()
    if headers:
        request_headers.update(headers)
    try:
        response = await client.request(
            method,
            f"{SUPABASE_URL}{path}",
            params=params,
            json=json_body,
            content=content,
            headers=request_headers,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise RuntimeError(
            f"Supabase ha rifiutato la richiesta (HTTP {error.response.status_code})"
        ) from None
    except httpx.HTTPError as error:
        raise RuntimeError(
            f"Connessione a Supabase non riuscita ({type(error).__name__})"
        ) from None
    if not response.content:
        return None
    return response.json()


async def upload_telegram_image(
    client: httpx.AsyncClient, message: dict, chat_id: str
) -> str:
    photos = message.get("photo") or []
    document = message.get("document") or {}
    if photos:
        attachment = max(photos, key=lambda photo: int(photo.get("file_size", 0)))
        file_id = str(attachment["file_id"])
        content_type = "image/jpeg"
    elif document:
        content_type = str(document.get("mime_type", "")).lower()
        file_id = str(document.get("file_id", ""))
    else:
        raise RuntimeError("Invia una foto o un file immagine supportato.")

    extension = ALLOWED_PRODUCT_IMAGE_TYPES.get(content_type)
    if not extension or not file_id:
        raise RuntimeError("Formato immagine non supportato. Usa JPG, PNG, WEBP, GIF o AVIF.")

    file_result = await telegram_api_call(client, "getFile", {"file_id": file_id})
    file_path = str(file_result.get("result", {}).get("file_path", ""))
    if not file_path:
        raise RuntimeError("Telegram non ha reso disponibile il file.")
    try:
        response = await client.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise RuntimeError(
            f"Download immagine da Telegram non riuscito ({type(error).__name__})"
        ) from None
    if len(response.content) > MAX_PRODUCT_IMAGE_SIZE:
        raise RuntimeError("L'immagine supera il limite di 10 MB.")

    storage_path = f"telegram/{chat_id}/{uuid.uuid4()}.{extension}"
    await supabase_request(
        client,
        "POST",
        f"/storage/v1/object/{SUPABASE_IMAGE_BUCKET}/{storage_path}",
        content=response.content,
        headers={"Content-Type": content_type, "x-upsert": "false"},
    )
    return storage_path


async def delete_product_images(client: httpx.AsyncClient, paths: list[str]) -> None:
    if paths:
        await supabase_request(
            client,
            "DELETE",
            f"/storage/v1/object/{SUPABASE_IMAGE_BUCKET}",
            json_body={"prefixes": paths},
        )


async def show_products_page(
    client: httpx.AsyncClient, chat_id: str, offset: int
) -> None:
    offset = max(0, offset)
    products = await supabase_request(
        client,
        "GET",
        "/rest/v1/products",
        params=[
            ("select", "id,name"),
            ("order", "created_at.desc"),
            ("limit", str(PRODUCTS_PAGE_SIZE + 1)),
            ("offset", str(offset)),
        ],
    )
    products = products if isinstance(products, list) else []
    has_next = len(products) > PRODUCTS_PAGE_SIZE
    products = products[:PRODUCTS_PAGE_SIZE]
    if not products and offset:
        await show_products_page(client, chat_id, max(0, offset - PRODUCTS_PAGE_SIZE))
        return
    if not products:
        await telegram_send_text(client, chat_id, "Non ci sono prodotti da gestire.")
        return

    keyboard = [
        [{
            "text": str(product.get("name", "Prodotto"))[:60],
            "callback_data": f"product:select:{product['id']}:{offset}",
        }]
        for product in products
    ]
    navigation = []
    if offset:
        navigation.append({"text": "← Precedenti", "callback_data": f"products:page:{max(0, offset - PRODUCTS_PAGE_SIZE)}"})
    if has_next:
        navigation.append({"text": "Successivi →", "callback_data": f"products:page:{offset + PRODUCTS_PAGE_SIZE}"})
    if navigation:
        keyboard.append(navigation)
    await telegram_send_text(
        client,
        chat_id,
        f"Prodotti {offset + 1}-{offset + len(products)}. Seleziona un prodotto per eliminarlo:",
        {"inline_keyboard": keyboard},
    )


async def ask_delete_product(
    client: httpx.AsyncClient, chat_id: str, product_id: str, offset: int
) -> None:
    product_id = str(uuid.UUID(product_id))
    result = await supabase_request(
        client,
        "GET",
        "/rest/v1/products",
        params=[("select", "name"), ("id", f"eq.{product_id}"), ("limit", "1")],
    )
    if not isinstance(result, list) or not result:
        await telegram_send_text(client, chat_id, "Prodotto non trovato; aggiorno la lista.")
        await show_products_page(client, chat_id, offset)
        return
    name = str(result[0].get("name", "Prodotto"))
    await telegram_send_text(
        client,
        chat_id,
        f"Confermi l'eliminazione di «{name}»?",
        {"inline_keyboard": [[
            {"text": "Elimina", "callback_data": f"product:delete:{product_id}:{offset}"},
            {"text": "Annulla", "callback_data": f"products:page:{offset}"},
        ]]},
    )


async def remove_product(
    client: httpx.AsyncClient, chat_id: str, product_id: str, offset: int
) -> None:
    product_id = str(uuid.UUID(product_id))
    result = await supabase_request(
        client,
        "GET",
        "/rest/v1/products",
        params=[("select", "images"), ("id", f"eq.{product_id}"), ("limit", "1")],
    )
    if not isinstance(result, list) or not result:
        await telegram_send_text(client, chat_id, "Il prodotto è già stato eliminato.")
        await show_products_page(client, chat_id, offset)
        return
    await supabase_request(
        client,
        "DELETE",
        "/rest/v1/products",
        params=[("id", f"eq.{product_id}")],
    )
    cleanup_failed = False
    try:
        await delete_product_images(client, result[0].get("images") or [])
    except Exception:
        cleanup_failed = True
        logger.exception("Prodotto eliminato, ma non è stato possibile rimuovere le immagini")
    message = "Prodotto eliminato."
    if cleanup_failed:
        message += " Alcune immagini potrebbero essere ancora nello Storage."
    await telegram_send_text(client, chat_id, message)
    await show_products_page(client, chat_id, offset)


def price_menu() -> dict:
    values = ("5", "10", "15", "20", "30", "50")
    buttons = [
        {"text": f"{value} €", "callback_data": f"max:{value}"}
        for value in values
    ]
    rows = [buttons[index : index + 3] for index in range(0, len(buttons), 3)]
    rows.append([{"text": "Altro importo", "callback_data": "max:custom"}])
    return {"inline_keyboard": rows}

def brand_menu() -> dict:
    buttons = [
        {"text": brand["name"], "callback_data": f"brand:{key}"}
        for key, brand in BRANDS.items()
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return {"inline_keyboard": rows}


async def process_telegram_update(
    client: httpx.AsyncClient,
    update: dict,
    price_state: dict[str, str],
    awaiting_custom_price: set[str],
    bot_state: dict,
) -> None:
    callback = update.get("callback_query")
    if callback:
        callback_message = callback.get("message", {})
        chat_id = str(callback_message.get("chat", {}).get("id", ""))
        if chat_id != TELEGRAM_CHAT_ID:
            return
        await telegram_api_call(
            client,
            "answerCallbackQuery",
            {"callback_query_id": str(callback["id"])},
        )
        callback_data = str(callback.get("data", ""))
        if callback_data == "upload:done":
            session = bot_state.get("product_sessions", {}).get(chat_id)
            if session and session.get("step") == "images":
                if not session["images"]:
                    await telegram_send_text(client, chat_id, "Invia almeno un'immagine prima di continuare.")
                else:
                    session["step"] = "description"
                    await telegram_send_text(client, chat_id, "Ora scrivi la descrizione del prodotto.")
        elif callback_data.startswith("products:page:"):
            try:
                await show_products_page(client, chat_id, int(callback_data.rsplit(":", 1)[1]))
            except Exception as error:
                await telegram_send_text(client, chat_id, f"Non riesco a caricare i prodotti: {error}")
        elif callback_data.startswith("product:select:"):
            try:
                _, _, product_id, offset = callback_data.split(":", 3)
                await ask_delete_product(client, chat_id, product_id, int(offset))
            except (ValueError, IndexError):
                await telegram_send_text(client, chat_id, "Selezione prodotto non valida.")
            except Exception as error:
                await telegram_send_text(client, chat_id, f"Non riesco a leggere il prodotto: {error}")
        elif callback_data.startswith("product:delete:"):
            try:
                _, _, product_id, offset = callback_data.split(":", 3)
                await remove_product(client, chat_id, product_id, int(offset))
            except (ValueError, IndexError):
                await telegram_send_text(client, chat_id, "Selezione prodotto non valida.")
            except Exception as error:
                await telegram_send_text(client, chat_id, f"Non riesco a eliminare il prodotto: {error}")
        elif callback_data == "max:custom":
            awaiting_custom_price.add(chat_id)
            await telegram_send_text(
                client, chat_id, "Scrivi il nuovo prezzo massimo in euro (es. 17,50)."
            )
        elif callback_data.startswith("max:"):
            price = normalize_price(callback_data.removeprefix("max:"))
            if price:
                set_max_price(price_state, price)
                await telegram_send_text(
                    client, chat_id, f"Prezzo massimo impostato a {price} €."
                )
        elif callback_data.startswith("brand:"):
            brand_key = callback_data.removeprefix("brand:")
            if brand_key in BRANDS:
                bot_state["brand"] = brand_key
                bot_state["force_batch_limit"] = 6
                await telegram_send_text(
                    client, chat_id, f"Brand impostato su {BRANDS[brand_key]['name']}."
                )
        return

    message = update.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = str(message.get("text", "")).strip()
    if chat_id != TELEGRAM_CHAT_ID:
        return

    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
    product_sessions = bot_state.setdefault("product_sessions", {})

    if command == "/carica":
        if not SUPABASE_SERVICE_ROLE_KEY:
            await telegram_send_text(
                client,
                chat_id,
                "Per caricare prodotti configura SUPABASE_SERVICE_ROLE_KEY nell'ambiente del bot.",
            )
        elif chat_id in product_sessions:
            await telegram_send_text(client, chat_id, "Hai già un caricamento in corso. Usa /annulla per interromperlo.")
        else:
            awaiting_custom_price.discard(chat_id)
            product_sessions[chat_id] = {"step": "name", "images": []}
            await telegram_send_text(client, chat_id, "Qual è il nome del prodotto?")
        return
    if command == "/prodotti":
        try:
            await show_products_page(client, chat_id, 0)
        except Exception as error:
            await telegram_send_text(client, chat_id, f"Non riesco a caricare i prodotti: {error}")
        return
    if command in {"/annulla", "/cancel"}:
        session = product_sessions.pop(chat_id, None)
        if session and session.get("images"):
            try:
                await delete_product_images(client, session["images"])
            except Exception as error:
                await telegram_send_text(
                    client, chat_id, f"Caricamento annullato; non sono riuscito a rimuovere tutte le immagini: {error}"
                )
                return
        await telegram_send_text(client, chat_id, "Caricamento annullato." if session else "Non c'è un caricamento in corso.")
        return

    session = product_sessions.get(chat_id)
    if session and session.get("step") == "images" and (message.get("photo") or message.get("document")):
        try:
            storage_path = await upload_telegram_image(client, message, chat_id)
            session["images"].append(storage_path)
            await telegram_send_text(
                client,
                chat_id,
                f"Immagine {len(session['images'])} caricata. Invia altre foto oppure premi Fine immagini.",
                {"inline_keyboard": [[{"text": "Fine immagini", "callback_data": "upload:done"}]]},
            )
        except Exception as error:
            await telegram_send_text(client, chat_id, f"Impossibile caricare l'immagine: {error}")
        return

    if not text:
        return

    if chat_id in awaiting_custom_price and not text.startswith("/"):
        price = normalize_price(text)
        if price:
            set_max_price(price_state, price)
            awaiting_custom_price.discard(chat_id)
            await telegram_send_text(
                client, chat_id, f"Prezzo massimo impostato a {price} €."
            )
        else:
            await telegram_send_text(
                client,
                chat_id,
                "Importo non valido. Inserisci un numero maggiore di zero, ad esempio 17,50.",
            )
        return

    if command == "/prezzo":
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            price = normalize_price(parts[1])
            if price:
                set_max_price(price_state, price)
                await telegram_send_text(
                    client, chat_id, f"Prezzo massimo impostato a {price} €."
                )
            else:
                await telegram_send_text(
                    client, chat_id, "Importo non valido. Usa, ad esempio, /prezzo 17,50."
                )
            return
        await telegram_send_text(
            client,
            chat_id,
            f"Prezzo massimo attuale: {price_state['value']} €. Scegli un importo:",
            price_menu(),
        )
        return
    if command == "/start":
        bot_state["active"] = True
        await telegram_send_text(
            client,
            chat_id,
            "Monitor Vinted avviato. Comandi: /prezzo, /brand, /carica, /prodotti, /annulla, /stop.",
        )
        return
    if command == "/stop":
        bot_state["active"] = False
        await telegram_send_text(
            client, chat_id, "Monitor Vinted fermato. Usa /start per riavviarlo."
        )
        return
    if command == "/brand":
        current_brand = str(bot_state.get("brand", "michael_kors"))
        await telegram_send_text(
            client,
            chat_id,
            f"Brand attuale: {BRANDS[current_brand]['name']}. Scegli un brand:",
            brand_menu(),
        )
        return

    session = product_sessions.get(chat_id)
    if session:
        step = session.get("step")
        if step == "name":
            if text.startswith("/"):
                await telegram_send_text(client, chat_id, "Scrivi il nome del prodotto oppure usa /annulla.")
            else:
                session["name"] = text[:200]
                session["step"] = "images"
                await telegram_send_text(
                    client,
                    chat_id,
                    "Invia una o più immagini del prodotto. Quando hai finito premi Fine immagini.",
                    {"inline_keyboard": [[{"text": "Fine immagini", "callback_data": "upload:done"}]]},
                )
            return
        if step == "images":
            if text.lower() in {"fine", "fine immagini", "fatto"}:
                if session["images"]:
                    session["step"] = "description"
                    await telegram_send_text(client, chat_id, "Ora scrivi la descrizione del prodotto.")
                else:
                    await telegram_send_text(client, chat_id, "Invia almeno un'immagine prima di continuare.")
            else:
                await telegram_send_text(client, chat_id, "Invia un'immagine oppure premi Fine immagini.")
            return
        if step == "description":
            if text.startswith("/"):
                await telegram_send_text(client, chat_id, "Scrivi la descrizione oppure usa /annulla.")
            else:
                session["description"] = text
                session["step"] = "price"
                await telegram_send_text(client, chat_id, "Qual è il prezzo del prodotto in euro? (es. 25,50)")
            return
        if step == "price":
            price = normalize_price(text)
            if not price:
                await telegram_send_text(client, chat_id, "Prezzo non valido. Inserisci un importo maggiore di zero, ad esempio 25,50.")
                return
            try:
                await supabase_request(
                    client,
                    "POST",
                    "/rest/v1/products",
                    json_body={
                        "name": session["name"],
                        "description": session["description"],
                        "price": price,
                        "images": session["images"],
                        "is_visible": True,
                    },
                    headers={"Prefer": "return=minimal"},
                )
            except Exception as error:
                await telegram_send_text(client, chat_id, f"Non sono riuscito a salvare il prodotto: {error}. Puoi riprovare il prezzo o usare /annulla.")
                return
            product_sessions.pop(chat_id, None)
            await telegram_send_text(client, chat_id, f"Prodotto «{session['name']}» caricato correttamente nella vetrina.")
            return

async def telegram_command_listener(
    client: httpx.AsyncClient, price_state: dict[str, str], bot_state: dict
) -> None:
    offset: int | None = None
    awaiting_custom_price: set[str] = set()
    while True:
        try:
            params: dict[str, str | int] = {"timeout": 20}
            if offset is not None:
                params["offset"] = offset
            response = await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params=params,
                timeout=25,
            )
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError("Telegram getUpdates ha restituito un errore")
            for update in result.get("result", []):
                offset = int(update["update_id"]) + 1
                await process_telegram_update(
                    client, update, price_state, awaiting_custom_price, bot_state
                )
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 409:
                logger.error(
                    "Telegram getUpdates HTTP 409: un'altra istanza sta gia usando il polling. "
                    "Arresta il bot locale e lascia attivo un solo Background Worker."
                )
                await asyncio.sleep(30)
            elif status == 429:
                try:
                    retry_after = int(error.response.json().get("parameters", {}).get("retry_after", 30))
                except (ValueError, TypeError, json.JSONDecodeError):
                    retry_after = 30
                logger.warning("Telegram rate limit HTTP 429; riprovo tra %d secondi", retry_after)
                await asyncio.sleep(max(1, retry_after))
            else:
                logger.warning(
                    "Telegram getUpdates HTTP %d; controlla token e configurazione chat",
                    status,
                )
                await asyncio.sleep(30 if status in {401, 403} else 5)
        except Exception as error:
            logger.warning(
                "Polling Telegram fallito (%s); nuovo tentativo tra 5 secondi",
                type(error).__name__,
            )
            await asyncio.sleep(5)


async def send_telegram_item(
    client: httpx.AsyncClient, item: dict[str, str]
) -> None:
    heading = f"{item['title']}\nPrezzo: {item['price']}"
    description = item["description"]
    caption_limit = 1000
    link_line = f"Link: {item['item_url']}"
    caption_overhead = len(heading) + len(link_line) + 10
    description_length = max(0, caption_limit - caption_overhead)
    caption = f"{heading}\n\n{description[:description_length]}\n\n{link_line}".strip()
    remaining_description = description[description_length:]

    await telegram_api_call(
        client,
        "sendPhoto",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "photo": item["image_url"],
        },
    )

    if remaining_description:
        chunks = [
            remaining_description[index : index + 3500]
            for index in range(0, len(remaining_description), 3500)
        ]
        for index, chunk in enumerate(chunks):
            text = chunk
            if index == len(chunks) - 1:
                text = f"{chunk}\n\n{link_line}"
            data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
            await telegram_api_call(client, "sendMessage", data)


SEEN_ITEMS_FILE = ROOT_DIR / "seen_items.json"

def load_seen_items() -> OrderedDict[str, None]:
    seen: OrderedDict[str, None] = OrderedDict()
    if SEEN_ITEMS_FILE.exists():
        try:
            with open(SEEN_ITEMS_FILE, "r") as f:
                data = json.load(f)
                for item_id in data:
                    seen[item_id] = None
        except Exception as e:
            logger.error("Errore caricamento cache: %s", e)
    return seen

def save_seen_items(seen: OrderedDict[str, None]) -> None:
    try:
        with open(SEEN_ITEMS_FILE, "w") as f:
            json.dump(list(seen.keys()), f)
    except Exception as e:
        logger.error("Errore salvataggio cache: %s", e)


async def monitor() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Configura TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID nell'ambiente prima dell'avvio"
        )

    seen_items: OrderedDict[str, None] = load_seen_items()
    price_state = load_price_state()
    bot_state = {"active": True}
    initial_batch = not seen_items
    pool_limits = httpx.Limits(
        max_connections=5,
        max_keepalive_connections=2,
    )
    async with (
        httpx.AsyncClient(
            timeout=30,
            headers=VINTED_HEADERS,
            follow_redirects=True,
            limits=pool_limits,
        ) as vinted_client,
        httpx.AsyncClient(timeout=30, limits=pool_limits) as telegram_client,
    ):
        telegram_task = asyncio.create_task(
            telegram_command_listener(telegram_client, price_state, bot_state)
        )
        try:
            while True:
                if not bot_state["active"]:
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                cycle_price = price_state["value"]
                cycle_batch_price = price_state.get("batch_price")
                cycle_succeeded = False
                try:
                    current_brand = str(bot_state.get('brand', 'michael_kors'))
                    item_urls = await listing_item_urls(vinted_client, cycle_price, current_brand)
                    if not item_urls:
                        raise RuntimeError("Nessuna inserzione trovata nel catalogo")

                    logger.info(
                        "Inserzioni trovate in elenco: %d (prezzo massimo: %s EUR)",
                        len(item_urls),
                        cycle_price,
                    )
                    if bot_state.get("force_batch_limit"):
                        limit = bot_state.pop("force_batch_limit")
                    elif initial_batch:
                        limit = INITIAL_BATCH_LIMIT
                    elif cycle_batch_price == cycle_price:
                        limit = PRICE_CHANGE_BATCH_LIMIT
                    else:
                        limit = None
                    urls_to_process, existing_item_id = select_new_item_urls(
                        item_urls, seen_items, limit
                    )

                    for item_url in urls_to_process:
                        item_id = item_id_from_url(item_url)
                        if item_id is None:
                            continue

                        item = await read_item(vinted_client, item_url)
                        await send_telegram_item(telegram_client, item)
                        remember_item(seen_items, item_id)
                        save_seen_items(seen_items)
                        logger.info(
                            "Trovata e inviata su Telegram: %s (%s)",
                            item["title"],
                            item["price"],
                        )
                        del item  # libera subito titolo, descrizione, immagine ecc.

                    del item_urls, urls_to_process  # libera le liste di URL

                    if existing_item_id:
                        logger.info(
                            "Raggiunta inserzione gia vista: ID %s", existing_item_id
                        )
                    elif initial_batch and len(urls_to_process) >= INITIAL_BATCH_LIMIT:
                        logger.info(
                            "Primo avvio: limite di %d inserzioni raggiunto",
                            INITIAL_BATCH_LIMIT,
                        )
                    else:
                        logger.info("Fine inserzioni nuove nell'elenco")
                    if limit == PRICE_CHANGE_BATCH_LIMIT:
                        logger.info(
                            "Scansione dopo cambio prezzo: massimo %d nuove inserzioni",
                            PRICE_CHANGE_BATCH_LIMIT,
                        )
                    cycle_succeeded = True

                except Exception as e:
                    logger.exception("Errore durante il controllo (%s); riprovo al prossimo ciclo", type(e).__name__)
                finally:
                    initial_batch = False
                    if (
                        cycle_succeeded
                        and cycle_batch_price == cycle_price
                        and price_state.get("batch_price") == cycle_price
                    ):
                        price_state.pop("batch_price", None)
                        save_price_state(price_state)

                gc.collect()
                wait = random.randint(5, 20)
                logger.info(
                    "Prossimo aggiornamento del catalogo tra %d secondi", wait
                )
                await asyncio.sleep(wait)
        finally:
            telegram_task.cancel()
            try:
                await telegram_task
            except asyncio.CancelledError:
                pass


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Il bot e' online!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()

    def log_message(self, format, *args):
        # Silenzio i log del web server per non inquinare l'output
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), HealthHandler)
    # daemon=False: il web server resta vivo anche se il monitor crasha
    thread = threading.Thread(target=server.serve_forever, daemon=False)
    thread.start()
    logger.info("Health server avviato sulla porta %d", port)


if __name__ == "__main__":
    start_health_server()
    while True:
        try:
            logger.info("Avvio del monitor Vinted...")
            asyncio.run(monitor())
        except KeyboardInterrupt:
            logger.info("Monitor interrotto dall'utente")
            break
        except Exception:
            logger.exception(
                "Il monitor e' crashato. Riavvio automatico tra 10 secondi..."
            )
            time.sleep(10)
