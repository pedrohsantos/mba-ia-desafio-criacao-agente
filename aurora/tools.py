"""Tools dos especialistas.

Regra geral: nenhuma tool recebe o apartamento como parâmetro. O apartamento
vem sempre de `_apartamento_da_sessao`, que o lê do vínculo gravado na criação
da sessão (Garantia 2). O modelo escolhe área, data, nome e código; nunca de
quem é a ação.
"""

import re
from datetime import date

from google.adk.tools import ToolContext

from . import db
from .config import DADOS_DIR


class SessaoSemApartamento(Exception):
    pass


def _apartamento_da_sessao(tool_context: ToolContext) -> str:
    session_id = tool_context.session.id
    apartamento = db.apartamento_da_sessao(session_id)
    if not apartamento:
        raise SessaoSemApartamento(session_id)
    return apartamento


def _validar_data(data: str) -> str | None:
    try:
        return date.fromisoformat(data.strip()).isoformat()
    except (ValueError, AttributeError):
        return None


def _resolver_area(area: str) -> dict | None:
    chave = (area or "").strip().lower()
    for a in db.areas():
        if chave in (a["id"], a["nome"].lower()):
            return a
    return None


def _erro(mensagem: str) -> dict:
    return {"status": "erro", "mensagem": mensagem}


# ================================================================ reservas

def listar_areas() -> dict:
    """Lista as áreas comuns que podem ser reservadas, com o id e a taxa.

    Taxa 0 significa área sem cobrança.
    """
    return {
        "areas": [
            {"id": a["id"], "nome": a["nome"], "taxa": a["taxa"], "gera_cobranca": a["taxa"] > 0}
            for a in db.areas()
        ]
    }


def consultar_disponibilidade(area: str, data: str) -> dict:
    """Informa se uma área comum está livre em uma data.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.
    """
    a = _resolver_area(area)
    if not a:
        return _erro("Área desconhecida. Use listar_areas para ver os ids válidos.")
    d = _validar_data(data)
    if not d:
        return _erro("Data inválida. Use o formato AAAA-MM-DD.")
    # Só livre/ocupada: o dono da reserva nunca sai daqui.
    return {"area": a["id"], "data": d, "disponivel": db.data_livre(a["id"], d)}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta conversa."""
    apartamento = _apartamento_da_sessao(tool_context)
    return {"reservas": db.reservas_do_apartamento(apartamento)}


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta conversa.

    Áreas com taxa geram cobrança e ficam pendentes até o morador aprovar pela
    confirmação do sistema. Áreas sem taxa são reservadas na hora.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    a = _resolver_area(area)
    if not a:
        return _erro("Área desconhecida. Use listar_areas para ver os ids válidos.")
    d = _validar_data(data)
    if not d:
        return _erro("Data inválida. Use o formato AAAA-MM-DD.")

    if not db.data_livre(a["id"], d):
        return {"status": "indisponivel", "area": a["id"], "data": d,
                "mensagem": "A área já está reservada nessa data."}

    # Garantia 1: cobrança só com confirmação vinda do sistema. Na primeira
    # execução pedimos a confirmação e não gravamos nada; o ADK pausa e só
    # chama a tool de novo quando a rota /confirmacoes devolver a resposta.
    if a["taxa"] > 0:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            tool_context.request_confirmation(
                hint=f"Reservar {a['nome']} em {d} gera cobrança de R$ {a['taxa']:.2f}.",
                payload={"area": a["id"], "data": d, "taxa": a["taxa"]},
            )
            return {"status": "aguardando_confirmacao", "area": a["id"], "data": d,
                    "mensagem": "Aguardando o morador aprovar a cobrança pelo aplicativo."}
        if not confirmacao.confirmed:
            return {"status": "recusada_pelo_morador", "area": a["id"], "data": d,
                    "mensagem": "O morador não aprovou a cobrança. Nada foi reservado."}

    try:
        reserva = db.criar_reserva(apartamento, a["id"], d)
    except db.DataOcupada:
        # Garantia 5: perdeu a disputa no instante da gravação.
        return {"status": "indisponivel", "area": a["id"], "data": d,
                "mensagem": "A área acabou de ser reservada nessa data. Nada foi cobrado."}
    return {"status": "reservada", **reserva, "taxa": a["taxa"]}


def cancelar_reserva(
    tool_context: ToolContext, codigo: str = "", area: str = "", data: str = ""
) -> dict:
    """Cancela uma reserva do apartamento do morador desta conversa.

    Informe o código da reserva, ou a área e a data.

    Args:
        codigo: código da reserva (opcional se área e data forem informadas).
        area: id da área da reserva.
        data: data da reserva no formato AAAA-MM-DD.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    if codigo:
        cancelada = db.cancelar_reserva(apartamento, codigo=codigo)
    else:
        a = _resolver_area(area)
        d = _validar_data(data)
        if not a or not d:
            return _erro("Informe o código da reserva ou a área e a data (AAAA-MM-DD).")
        cancelada = db.cancelar_reserva(apartamento, area=a["id"], data=d)
    if not cancelada:
        # Mesma resposta exista ou não reserva de outro apartamento: nada vaza.
        return {"status": "nao_encontrada",
                "mensagem": "Não há reserva ativa do seu apartamento com esses dados."}
    return {"status": "cancelada", **cancelada}


