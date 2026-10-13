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
import secrets
import signal
import struct
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

# Sistema de prompt simples para manter o modelo focado em conversa.
SYSTEM_PROMPT = (
    "Voce e o terminalIA, um assistente que roda 100% local via llama.cpp. "
    "Responda de forma direta, util e no idioma do usuario."
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
# Chat: proxy para o llama-server local (inferencia 100% local)
# --------------------------------------------------------------------------- #
@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return JSONResponse({"error": "campo 'messages' ausente ou invalido"}, status_code=400)

    payload = {
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "max_tokens": int(body.get("max_tokens", 512)),
        "temperature": float(body.get("temperature", 0.7)),
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{LLAMA_URL}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # urllib bloqueia; joga em thread para nao travar o event loop.
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=300).read())
    except urllib.error.URLError as exc:
        return JSONResponse(
            {"error": f"llama-server indisponivel em {LLAMA_URL}: {exc}"}, status_code=502
        )
    result = json.loads(raw.decode("utf-8"))
    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    return {"reply": content, "usage": result.get("usage", {})}


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
