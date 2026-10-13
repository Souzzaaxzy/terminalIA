#!/usr/bin/env python3
"""terminalIA - chat local (llama.cpp) + terminal Linux web, com autenticacao basica.

Servicos:
  - Chat:      POST /api/chat        -> proxy para o llama-server local (sem APIs externas)
  - Terminal:  WS   /ws/terminal     -> pty Linux real (xterm.js no frontend)
  - Estaticos: GET  /                -> single page responsiva (tema hacker)

Variaveis de ambiente:
  PORT           porta HTTP do site          (padrao 12000)
  BIND           interface de escuta         (padrao 0.0.0.0)
  LLAMA_URL      base do llama-server        (padrao http://127.0.0.1:8080)
  BASIC_USER     usuario do basic auth       (padrao admin)
  BASIC_PASS     senha do basic auth         (padrao terminalia)
"""

import asyncio
import base64
import fcntl
import json
import os
import pty
import re
import secrets
import signal
import struct
import subprocess
import tempfile
import termios
import urllib.error
import urllib.request

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

PORT = int(os.environ.get("PORT", "12000"))
BIND = os.environ.get("BIND", "0.0.0.0")
LLAMA_URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:8080").rstrip("/")
BASIC_USER = os.environ.get("BASIC_USER", "admin")
BASIC_PASS = os.environ.get("BASIC_PASS", "terminalia")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Instrui o modelo a usar a ferramenta de execucao de codigo (skill do agente).
# A saida e restringida por JSON schema (decodificacao guiada), o que torna o
# protocolo confiavel mesmo com um modelo quantizado pequeno (Q2_K).
SYSTEM_PROMPT = """Voce e o terminalIA, um agente que roda 100% local via llama.cpp.
Voce responde SEMPRE com um objeto JSON, em uma das duas acoes:

1) "run"  -> executa codigo de verdade no servidor (python, javascript ou nodejs)
   {"action":"run","lang":"python","code":"print(2**10)"}

2) "reply" -> conversa normal, sem executar codigo
   {"action":"reply","text":"sua resposta"}

Use "run" SEMPRE que o usuario pedir para calcular, contar, testar, gerar,
transformar ou rodar codigo. Nao invente resultados: rode o codigo.
Depois de um "run", o sistema devolve a saida real e voce responde com "reply".

Exemplos:
Usuario: quanto e 2 elevado a 10?
{"action":"run","lang":"python","code":"print(2**10)"}
(sistema devolve stdout: 1024)
{"action":"reply","text":"2 elevado a 10 e 1024."}

Usuario: ola, tudo bem?
{"action":"reply","text":"Ola! Tudo bem. Como posso ajudar?"}

Regras: lang deve ser "python", "javascript" ou "nodejs". Responda no idioma do usuario."""

# Schema que restringe a saida do modelo a um objeto JSON valido.
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["run", "reply"]},
        "lang": {"type": "string", "enum": ["python", "javascript", "nodejs"]},
        "code": {"type": "string"},
        "text": {"type": "string"},
    },
    "required": ["action"],
}