# ================================================================ visitantes

def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista os visitantes autorizados do apartamento do morador desta conversa."""
    apartamento = _apartamento_da_sessao(tool_context)
    return {"visitantes": db.visitantes_do_apartamento(apartamento)}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio em uma data.

    Sempre exige aprovação do morador pela confirmação do sistema; o que o
    morador escreve na conversa não conta como confirmação.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    nome = " ".join((nome or "").split())
    if not nome:
        return _erro("Informe o nome do visitante.")
    d = _validar_data(data)
    if not d:
        return _erro("Data inválida. Use o formato AAAA-MM-DD.")

    # Garantia 1: liberar acesso só com confirmação vinda do sistema.
    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Liberar a entrada de {nome} em {d}.",
            payload={"nome": nome, "data": d},
        )
        return {"status": "aguardando_confirmacao", "nome": nome, "data": d,
                "mensagem": "Aguardando o morador aprovar a autorização pelo aplicativo."}
    if not confirmacao.confirmed:
        return {"status": "recusada_pelo_morador", "nome": nome, "data": d,
                "mensagem": "O morador não aprovou. Nenhuma entrada foi liberada."}
    return {"status": "autorizado", **db.autorizar_visitante(apartamento, nome, d)}


# ================================================================ regulamento

_CAPITULO_RE = re.compile(r"^## (Capítulo [IVXLC]+): (.+)$", re.MULTILINE)


def _capitulos() -> list[dict]:
    texto = (DADOS_DIR / "regulamento.md").read_text(encoding="utf-8")
    marcas = list(_CAPITULO_RE.finditer(texto))
    capitulos = []
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        capitulos.append({
            "numero": i + 1,
            "capitulo": m.group(1),
            "assunto": m.group(2).strip(),
            "texto": texto[m.start():fim].strip(),
        })
    return capitulos


def listar_capitulos_regulamento() -> dict:
    """Lista os capítulos do regulamento interno (número e assunto, sem o texto)."""
    return {"capitulos": [
        {"numero": c["numero"], "capitulo": c["capitulo"], "assunto": c["assunto"]}
        for c in _capitulos()
    ]}


def ler_capitulo_regulamento(numero: int) -> dict:
    """Devolve o texto de UM capítulo do regulamento interno.

    Args:
        numero: número do capítulo, como em listar_capitulos_regulamento.
    """
    for c in _capitulos():
        if c["numero"] == int(numero):
            return {"capitulo": c["capitulo"], "assunto": c["assunto"], "texto": c["texto"]}
    return _erro("Capítulo inexistente. Use listar_capitulos_regulamento.")
