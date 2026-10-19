# Residencial Aurora: assistente virtual com Google ADK

API em Python (FastAPI + Google ADK 2.12.0 + Gemini) para os moradores do Residencial Aurora reservarem áreas comuns, cancelarem as próprias reservas, autorizarem visitantes e tirarem dúvidas sobre o regulamento. A ideia central: **o modelo decide o caminho, o código decide o que é permitido.** Nenhuma regra crítica depende do prompt.

```
aurora/
├── agent.py      # agente principal + 3 especialistas
├── tools.py      # tools (únicas que leem/gravam reservas e visitantes)
├── db.py         # SQLite do condomínio (reservas, visitantes, sessão -> apartamento)
├── api.py        # FastAPI: contrato da API, Runner, confirmações
├── restaurar.py  # comando de restauração dos dados
└── config.py     # caminhos e variáveis de ambiente
dados/            # estado inicial (somente leitura, nunca alterado)
var/              # gerado em tempo de execução: condominio.db e sessoes.db (fora do Git)
```

## Arquitetura

O morador sempre fala com o **`assistente_aurora`** (agente principal). Ele não conhece o regulamento nem acessa dados: só entende o pedido e o direciona.

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente_aurora` | Conversa com o morador e roteia o pedido | Raiz do `App`; o `Runner` volta a ele a cada mensagem nova | Um único ponto de entrada mantém a conversa coerente e o prompt principal pequeno |
| `especialista_reservas` | Áreas, disponibilidade, reservar, cancelar, listar as reservas do morador | **Sub-agente por transferência** (`transfer_to_agent`) | A reserva de área com taxa pede confirmação ao morador. Como sub-agente, os eventos dele (inclusive o `adk_request_confirmation`) ficam na sessão persistida, e o `Runner` devolve a resposta da confirmação a ele, que reexecuta a tool |
| `especialista_visitantes` | Autorizar visitantes e listar os visitantes do morador | **Sub-agente por transferência** | Mesmo motivo: toda autorização pede confirmação, e a retomada precisa chegar ao agente que fez o pedido |
| `especialista_regulamento` | Responder dúvidas com base em `dados/regulamento.md` | **`AgentTool`** do agente principal | O `AgentTool` roda o especialista num `Runner` com sessão própria em memória. A leitura dos capítulos acontece lá dentro, e só a resposta final, curta, volta como resultado da tool para a sessão do morador. O texto do regulamento nunca entra no histórico persistido |

Detalhes da topologia:

- Os especialistas de reservas e visitantes têm `disallow_transfer_to_parent=True` e `disallow_transfer_to_peers=True` ([aurora/agent.py](aurora/agent.py)). Por isso o roteador do ADK não os considera "ativos" para a próxima mensagem: cada mensagem nova começa no agente principal, que escolhe de novo o especialista. Já a **resposta de uma confirmação** é um `FunctionResponse`, e nesse caso o roteador (`google/adk/agents/_agent_router.py`) procura o evento com o `FunctionCall` correspondente e retoma o **autor** dele, que é o especialista que pediu a confirmação. Essa combinação foi testada com a sessão persistida em SQLite, inclusive aprovando uma confirmação criada antes de reiniciar a API.
- Os especialistas recebem o histórico da conversa reescrito pelo ADK ("For context: ..."), então entendem o pedido sem que o agente principal precise repassá-lo.
- Modelos: `gemini-3.8-flash` por padrão para todos, configurável no `.env`. Cada modelo tem `retry_options` (5 tentativas com espera crescente) para absorver erros 429/5xx do Gemini.

### Fluxo de uma confirmação

1. `POST /sessoes/{id}/mensagens` → agente principal → `especialista_reservas` → `reservar_area("salao-de-festas", "2030-04-20")`.
2. A tool vê que a área tem taxa e que ainda não há confirmação. Ela chama `tool_context.request_confirmation(...)` e retorna **sem gravar**. O ADK emite um `FunctionCall` `adk_request_confirmation` e a execução para.
3. A API varre os eventos da sessão e lista em `confirmacoes_pendentes` todo `adk_request_confirmation` que ainda não tem `FunctionResponse` (`id`, `acao` = nome da tool, `detalhes` = argumentos originais + payload).
4. `POST /sessoes/{id}/confirmacoes` valida que o `id` está pendente **nesta** sessão (senão, `409`) e envia ao `Runner` um `FunctionResponse(id, name="adk_request_confirmation", response={"confirmed": ...})`.
5. O ADK retoma o especialista e reexecuta `reservar_area` com os **mesmos argumentos originais**, agora com `tool_context.tool_confirmation` preenchido. Se a resposta for aprovação, grava. Se for negação, devolve "recusada" sem gravar.

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

- **Onde:** [aurora/tools.py](aurora/tools.py), em `reservar_area` (bloco `if a["taxa"] > 0:` com `tool_context.tool_confirmation` / `tool_context.request_confirmation`) e em `autorizar_visitante` (o mesmo padrão, sem condição: toda autorização pede confirmação). A rota fica em [aurora/api.py](aurora/api.py): `responder_confirmacao` e `confirmacoes_pendentes`.
- **Por que não depende do modelo:** a gravação só acontece quando `tool_context.tool_confirmation.confirmed` é verdadeiro. Esse valor só existe quando o ADK recebe um `FunctionResponse` do `adk_request_confirmation`, e só a rota `/confirmacoes` cria esse `FunctionResponse`. O texto "já estou confirmando aqui" chega ao modelo como texto comum e não preenche `tool_confirmation`. Na retomada, o ADK usa os argumentos do `FunctionCall` original, então o modelo não troca a área, a data ou o nome entre o pedido e a aprovação.
- **Uma única execução:** a rota relê os eventos da sessão dentro de uma trava por sessão e só aceita um `id` sem `FunctionResponse`. Depois de responder, o `FunctionResponse` fica gravado, e o mesmo `id` passa a receber `409`. Um `id` de outra sessão ou inexistente também recebe `409`. A área sem taxa (quadra) é gravada direto, sem confirmação.

### Garantia 2: cada sessão pertence a um apartamento

- **Onde:** `criar_sessao` em [aurora/api.py](aurora/api.py) grava o vínculo com `db.registrar_sessao` (tabela `sessoes` em [aurora/db.py](aurora/db.py)). Toda tool que lê ou grava dados do morador chama `_apartamento_da_sessao(tool_context)` em [aurora/tools.py](aurora/tools.py), que busca o apartamento pelo `tool_context.session.id`.
- **Por que não depende do modelo:** **nenhuma tool tem parâmetro de apartamento**. O modelo escolhe só área, data, nome ou código. O `session_id` vem do `Runner`, não do modelo, e o vínculo sessão → apartamento é gravado uma vez e nunca é atualizado. Todas as consultas de [aurora/db.py](aurora/db.py) filtram por esse apartamento (`cancelar_reserva` exige `apartamento = ?`, por isso "cancele a reserva do 302" não encontra nada). `consultar_disponibilidade` e a verificação dentro de `reservar_area` usam `db.data_livre`, que devolve só verdadeiro/falso, nunca o código nem o apartamento da reserva existente. A resposta "não encontrada" é a mesma exista ou não reserva de outro apartamento.

### Garantia 3: nada se perde no reinício

- **Onde:** `lifespan` em [aurora/api.py](aurora/api.py) usa `SqliteSessionService(str(SESSOES_DB))` (`var/sessoes.db`) para sessões e eventos do ADK. Os dados do condomínio ficam em `var/condominio.db` ([aurora/db.py](aurora/db.py)). `db.inicializar()` só carrega os arquivos de `dados/` **na primeira vez** (flag `carregado` na tabela `meta`). Subir a API de novo não restaura nada.
- **Por que não depende do modelo:** tudo vive em arquivos SQLite. O modelo não tem memória própria: reservas e visitantes sempre são lidos pelas tools. Os códigos são gerados em `_novo_codigo` e registrados em `codigos_emitidos`, uma tabela que nunca é limpa (nem pela restauração), então um código nunca se repete, nem o de uma reserva cancelada.

### Garantia 4: o regulamento é consultado, não carregado

- **Onde:** `especialista_regulamento` e `tools=[AgentTool(agent=especialista_regulamento)]` em [aurora/agent.py](aurora/agent.py). As tools `listar_capitulos_regulamento` e `ler_capitulo_regulamento` estão em [aurora/tools.py](aurora/tools.py).
- **Por que não depende do modelo:** a instrução do `assistente_aurora` não contém o regulamento, e ele não tem tool que leia o arquivo. O `AgentTool` do ADK (`google/adk/tools/agent_tool.py`) executa o especialista num `Runner` com `InMemorySessionService` próprio: as chamadas `listar_capitulos`/`ler_capitulo` e os textos dos capítulos ficam nessa sessão descartável, e só a resposta final entra na sessão do morador, como resultado da tool `especialista_regulamento`. Mesmo dentro do especialista, `ler_capitulo_regulamento` devolve **um capítulo por vez**, nunca o arquivo inteiro.

### Garantia 5: dois moradores, uma reserva

- **Onde:** índice `ux_reserva_ativa_por_area_data` em [aurora/db.py](aurora/db.py):
  ```sql
  CREATE UNIQUE INDEX ux_reserva_ativa_por_area_data ON reservas (area, data) WHERE status = 'ativa';
  ```
  `criar_reserva` faz o `INSERT` dentro de `BEGIN IMMEDIATE` e converte o `IntegrityError` em `DataOcupada`. `reservar_area` ([aurora/tools.py](aurora/tools.py)) transforma isso numa resposta normal (`status: indisponivel`), e a rota responde `200`.
- **Por que não depende do modelo (nem da conferência prévia):** a conferência `data_livre` antes de pedir a confirmação serve só para dar uma resposta rápida. Quem garante a exclusividade é o SQLite, no instante do `INSERT`: o índice único parcial impede uma segunda reserva **ativa** para a mesma área e data, não importa quantas gravações cheguem juntas. Uma reserva cancelada sai do índice, e a data volta a ficar livre.

## Como rodar

### Pré-requisitos

- Python 3.12+ e [uv](https://docs.astral.sh/uv/).
- Uma chave do [Google AI Studio](https://aistudio.google.com/apikey).
- Nenhum serviço externo: o armazenamento é SQLite, criado em `var/`.

### Variáveis do `.env`

```bash
cp .env.example .env
```

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio |
| `AURORA_MODELO_PRINCIPAL` | não | Modelo do agente principal (padrão `gemini-3.8-flash`) |
| `AURORA_MODELO_ESPECIALISTAS` | não | Modelo dos especialistas (padrão `gemini-3.8-flash`) |

### Instalar

```bash
uv sync
```

### Restaurar os dados iniciais

Volta reservas e visitantes ao estado de `dados/`. Por padrão mantém as sessões. Com `--sessoes`, apaga também as sessões e os eventos.

```bash
uv run python -m aurora.restaurar
```

```bash
uv run python -m aurora.restaurar --sessoes
```

Na primeira subida, se o banco ainda não existir, a API carrega os dados iniciais sozinha.

### Subir a API

```bash
uv run uvicorn aurora.api:app --host 0.0.0.0 --port 8000
```

A API responde em `http://localhost:8000` (documentação interativa em `/docs`).

