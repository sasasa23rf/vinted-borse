"""Rimozione sfondo in locale con rembg (stessa API usata dal bot).

Pixelcut via Playwright (esempio.py) funziona in locale con browser visibile,
ma su Render headless i pulsanti di download non compaiono / vengono bloccati.
Per il deploy usiamo rembg: nessuna UI web, risultato PNG in RAM.
"""

from __future__ import annotations

import glob
import os
from functools import lru_cache

from rembg import new_session, remove

# u2netp = modello piu leggero, adatto a Render. Alternative: u2net, isnet-general-use
REMBG_MODEL = os.getenv("REMBG_MODEL", "u2netp").strip() or "u2netp"


@lru_cache(maxsize=1)
def _session():
    return new_session(REMBG_MODEL)


def remove_background(image_bytes: bytes) -> bytes:
    """Riceve i bytes della foto e restituisce PNG senza sfondo (in RAM)."""
    if not image_bytes:
        raise ValueError("Immagine vuota: impossibile rimuovere lo sfondo")
    result = remove(image_bytes, session=_session())
    if not result:
        raise RuntimeError("rembg ha restituito un risultato vuoto")
    return bytes(result)


def find_foto():
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
    with open(foto_path, "rb") as handle:
        result = remove_background(handle.read())
    current_dir = os.path.dirname(os.path.abspath(__file__))
    save_path = os.path.join(current_dir, "fotosenzasfondo.png")
    with open(save_path, "wb") as handle:
        handle.write(result)
    print(f"Successo! Immagine salvata in: {save_path}")


if __name__ == "__main__":
    main()
