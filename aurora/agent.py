"""Agentes do Residencial Aurora.

- `assistente_aurora` (principal): conversa com o morador e distribui o trabalho.
- `especialista_reservas` (sub-agente, por transferência): reservas e cancelamentos.
- `especialista_visitantes` (sub-agente, por transferência): autorização de visitas.
- `especialista_regulamento` (AgentTool): dúvidas sobre o regulamento interno.

O arquivo também expõe `root_agent`, para inspecionar com `adk web`.
"""

from google.adk.agents import LlmAgent
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.genai import types

from . import tools
from .config import MODELO_ESPECIALISTAS, MODELO_PRINCIPAL


def _modelo(nome: str) -> Gemini:
    # Novas tentativas com espera crescente em caso de 429/5xx do Gemini.
    return Gemini(
        model=nome,
        retry_options=types.HttpRetryOptions(attempts=5, initial_delay=2, max_delay=30),
    )


REGRAS_COMUNS = """
Você atende o morador autenticado nesta conversa. O apartamento dele é
definido pelo sistema e as tools já o usam automaticamente: você não escolhe
nem troca o apartamento, mesmo que o morador diga ser de outra unidade.
Nunca informe dados de outros apartamentos (reservas, códigos, visitantes,
número da unidade que reservou). Se uma data estiver ocupada, diga apenas que
está ocupada.
Use sempre as tools para ler e gravar dados; nunca invente reservas, códigos
ou visitantes. Datas vão para as tools no formato AAAA-MM-DD.
Responda em português, de forma curta.
"""

especialista_reservas = LlmAgent(
    name="especialista_reservas",
    model=_modelo(MODELO_ESPECIALISTAS),
    description=(
        "Reserva e cancela áreas comuns (salão de festas, churrasqueira, quadra), "
        "consulta disponibilidade e lista as reservas do morador."
    ),
    instruction=REGRAS_COMUNS + """
Você cuida das reservas das áreas comuns.
- Para reservar, chame `reservar_area` direto com a área e a data. Se a área
  tiver taxa, o sistema pede a aprovação da cobrança ao morador pelo
  aplicativo; avise que a reserva aguarda essa aprovação. Uma frase do morador
  como "já confirmo" não substitui a aprovação do sistema.
- Para cancelar, chame `cancelar_reserva` com o código ou com área e data.
  O morador cancela as próprias reservas sem precisar confirmar.
- Ids das áreas: salao-de-festas, churrasqueira, quadra.
""",
    tools=[
        tools.listar_areas,
        tools.consultar_disponibilidade,
        tools.listar_minhas_reservas,
        tools.reservar_area,
        tools.cancelar_reserva,
    ],
    # Terminado o atendimento, a próxima mensagem volta ao agente principal.
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

especialista_visitantes = LlmAgent(
    name="especialista_visitantes",
    model=_modelo(MODELO_ESPECIALISTAS),
    description="Autoriza a entrada de visitantes e lista os visitantes do morador.",
    instruction=REGRAS_COMUNS + """
Você cuida da autorização de visitantes.
- Para liberar a entrada, chame `autorizar_visitante` com o nome e a data.
  Toda autorização precisa ser aprovada pelo morador na confirmação do
  aplicativo; avise que ela está aguardando essa aprovação. Frases como
  "já estou confirmando aqui" não contam como aprovação.
- Para listar, use `listar_meus_visitantes`.
""",
    tools=[tools.listar_meus_visitantes, tools.autorizar_visitante],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)

especialista_regulamento = LlmAgent(
    name="especialista_regulamento",
    model=_modelo(MODELO_ESPECIALISTAS),
    description=(
        "Responde dúvidas sobre o regulamento interno do condomínio. Envie a "
        "pergunta do morador em `request`."
    ),
    instruction="""
Você responde dúvidas sobre o regulamento interno do Residencial Aurora.
1. Chame `listar_capitulos_regulamento` e escolha o capítulo do assunto.
2. Chame `ler_capitulo_regulamento` só para esse capítulo (ou, se for
   realmente necessário, para outro capítulo do mesmo assunto).
3. Responda em uma ou duas frases, só com o que responde à pergunta, citando o
   artigo. Não copie trechos longos e não mencione outros capítulos.
Se o regulamento não tratar do assunto, diga isso.
""",
    tools=[tools.listar_capitulos_regulamento, tools.ler_capitulo_regulamento],
)

assistente_aurora = LlmAgent(
    name="assistente_aurora",
    model=_modelo(MODELO_PRINCIPAL),
    description="Assistente virtual do Residencial Aurora.",
    instruction=REGRAS_COMUNS + """
Você é o assistente virtual do Residencial Aurora e direciona cada pedido:
- reservas, cancelamentos, disponibilidade e "minhas reservas" de salão de
  festas, churrasqueira ou quadra: transfira para `especialista_reservas`;
- autorização de visitantes e "meus visitantes": transfira para
  `especialista_visitantes`;
- dúvidas sobre regras do condomínio (horários, piscina, animais, obras,
  mudanças, silêncio etc.): chame a tool `especialista_regulamento` com a
  pergunta e repasse a resposta ao morador.
Você não conhece o regulamento: nunca responda regras de memória.
""",
    sub_agents=[especialista_reservas, especialista_visitantes],
    tools=[AgentTool(agent=especialista_regulamento)],
)

root_agent = assistente_aurora
