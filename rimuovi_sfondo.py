"""Rimozione sfondo via Pixelcut — logica di esempio.py.

Perche falliva su Render:
1) Il click su "gratuito/free" di Pixelcut in headless spesso non apre il download
   (in locale esempio.py usa browser visibile e funziona).
2) Se quel click va in timeout, Playwright in cleanup lanciava anche
   "This event loop is already running" e mascherava l'errore vero.

Qui: stessa sequenza di esempio.py, browser chiuso sempre in finally,
e worker in subprocess reale (non ProcessPool) per isolare Playwright da asyncio.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

# Su Render non c'e display: default true. In locale: PIXELCUT_HEADLESS=false
HEADLESS = os.getenv("PIXELCUT_HEADLESS", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}


def _guess_suffix(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _run_pixelcut(foto_path: str, save_path: str) -> None:
    """Flusso identico a esempio.py main(), da file a file."""
    browser = None
    with sync_playwright() as p:
        try:
            print("Avvio del browser...")
            # headless=False in esempio.py; su Render serve True
            browser = p.chromium.launch(headless=HEADLESS)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            print("Apertura del sito web...")
            page.goto(
                "https://www.pixelcut.ai/it/rimuovi-sfondo/sfondo-bianco",
                timeout=60000,
            )

            # Aspettiamo che la pagina carichi
            page.wait_for_load_state("networkidle")

            print("Caricamento dell'immagine...")
            # Inseriamo il file direttamente nell'input nascosto
            try:
                page.locator('input[type="file"]').first.set_input_files(foto_path)
            except Exception:
                # Se l'input file non viene trovato, proviamo con il file chooser
                with page.expect_file_chooser() as fc_info:
                    page.locator("button, div, a").filter(
                        has_text=re.compile(r"(Carica|Upload)", re.IGNORECASE)
                    ).first.click()
                file_chooser = fc_info.value
                file_chooser.set_files(foto_path)

            print("Immagine caricata. Attesa dell'elaborazione...")

            # Aspettiamo un tempo generoso affinché l'IA finisca di rimuovere lo sfondo
            page.wait_for_timeout(10000)

            # Aspettiamo che appaia il pulsante "Scarica" (Download) e che sia pronto
            download_btn = page.locator("button, a").filter(
                has_text=re.compile(r"(Scarica|Download)", re.IGNORECASE)
            ).first
            download_btn.wait_for(state="visible", timeout=60000)

            print("Pulsante di download trovato. Tentativo di scaricamento...")

            # Clicchiamo sul pulsante Scarica principale e aspettiamo che il popup si apra
            download_btn.click(force=True)

            # Diamo tempo al popup di aprirsi
            print("Attesa apertura pannello di download...")
            page.wait_for_timeout(3000)

            # Cerchiamo il pulsante per il download gratuito / anteprima
            print("Selezione risoluzione gratuita...")

            # Cerchiamo vari testi possibili nel popup
            free_btn = page.locator("text=/anteprima/i").first

            try:
                free_btn.wait_for(state="visible", timeout=5000)
                print("Pulsante con 'anteprima' trovato!")
            except Exception:
                print("Testo 'anteprima' non trovato. Cerco la parola 'gratuito' o 'free'...")
                free_btn = page.locator("text=/gratuito|free/i").first
                try:
                    free_btn.wait_for(state="visible", timeout=5000)
                    print("Pulsante gratuito trovato!")
                except Exception:
                    print(
                        "Nessun testo specifico trovato. Cerco il pulsante 'Scarica' nel popup..."
                    )
                    # Cerchiamo un pulsante Scarica che sia visibile nel popup
                    free_btn = (
                        page.locator("button")
                        .filter(has_text=re.compile(r"(Scarica|Download)", re.IGNORECASE))
                        .locator("visible=true")
                        .last
                    )

            print("Avvio scaricamento...")
            with page.expect_download(timeout=60000) as download_info:
                free_btn.click(force=True, timeout=60000)
            download = download_info.value

            download.save_as(save_path)
            print(f"Successo! Immagine salvata in: {save_path}")
        finally:
            # Chiudi il browser PRIMA di uscire da sync_playwright:
            # evita RuntimeError "event loop is already running" in cleanup.
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


def _remove_background_impl(image_bytes: bytes) -> bytes:
    if not image_bytes:
        raise ValueError("Immagine vuota: impossibile rimuovere lo sfondo")

    suffix = _guess_suffix(image_bytes)
    with tempfile.TemporaryDirectory(prefix="pixelcut_") as tmp_dir:
        foto_path = os.path.join(tmp_dir, f"foto{suffix}")
        save_path = os.path.join(tmp_dir, f"fotosenzasfondo{suffix}")
        with open(foto_path, "wb") as handle:
            handle.write(image_bytes)

        _run_pixelcut(foto_path, save_path)

        with open(save_path, "rb") as handle:
            result = handle.read()
        if not result:
            raise RuntimeError("Download Pixelcut vuoto")
        return result


def remove_background(image_bytes: bytes) -> bytes:
    """Lancia un subprocess Python dedicato (isolamento totale da asyncio)."""
    suffix = _guess_suffix(image_bytes)
    with tempfile.TemporaryDirectory(prefix="pixelcut_job_") as tmp_dir:
        foto_path = os.path.join(tmp_dir, f"foto{suffix}")
        save_path = os.path.join(tmp_dir, f"fotosenzasfondo{suffix}")
        with open(foto_path, "wb") as handle:
            handle.write(image_bytes)

        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker", foto_path, save_path],
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "PIXELCUT_HEADLESS": os.getenv("PIXELCUT_HEADLESS", "true")},
        )
        if proc.stdout:
            print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
        if proc.returncode != 0:
            if proc.stderr:
                print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n")
            detail = (proc.stderr or proc.stdout or "errore sconosciuto").strip()
            raise RuntimeError(f"Rimozione sfondo fallita: {detail[-500:]}")

        with open(save_path, "rb") as handle:
            result = handle.read()
        if not result:
            raise RuntimeError("Download Pixelcut vuoto")
        return result


def find_foto():
    """Trova un file che si chiama 'foto' con qualsiasi estensione nella cartella corrente."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    for file_path in glob.glob(os.path.join(current_dir, "foto.*")):
        if os.path.isfile(file_path):
            return file_path
    return None


def main():
    foto_path = find_foto()
    if not foto_path:
        print("Errore: Nessun file denominato 'foto' trovato nella cartella corrente.")
        return

    print(f"File trovato: {foto_path}")
    original_ext = os.path.splitext(foto_path)[1]
    with open(foto_path, "rb") as handle:
        result = _remove_background_impl(handle.read())
    current_dir = os.path.dirname(os.path.abspath(__file__))
    save_path = os.path.join(current_dir, f"fotosenzasfondo{original_ext}")
    with open(save_path, "wb") as handle:
        handle.write(result)
    print(f"Successo! Immagine salvata in: {save_path}")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        _run_pixelcut(sys.argv[2], sys.argv[3])
    else:
        main()
