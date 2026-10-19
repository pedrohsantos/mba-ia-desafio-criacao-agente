"""Caminhos e configurações compartilhadas."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

DADOS_DIR = RAIZ / "dados"
VAR_DIR = Path(os.getenv("AURORA_VAR_DIR", RAIZ / "var"))
VAR_DIR.mkdir(parents=True, exist_ok=True)

# Dados do condomínio (reservas, visitantes, sessões -> apartamento).
CONDOMINIO_DB = VAR_DIR / "condominio.db"
# Sessões e eventos do ADK.
SESSOES_DB = VAR_DIR / "sessoes.db"

APP_NAME = "residencial_aurora"

MODELO_PRINCIPAL = os.getenv("AURORA_MODELO_PRINCIPAL") or "gemini-3.8-flash"
MODELO_ESPECIALISTAS = os.getenv("AURORA_MODELO_ESPECIALISTAS") or "gemini-3.8-flash"
