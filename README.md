# terminalIA

Página única de **chat**, responsiva, com a aparência nativa do painel OpenHands
(Agent Canvas). Reúne:

- **Chat** com um modelo **Dolphin 3.0 Llama 3.1 8B** (uncensored, GGUF) rodando **100% local** via [llama.cpp](https://github.com/ggml-org/llama.cpp). Sem APIs externas de inferência e sem chaves de provedores.
- **Agente que escreve e executa código** em Python, JavaScript e Node.js de verdade no servidor, durante a conversa (skill via *function calling* com JSON restrito). Também instala pacotes (pip/npm) e manipula arquivos do workspace.
- **Autenticação básica** protegendo a API e o agente.

```
navegador (celular/desktop)
        |  HTTP  /api/chat  -->  server.py  -->  llama-server (llama.cpp, local)
        +  HTTP  /           -->  static/index.html
```

## As ferramentas do agente

O agente não só mostra código: ele **executa** de verdade no servidor e usa o resultado real para
responder. O protocolo é a *function calling* do llama.cpp com **JSON restrito por schema**
(`response_format: json_schema`), o que torna o comportamento confiável. Ferramentas disponíveis:

| Ação    | Exemplo de resposta do modelo                                            | O que faz                                   |
|---------|--------------------------------------------------------------------------|---------------------------------------------|
| `run`   | `{"action":"run","lang":"python","code":"print(2**10)"}`                 | executa código (python/javascript/nodejs)   |
| `pip`   | `{"action":"pip","packages":"requests"}`                                 | instala pacotes Python                      |
| `npm`   | `{"action":"npm","packages":"lodash"}`                                   | instala pacotes Node                        |
| `write` | `{"action":"write","path":"dados.txt","content":"ola"}`                  | cria/sobrescreve arquivo no workspace       |
| `read`  | `{"action":"read","path":"dados.txt"}`                                   | lê arquivo do workspace                     |
| `list`  | `{"action":"list","path":"."}`                                           | lista arquivos do workspace                 |
| `reply` | `{"action":"reply","text":"2 elevado a 10 é 1024."}`                     | conversa normal                             |

Fluxo: o modelo escolhe uma ferramenta → o backend executa (timeout 20s; instalações até 300s) →
devolve o resultado real ao modelo → o modelo responde com `reply`. O site mostra cada passo
executado e sua saída.

**Workspace do agente:** as ferramentas de arquivo operam em `agent_home/` (ao lado do repositório),
e o código roda nesse mesmo diretório — então `write` + `run` funcionam juntos. Caminhos são
confinados ao workspace (`../` é rejeitado). Pacotes pip/npm são instalados no ambiente do servidor.

## Modelo

Padrão: **Dolphin 3.0 Llama 3.1 8B** (`Q4_K_M`, ~4.6 GB), do repo
[`bartowski/Dolphin3.0-Llama3.1-8B-GGUF`](https://huggingface.co/bartowski/Dolphin3.0-Llama3.1-8B-GGUF).
É *uncensored por fine-tune* (sem recusas), bom em código e **tool calling** — casa com o protocolo
do agente. Testado neste projeto:

| Pedido | Dolphin 3.0 8B | Llama 3.2 3B Q2_K (antigo) |
|--------|----------------|----------------------------|
| `2**10` em Python | ✅ `1024` (autocorrigiu o código) | ✅ (simples) |
| quadrados em JavaScript | ✅ `1 4 9` | ❌ código inválido |
| criar arquivo + listar | ✅ funcionou | ⚠️ irregular |
| `pip install` + usar | ✅ funcionou | ⚠️ irregular |
| pedido criativo/"sem filtro" | ✅ responde direto | ✅ |

A escolha do quant importa: o antigo **Q2_K** (1.3 GB) era rápido mas degradava o raciocínio. Para
trocar de modelo sem editar código:

```bash
export MODEL_FILE=/workspace/models/outro.gguf
export MODEL_URL="https://huggingface.co/<repo>/resolve/main/<arquivo>.gguf"
bash scripts/start.sh
```

## Requisitos

- Linux com Python 3.10+, `git`, `gcc/g++`, `make`, `wget`.
- ~4.6 GB em disco para o modelo e ~1.5 GB para compilar o llama.cpp.
- **CPU apenas** (não é necessário GPU). O Dolphin 8B Q4_K_M roda em ~6 GB de RAM.

> Observação sobre recursos: validado em um ambiente com **4 vCPU / 15 GB**, onde o Dolphin 8B carrega
> em ~12 s e gera a ~6–16 tokens/s em CPU. Se faltar RAM, reduza o contexto (`LLAMA_CTX`) ou use um
> modelo/quant menor.

## Como rodar

```bash
git clone https://github.com/Souzzaaxzy/terminalIA.git
cd terminalIA
bash scripts/start.sh
```

O script, de forma idempotente:

1. instala o `cmake` (pip ou apt) se faltar;
2. clona e compila o `llama.cpp` (target `llama-server`);
3. baixa o `dolphin3-8b-Q4_K_M.gguf` do Hugging Face;
4. sobe o `llama-server` em `127.0.0.1:8080`;
5. sobe o site em `0.0.0.0:12000`.

Acesse **http://localhost:12000** (ou a URL pública do Codespaces na porta 12000).

## Credenciais

Padrão: usuário `admin`, senha `terminalia`. **Troque antes de expor publicamente:**

```bash
export BASIC_USER=meuuser BASIC_PASS='umaSenhaForte'
bash scripts/start.sh
```

Variáveis aceitas pelo `server.py`:

| Variável      | Padrão                  | Descrição                          |
|---------------|-------------------------|------------------------------------|
| `PORT`        | `12000`                 | porta HTTP do site                 |
| `BIND`        | `0.0.0.0`               | interface de escuta                |
| `LLAMA_URL`   | `http://127.0.0.1:8080` | base do llama-server               |
| `BASIC_USER`  | `admin`                 | usuário do basic auth              |
| `BASIC_PASS`  | `terminalia`            | senha do basic auth                |

## Segurança

- `/api/*` exigem `Authorization: Basic ...`. A casca estática é pública apenas para que o frontend
  possa coletar as credenciais.
- O agente executa código e instala pacotes **no próprio servidor**. As ferramentas de arquivo são
  confinadas a `agent_home/` (caminhos com `../` são rejeitados), mas `pip`/`npm` instalam no
  ambiente do servidor.
- Para expor na internet, use HTTPS e uma senha forte: quem entra pode rodar código no host.

## Endpoints

| Método | Rota          | Descrição                                    |
|--------|---------------|----------------------------------------------|
| `GET`  | `/`           | página única de chat (responsiva)            |
| `POST` | `/api/chat`   | `{messages:[...]}` -> resposta do agente     |
| `GET`  | `/api/health` | estado do site e do modelo                   |

## Estrutura

```
server.py            backend FastAPI (agente + ferramentas + basic auth)
static/index.html    página única de chat (aparência nativa OpenHands, responsiva)
static/favicon.svg   ícone
scripts/start.sh     instalação e inicialização
```

## Testes manuais

```bash
# saúde (autenticado)
curl -u admin:terminalia http://localhost:12000/api/health

# chat
curl -u admin:terminalia -X POST http://localhost:12000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"diga ola"}]}'
```