### Exemplo rápido

```bash
curl -s -X POST localhost:8000/sessoes -H "Content-Type: application/json" -d '{"apartamento": "101"}'
```

```bash
curl -s -X POST localhost:8000/sessoes/<session_id>/mensagens -H "Content-Type: application/json" -d '{"texto": "Reserve o salão de festas para 2030-04-20."}'
```

```bash
curl -s -X POST localhost:8000/sessoes/<session_id>/confirmacoes -H "Content-Type: application/json" -d '{"id": "<id da confirmação>", "confirmado": true}'
```

### Rotas

| Método e caminho | Descrição |
|---|---|
| `POST /sessoes` | Cria a sessão de um apartamento (`201`) |
| `POST /sessoes/{id}/mensagens` | Envia mensagem; devolve `resposta` e `confirmacoes_pendentes` |
| `POST /sessoes/{id}/confirmacoes` | Aprova ou nega uma confirmação pendente (`409` se o id não estiver pendente nesta sessão) |
| `GET /sessoes/{id}/eventos` | Todos os eventos da sessão, em ordem |
| `GET /apartamentos/{n}/reservas` | Reservas ativas do apartamento (verificação, sem modelo) |
| `GET /apartamentos/{n}/visitantes` | Visitantes autorizados do apartamento (verificação, sem modelo) |

Rotas com `{id}` respondem `404` quando a sessão não existe.
