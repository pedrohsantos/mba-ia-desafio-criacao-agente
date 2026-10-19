"""API HTTP do assistente do Residencial Aurora."""

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.adk.apps import App
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types
from pydantic import BaseModel

from . import db
from .agent import root_agent
from .config import APP_NAME, SESSOES_DB

logger = logging.getLogger("aurora")

CONFIRMACAO = "adk_request_confirmation"


class NovaSessao(BaseModel):
    apartamento: str


class Mensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


estado: dict = {}
# Uma execução por vez em cada sessão; sessões diferentes rodam em paralelo.
travas: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.inicializar()
    # Garantia 3: sessões e eventos persistidos em SQLite (var/sessoes.db).
    session_service = SqliteSessionService(str(SESSOES_DB))
    app_adk = App(name=APP_NAME, root_agent=root_agent)
    estado["sessions"] = session_service
    estado["runner"] = Runner(app=app_adk, session_service=session_service)
    yield
    await estado["runner"].close()


app = FastAPI(title="Residencial Aurora", lifespan=lifespan)


def _user_id(apartamento: str) -> str:
    return f"apto-{apartamento}"


async def _carregar_sessao(session_id: str) -> tuple[str, Session]:
    apartamento = db.apartamento_da_sessao(session_id)
    sessao = None
    if apartamento:
        sessao = await estado["sessions"].get_session(
            app_name=APP_NAME, user_id=_user_id(apartamento), session_id=session_id
        )
    if sessao is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return apartamento, sessao


def confirmacoes_pendentes(sessao: Session) -> list[dict]:
    """Pedidos de confirmação do ADK ainda sem resposta nesta sessão."""
    respondidas = {
        fr.id for ev in sessao.events for fr in ev.get_function_responses() if fr.id
    }
    pendentes = []
    for ev in sessao.events:
        for fc in ev.get_function_calls():
            if fc.name != CONFIRMACAO or not fc.id or fc.id in respondidas:
                continue
            args = fc.args or {}
            original = args.get("originalFunctionCall") or {}
            confirmacao = args.get("toolConfirmation") or {}
            pendentes.append({
                "id": fc.id,
                "acao": original.get("name", ""),
                "detalhes": {
                    **(original.get("args") or {}),
                    **(confirmacao.get("payload") or {}),
                },
                "descricao": confirmacao.get("hint", ""),
            })
    return pendentes


def _texto_final(eventos: list[Event]) -> str:
    textos = []
    for ev in eventos:
        if ev.author == "user" or ev.partial or not ev.content or not ev.content.parts:
            continue
        if not ev.is_final_response():
            continue
        for parte in ev.content.parts:
            if parte.text and not parte.thought:
                textos.append(parte.text.strip())
    return "\n".join(t for t in textos if t)


async def _executar(apartamento: str, session_id: str, mensagem: types.Content) -> dict:
    eventos = []
    async for ev in estado["runner"].run_async(
        user_id=_user_id(apartamento), session_id=session_id, new_message=mensagem
    ):
        eventos.append(ev)
    _, sessao = await _carregar_sessao(session_id)
    return {
        "resposta": _texto_final(eventos),
        "confirmacoes_pendentes": confirmacoes_pendentes(sessao),
    }


@app.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao):
    apartamento = corpo.apartamento.strip()
    # Garantia 2: o apartamento é fixado aqui, uma única vez.
    sessao = await estado["sessions"].create_session(
        app_name=APP_NAME, user_id=_user_id(apartamento),
        state={"apartamento": apartamento},
    )
    db.registrar_sessao(sessao.id, apartamento)
    return {"session_id": sessao.id}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: Mensagem):
    apartamento, _ = await _carregar_sessao(session_id)
    async with travas[session_id]:
        mensagem = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(apartamento, session_id, mensagem)


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao):
    apartamento, _ = await _carregar_sessao(session_id)
    async with travas[session_id]:
        # Garantia 1: só aceita um id que esteja pendente NESTA sessão. Lido de
        # novo dentro da trava, então um id já respondido recebe 409.
        _, sessao = await _carregar_sessao(session_id)
        if corpo.id not in {p["id"] for p in confirmacoes_pendentes(sessao)}:
            return JSONResponse(
                status_code=409,
                content={"detail": "Não existe confirmação pendente com esse id nesta sessão."},
            )
        # A resposta volta ao ADK como FunctionResponse do adk_request_confirmation;
        # o Runner encontra o agente que fez o pedido e reexecuta a tool original.
        resposta = types.Content(role="user", parts=[types.Part(
            function_response=types.FunctionResponse(
                id=corpo.id, name=CONFIRMACAO, response={"confirmed": corpo.confirmado},
            )
        )])
        return await _executar(apartamento, session_id, resposta)


@app.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str):
    _, sessao = await _carregar_sessao(session_id)
    return [ev.model_dump(mode="json", exclude_none=True, by_alias=True) for ev in sessao.events]


@app.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str):
    return db.reservas_do_apartamento(numero)


@app.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str):
    return db.visitantes_do_apartamento(numero)