# Fallback: se o modelo escapar do JSON, aceitamos <run> ou cerca ```lang.
RUN_RE = re.compile(
    r"<run\s+lang\s*=\s*[\"']?\s*(python|py|javascript|js|node|nodejs)\s*[\"']?\s*>(.*?)</run>",
    re.DOTALL | re.IGNORECASE,
)
FENCE_RE = re.compile(
    r"```\s*(python|py|javascript|js|node|nodejs)\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

app = FastAPI(title="terminalIA", docs_url=None, redoc_url=None, openapi_url=None)


# --------------------------------------------------------------------------- #
# Autenticacao basica (HTTP e WebSocket)
# --------------------------------------------------------------------------- #
def _valid_credentials(user: str, pwd: str) -> bool:
    return secrets.compare_digest(user, BASIC_USER) and secrets.compare_digest(pwd, BASIC_PASS)


def _decode_basic(token: str) -> tuple[str, str] | None:
    """Decodifica 'base64(user:pass)' (base64 padrao ou base64url)."""
    try:
        token = token.replace("-", "+").replace("_", "/")
        token += "=" * (-len(token) % 4)
        decoded = base64.b64decode(token).decode("utf-8")
        user, _, pwd = decoded.partition(":")
        return user, pwd
    except Exception:
        return None


def _check_auth(header: str | None) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    creds = _decode_basic(header.split(" ", 1)[1].strip())
    return bool(creds) and _valid_credentials(*creds)


def _unauthorized() -> Response:
    return Response(
        status_code=401,
        content="Acesso restrito. Informe usuario e senha.",
        headers={"WWW-Authenticate": 'Basic realm="terminalIA"'},
    )


# Protegemos a API e o terminal. A casca HTML/CSS/JS e publica para que o
# frontend possa coletar as credenciais e repassa-las ao WebSocket (que nao
# recebe o header Authorization pelo handshake do navegador).
PROTECTED_PREFIXES = ("/api/", "/ws/")


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if request.url.path.startswith(PROTECTED_PREFIXES):
        if not _check_auth(request.headers.get("authorization")):
            return _unauthorized()
    return await call_next(request)


# --------------------------------------------------------------------------- #
# Chat: agente local com ferramenta de execucao de codigo (skill)
# --------------------------------------------------------------------------- #
def _llama_chat(messages: list, max_tokens: int, temperature: float) -> dict:
    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        # Decodificacao guiada: forca a saida a ser um objeto JSON valido.
        "response_format": {"type": "json_schema", "json_schema": {"schema": TOOL_SCHEMA}},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{LLAMA_URL}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return JSONResponse({"error": "campo 'messages' ausente ou invalido"}, status_code=400)

    max_tokens = int(body.get("max_tokens", 512))
    temperature = float(body.get("temperature", 0.7))
    max_steps = min(int(body.get("max_steps", 4)), 8)
    loop = asyncio.get_running_loop()

    convo = [{"role": "system", "content": SYSTEM_PROMPT}] + list(messages)
    steps: list[dict] = []
    usage_total = {}

    try:
        for _ in range(max_steps + 1):
            result = await loop.run_in_executor(
                None, _llama_chat, convo, max_tokens, temperature
            )
            if result.get("usage"):
                usage_total = result["usage"]
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            action = _parse_action(content)

            # Conversa normal: devolve o texto.
            if action is None or action["action"] == "reply":
                return {"reply": action["text"] if action else content, "steps": steps, "usage": usage_total}

            # Executa o codigo pedido pelo agente.
            lang = action["lang"]
            code = action["code"]
            if not code.strip():
                return {"reply": "O agente pediu para executar codigo vazio.", "steps": steps, "usage": usage_total}

            run = await loop.run_in_executor(None, _run_code, lang, code, CODE_TIMEOUT)
            run.update({"lang": lang, "code": code})
            steps.append(run)

            # Realimenta o modelo com a saida real do codigo.
            convo.append({"role": "assistant", "content": content})
            convo.append({"role": "user", "content": _format_tool_output(run)})

        return {
            "reply": "Limite de execucoes atingido nesta mensagem.",
            "steps": steps,
            "usage": usage_total,
        }
    except urllib.error.URLError as exc:
        return JSONResponse(
            {"error": f"llama-server indisponivel em {LLAMA_URL}: {exc}"}, status_code=502
        )


# --------------------------------------------------------------------------- #
# Executor de codigo: Python, JavaScript e Node.js rodando de verdade
# --------------------------------------------------------------------------- #
LANG_SPEC = {
    "python": {"cmd": ["python3", "-"], "ext": "py"},
    "javascript": {"cmd": ["node", "-"], "ext": "js"},   # mesmo runtime do Node
    "nodejs": {"cmd": ["node", "-"], "ext": "js"},
}
CODE_TIMEOUT = int(os.environ.get("CODE_TIMEOUT", "20"))
CODE_MAX_OUTPUT = 100_000  # bytes


def _run_code(lang: str, code: str, timeout: int) -> dict:
    spec = LANG_SPEC[lang]
    try:
        proc = subprocess.run(
            spec["cmd"],
            input=code.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=tempfile.gettempdir(),
        )
        return {
            "stdout": proc.stdout[:CODE_MAX_OUTPUT].decode("utf-8", "replace"),
            "stderr": proc.stderr[:CODE_MAX_OUTPUT].decode("utf-8", "replace"),
            "exit_code": proc.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "stdout": (exc.stdout or b"")[:CODE_MAX_OUTPUT].decode("utf-8", "replace"),
            "stderr": (exc.stderr or b"")[:CODE_MAX_OUTPUT].decode("utf-8", "replace"),
            "exit_code": None,
            "timed_out": True,
        }


_LANG_ALIASES = {
    "python": "python", "py": "python",
    "javascript": "javascript", "js": "javascript",
    "node": "nodejs", "nodejs": "nodejs",
}


def _normalize_lang(raw: str) -> str:
    return _LANG_ALIASES.get(str(raw).strip().lower(), "python")


def _parse_action(content: str) -> dict | None:
    """Interpreta a decisao do agente.

    Caminho normal: o modelo responde JSON (garantido pelo json_schema).
    Fallbacks: bloco <run> ou cerca ```lang, caso o JSON escape.
    """
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        obj = None

    if isinstance(obj, dict):
        action = str(obj.get("action", "")).lower()
        if action == "run":
            return {"action": "run", "lang": _normalize_lang(obj.get("lang")), "code": str(obj.get("code", ""))}
        if action == "reply":
            return {"action": "reply", "text": str(obj.get("text", ""))}

    match = RUN_RE.search(content or "") or FENCE_RE.search(content or "")
    if match:
        return {"action": "run", "lang": _normalize_lang(match.group(1)), "code": match.group(2).strip()}
    return None


def _format_tool_output(run: dict) -> str:
    """Monta a mensagem devolvida ao modelo com a saida real do codigo."""
    if run["timed_out"]:
        return (
            f"RESULTADO DA EXECUCAO: timeout de {CODE_TIMEOUT}s, o codigo nao terminou.\n"
            'Responda com {"action":"run", ...} com um codigo mais rapido, ou explique a falha em "reply".'
        )
    parts = [f"RESULTADO DA EXECUCAO (exit_code={run['exit_code']}):"]
    if run["stdout"]:
        parts.append("stdout:\n" + run["stdout"].rstrip())
    if run["stderr"]:
        parts.append("stderr:\n" + run["stderr"].rstrip())
    if not run["stdout"] and not run["stderr"]:
        parts.append("(sem saida)")
    if run["exit_code"] == 0 and not run["stdout"] and not run["stderr"]:
        parts.append(
            "O codigo rodou mas NAO imprimiu nada. Adicione print(...) (Python) ou "
            'console.log(...) (JS/Node) e responda com {"action":"run", ...} de novo. '
            "Nunca invente um valor que voce nao viu na saida."
        )
    elif run["exit_code"] == 0:
        parts.append(
            'Agora responda ao usuario com {"action":"reply","text":"..."} usando SOMENTE a saida real '
            "acima (nao invente valores), no idioma da mensagem do usuario."
        )
    else:
        parts.append(
            'O codigo falhou. Corrija o erro e responda com {"action":"run", ...} de novo, '
            "ou explique a falha com {\"action\":\"reply\", ...}."
        )
    return "\n".join(parts)


@app.get("/api/health")
async def health():
    loop = asyncio.get_running_loop()
    ok = False
    try:
        req = urllib.request.Request(f"{LLAMA_URL}/health")
        raw = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5).read())
        ok = json.loads(raw.decode()).get("status") == "ok"
    except Exception:
        ok = False
    return {"web": "ok", "model": "ok" if ok else "down", "llama_url": LLAMA_URL}


