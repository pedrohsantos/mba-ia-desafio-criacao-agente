"""Restaura reservas e visitantes ao estado dos arquivos de `dados/`.

Uso:
    uv run python -m aurora.restaurar             # mantém as sessões
    uv run python -m aurora.restaurar --sessoes   # também apaga as sessões
"""

import argparse

from . import db
from .config import SESSOES_DB


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessoes", action="store_true", help="apaga também as sessões")
    args = parser.parse_args()
    db.restaurar(apagar_sessoes=args.sessoes)
    if args.sessoes:
        for sufixo in ("", "-wal", "-shm"):
            caminho = SESSOES_DB.with_name(SESSOES_DB.name + sufixo)
            caminho.unlink(missing_ok=True)
    print("Dados restaurados" + (" e sessões apagadas." if args.sessoes else "."))


if __name__ == "__main__":
    main()
