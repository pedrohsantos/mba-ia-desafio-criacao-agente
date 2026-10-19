"""Armazenamento dos dados do condomínio em SQLite.

Os arquivos de `dados/` são só o estado inicial: eles são lidos, nunca
escritos. Reservas, visitantes e o vínculo sessão -> apartamento ficam em
`var/condominio.db`.
"""

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import CONDOMINIO_DB, DADOS_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservas (
    codigo      TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    area        TEXT NOT NULL,
    data        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'cancelada')),
    criada_em   TEXT NOT NULL
);

-- Garantia 5: no máximo uma reserva ATIVA por área e data. A restrição é
-- verificada pelo próprio SQLite no instante do INSERT, então duas gravações
-- simultâneas nunca produzem duas reservas ativas para a mesma área e data.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa_por_area_data
    ON reservas (area, data) WHERE status = 'ativa';

-- Todo código já usado, inclusive de reservas canceladas ou apagadas por uma
-- restauração. Esta tabela nunca é limpa, então um código nunca se repete.
CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL,
    criada_em   TEXT NOT NULL
);

-- Garantia 2: o apartamento de cada sessão é gravado uma única vez, na criação.
CREATE TABLE IF NOT EXISTS sessoes (
    session_id  TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    criada_em   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
"""


class DataOcupada(Exception):
    """A área já tem reserva ativa na data pedida."""


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def conexao():
    con = sqlite3.connect(CONDOMINIO_DB, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    try:
        yield con
    finally:
        con.close()


@contextmanager
def transacao():
    """Transação de escrita: BEGIN IMMEDIATE já reserva o lock de escrita."""
    with conexao() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")


def inicializar() -> None:
    """Cria o schema e, só na primeira vez, carrega o estado inicial."""
    with conexao() as con:
        con.executescript(SCHEMA)
        carregado = con.execute(
            "SELECT 1 FROM meta WHERE chave = 'carregado'"
        ).fetchone()
    if not carregado:
        restaurar()


def _ler_json(nome: str):
    with open(DADOS_DIR / nome, encoding="utf-8") as f:
        return json.load(f)


def restaurar(apagar_sessoes: bool = False) -> None:
    """Volta reservas e visitantes ao estado dos arquivos de `dados/`."""
    with conexao() as con:
        con.executescript(SCHEMA)
    reservas = _ler_json("reservas.json")
    visitantes = _ler_json("visitantes.json")
    with transacao() as con:
        con.execute("DELETE FROM reservas")
        con.execute("DELETE FROM visitantes")
        for r in reservas:
            con.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, criada_em)"
                " VALUES (?, ?, ?, ?, ?)",
                (r["codigo"], r["apartamento"], r["area"], r["data"], _agora()),
            )
            con.execute(
                "INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (?)",
                (r["codigo"],),
            )
        for v in visitantes:
            con.execute(
                "INSERT INTO visitantes (apartamento, nome, data, criada_em)"
                " VALUES (?, ?, ?, ?)",
                (v["apartamento"], v["nome"], v["data"], _agora()),
            )
        if apagar_sessoes:
            con.execute("DELETE FROM sessoes")
        con.execute(
            "INSERT OR REPLACE INTO meta (chave, valor) VALUES ('carregado', ?)",
            (_agora(),),
        )


# ---------------------------------------------------------------- catálogo

def apartamentos() -> list[dict]:
    return _ler_json("apartamentos.json")


def areas() -> list[dict]:
    return _ler_json("areas.json")


def area_por_id(area_id: str) -> dict | None:
    return next((a for a in areas() if a["id"] == area_id), None)


# ---------------------------------------------------------------- sessões

def registrar_sessao(session_id: str, apartamento: str) -> None:
    with transacao() as con:
        con.execute(
            "INSERT INTO sessoes (session_id, apartamento, criada_em) VALUES (?, ?, ?)",
            (session_id, apartamento, _agora()),
        )


def apartamento_da_sessao(session_id: str) -> str | None:
    with conexao() as con:
        row = con.execute(
            "SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row["apartamento"] if row else None


# ---------------------------------------------------------------- reservas

def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        rows = con.execute(
            "SELECT codigo, area, data FROM reservas"
            " WHERE apartamento = ? AND status = 'ativa' ORDER BY data, codigo",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def data_livre(area: str, data: str) -> bool:
    """Só diz se a data está livre; nunca expõe de quem é a reserva."""
    with conexao() as con:
        row = con.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area, data),
        ).fetchone()
    return row is None


def _novo_codigo(con: sqlite3.Connection) -> str:
    while True:
        codigo = f"RSV-{secrets.token_hex(4).upper()}"
        try:
            con.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
            return codigo
        except sqlite3.IntegrityError:
            continue


def criar_reserva(apartamento: str, area: str, data: str) -> dict:
    """Grava a reserva. Levanta DataOcupada se a área já está reservada.

    A exclusividade não depende de uma consulta anterior: quem decide é o
    índice único parcial `ux_reserva_ativa_por_area_data`, no momento do INSERT.
    """
    try:
        with transacao() as con:
            codigo = _novo_codigo(con)
            con.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, criada_em)"
                " VALUES (?, ?, ?, ?, ?)",
                (codigo, apartamento, area, data, _agora()),
            )
    except sqlite3.IntegrityError as e:
        raise DataOcupada(f"{area} {data}") from e
    return {"codigo": codigo, "area": area, "data": data}


def cancelar_reserva(
    apartamento: str, codigo: str = "", area: str = "", data: str = ""
) -> dict | None:
    """Cancela uma reserva ativa DO PRÓPRIO apartamento. None se não houver."""
    with transacao() as con:
        if codigo:
            row = con.execute(
                "SELECT codigo, area, data FROM reservas WHERE codigo = ?"
                " AND apartamento = ? AND status = 'ativa'",
                (codigo.strip().upper(), apartamento),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT codigo, area, data FROM reservas WHERE area = ? AND data = ?"
                " AND apartamento = ? AND status = 'ativa'",
                (area, data, apartamento),
            ).fetchone()
        if row is None:
            return None
        con.execute(
            "UPDATE reservas SET status = 'cancelada' WHERE codigo = ?", (row["codigo"],)
        )
    return dict(row)


# ---------------------------------------------------------------- visitantes

def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with conexao() as con:
        rows = con.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def autorizar_visitante(apartamento: str, nome: str, data: str) -> dict:
    with transacao() as con:
        con.execute(
            "INSERT INTO visitantes (apartamento, nome, data, criada_em) VALUES (?, ?, ?, ?)",
            (apartamento, nome, data, _agora()),
        )
    return {"nome": nome, "data": data}
