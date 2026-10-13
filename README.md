# terminalIA

Página única, responsiva e com tema *terminal hacker* que reúne:

- **Chat** com um modelo **Llama 3.2 3B Uncensored (GGUF)** rodando **100% local** via [llama.cpp](https://github.com/ggml-org/llama.cpp). Sem APIs externas de inferência e sem chaves de provedores.
- **Agente que escreve e executa código** em Python, JavaScript e Node.js de verdade no servidor, durante a conversa (skill via *function calling* com JSON restrito).
- **Terminal Linux real** no servidor, acessível pelo navegador do celular (xterm.js + WebSocket + pty).
- **Autenticação básica** protegendo a IA e o terminal.

```
navegador (celular/desktop)
        |  HTTP  /api/chat  -->  server.py  -->  llama-server (llama.cpp, local)
        |  WS    /ws/terminal -> server.py  -->  pty (/bin/bash)
        +  HTTP  /           -->  static/index.html
```

## A skill de código do agente

O agente não só mostra código: ele **executa** Python, JavaScript e Node.js no servidor e usa a
saída real para responder. O protocolo é a *function calling* do llama.cpp com **JSON restrito por
schema** (`response_format: json_schema`), o que torna o comportamento confiável:

```json
{"action":"run","lang":"python","code":"print(2**10)"}   -> executa
{"action":"reply","text":"2 elevado a 10 é 1024."}       -> conversa
```

Fluxo: o modelo decide `run` → o backend executa (timeout 20s) → devolve stdout/stderr ao modelo →
o modelo responde com `reply`. O site mostra cada bloco executado e sua saída.

**Importante sobre a quantização:** a escolha do quant muda tudo. Com **Q2_K** o modelo é rápido e
leve, mas degrada demais o raciocínio e frequentemente escreve código errado ou ignora o protocolo.
Com **Q4_K_M** (padrão do `start.sh`, ~2 GB) o agente executa corretamente. Testado:

| Pedido | Q4_K_M | Q2_K |
|--------|--------|------|
| `2**10` em Python | ✅ `1024` | ✅ (simples) |
| quadrados em JavaScript | ✅ `1 4 9` | ❌ código inválido |
| versão do Node | ✅ `v24.21.0` | ❌ alucinou valor |

Para economizar disco/RAM você ainda pode usar `MODEL_QUANT=Q2_K`, aceitando essas limitações.

## Requisitos

- Linux com Python 3.10+, `git`, `gcc/g++`, `make`, `wget`.
- ~2 GB em disco para o modelo Q4_K_M (ou ~1.3 GB com `MODEL_QUANT=Q2_K`) e ~1.5 GB para compilar o llama.cpp.
- **CPU apenas** (não é necessário GPU). O modelo roda em ~3 GB de RAM (Q4_K_M) ou ~2 GB (Q2_K).

> Observação sobre recursos: validado em um ambiente com **4 vCPU / 15 GB**, onde o modelo carrega em
> poucos segundos e gera a ~12–16 tokens/s em CPU. O Codespaces padrão (2-core / 8 GB) também
> executa. Se faltar RAM, reduza o contexto (`LLAMA_CTX`) ou use um quant menor.

## Como rodar

```bash
git clone https://github.com/Souzzaaxzy/terminalIA.git
cd terminalIA
bash scripts/start.sh
```

O script, de forma idempotente:

1. instala o `cmake` (pip ou apt) se faltar;
2. clona e compila o `llama.cpp` (target `llama-server`);
3. baixa o `model-Q2_K.gguf` do Hugging Face;
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

- `/api/*` e `/ws/*` exigem `Authorization: Basic ...`. A casca estática é pública apenas para que o
  frontend possa coletar as credenciais.
- O WebSocket não recebe o header `Authorization` no handshake do navegador; por isso a credencial
  viaja em um **subprotocolo** `basic.<base64url(user:pass)>`, validado no servidor.
- Para expor na internet, use HTTPS e uma senha forte. O terminal dá acesso a um shell real no host.

## Endpoints

| Método | Rota            | Descrição                                   |
|--------|-----------------|---------------------------------------------|
| `GET`  | `/`             | página única (chat + terminal)              |
| `POST` | `/api/chat`     | `{messages:[...]}` -> resposta do modelo    |
| `GET`  | `/api/health`   | estado do site e do modelo                  |
| `WS`   | `/ws/terminal`  | shell Linux (autenticado)                   |

## Estrutura

```
server.py            backend FastAPI (chat proxy + terminal WS + basic auth)
static/index.html    página única (chat + terminal, tema hacker, responsiva)
static/vendor/       xterm.js + addon-fit (vendorizados, sem CDN em runtime)
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