# --------------------------------------------------------------------------- #
# Terminal: shell Linux real via pty + WebSocket
# --------------------------------------------------------------------------- #
SHELL = os.environ.get("SHELL", "/bin/bash")


async def _pump_to_ws(fd: int, ws: WebSocket) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            data = await loop.run_in_executor(None, os.read, fd, 4096)
        except OSError:
            break
        if not data:
            break
        await ws.send_bytes(data)


def _ws_subprotocol(ws: WebSocket) -> str | None:
    """Retorna o subprotocolo 'basic.<base64url(user:pass)>' valido, se houver.

    O navegador nao permite definir o header Authorization no handshake do
    WebSocket, entao as credenciais viajam no subprotocolo (o servidor precisa
    ecoar exatamente o valor ofertado para concluir o handshake).
    """
    offered = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    for proto in offered:
        if proto.startswith("basic."):
            creds = _decode_basic(proto[len("basic."):])
            if creds and _valid_credentials(*creds):
                return proto
    return None


@app.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket):
    proto = _ws_subprotocol(ws)
    if proto is None and not _check_auth(ws.headers.get("authorization")):
        await ws.close(code=4401)
        return

    # Ecoa o subprotocolo ofertado (exigencia do protocolo WebSocket) ou "basic".
    await ws.accept(subprotocol=proto or "basic")

    # Fork de um shell conectado a um pty novo.
    pid, fd = pty.fork()
    if pid == 0:  # processo filho -> executa o shell
        os.environ["TERM"] = "xterm-256color"
        os.environ["PS1"] = "\\[\\e[1;32m\\]terminalIA\\[\\e[0m\\]:\\w\\$ "
        os.execvp(SHELL, [SHELL, "-i"])
        os._exit(1)

    # Processo pai -> ajusta tamanho inicial do pty.
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    except OSError:
        pass

    reader = asyncio.create_task(_pump_to_ws(fd, ws))
    try:
        while True:
            msg = await ws.receive_text()
            try:
                evt = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if evt.get("t") == "i":  # input do usuario
                os.write(fd, evt.get("d", "").encode("utf-8", "ignore"))
            elif evt.get("t") == "r":  # resize
                try:
                    fcntl.ioctl(
                        fd,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", int(evt["rows"]), int(evt["cols"]), 0, 0),
                    )
                except (OSError, KeyError, ValueError):
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Estaticos
# --------------------------------------------------------------------------- #
@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
