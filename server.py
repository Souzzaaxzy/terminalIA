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

# Raiz do agente: os arquivos do agente e o diretorio de trabalho do codigo.
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", os.path.dirname(STATIC_DIR))
AGENT_HOME = os.path.join(WORKSPACE_ROOT, "agent_home")
AGENT_FILES = os.path.join(AGENT_HOME, "files")
os.makedirs(AGENT_FILES, exist_ok=True)

# Instrui o modelo a usar as ferramentas do agente.
# A saida e restringida por JSON schema (decodificacao guiada), o que torna o
# protocolo confiavel mesmo com um modelo quantizado pequeno.
SYSTEM_PROMPT = """Voce e o terminalIA, um agente que roda 100% local via llama.cpp.
Voce responde SEMPRE com um objeto JSON. Escolha uma acao:

1) "run" -> executa codigo no servidor (python, javascript ou nodejs)
   {"action":"run","lang":"python","code":"print(2**10)"}

2) "pip" -> instala pacotes Python com pip
   {"action":"pip","packages":"requests"}

3) "npm" -> instala pacotes Node com npm
   {"action":"npm","packages":"lodash"}

4) "write" -> cria/sobrescreve um arquivo no workspace
   {"action":"write","path":"dados.txt","content":"ola mundo"}

5) "read" -> le um arquivo do workspace
   {"action":"read","path":"dados.txt"}

6) "list" -> lista arquivos do workspace (path opcional)
   {"action":"list","path":"."}

7) "reply" -> conversa normal
   {"action":"reply","text":"sua resposta"}

Regras:
- Use "run" SEMPRE que precisar calcular, contar, testar, gerar ou rodar codigo.
- Se o codigo precisar de um pacote que talvez nao esteja instalado (ex.: requests,
  cowsay, numpy, lodash), chame "pip" (python) ou "npm" (node) PRIMEIRO, em uma
  resposta separada, e so depois use "run". Nunca assuma que o pacote ja existe.
- Use "write"/"read"/"list" para criar e manipular arquivos do workspace.
- Nao invente resultados: execute e use a saida real. Uma acao por resposta.
- "lang" deve ser "python", "javascript" ou "nodejs".
- Responda no idioma do usuario, de forma direta e curta."""

# Schema que restringe a saida do modelo a um objeto JSON valido.
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["run", "pip", "npm", "write", "read", "list", "reply"]},
        "lang": {"type": "string", "enum": ["python", "javascript", "nodejs"]},
        "code": {"type": "string"},
        "packages": {"type": "string"},
        "path": {"type": "string"},
        "content": {"type": "string"},
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

            # Executa a ferramenta escolhida e realimenta o modelo com o resultado real.
            step = await _dispatch(action, loop)
            steps.append(step)
            convo.append({"role": "assistant", "content": content})
            convo.append({"role": "user", "content": _format_tool_output(step)})

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
            cwd=AGENT_HOME,  # o codigo roda onde o agente cria/le arquivos
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


# --------------------------------------------------------------------------- #
# Ferramentas de arquivo (sandbox no workspace do agente)
# --------------------------------------------------------------------------- #
def _safe_path(rel: str) -> str | None:
    """Resolve um caminho relativo dentro do workspace; None se escapar."""
    rel = (rel or ".").strip().lstrip("/\\")
    target = os.path.realpath(os.path.join(AGENT_HOME, rel))
    root = os.path.realpath(AGENT_HOME)
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def _tool_write(path: str, content: str) -> dict:
    target = _safe_path(path)
    if target is None:
        return {"ok": False, "error": "caminho fora do workspace"}
    os.makedirs(os.path.dirname(target) or AGENT_HOME, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
    return {"ok": True, "info": f"arquivo gravado: {os.path.relpath(target, AGENT_HOME)} ({len(content)} bytes)"}


def _tool_read(path: str) -> dict:
    target = _safe_path(path)
    if target is None:
        return {"ok": False, "error": "caminho fora do workspace"}
    if not os.path.isfile(target):
        return {"ok": False, "error": f"arquivo nao encontrado: {path}"}
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        return {"ok": True, "content": fh.read(CODE_MAX_OUTPUT)}


def _tool_list(path: str) -> dict:
    target = _safe_path(path)
    if target is None:
        return {"ok": False, "error": "caminho fora do workspace"}
    if not os.path.isdir(target):
        return {"ok": False, "error": f"diretorio nao encontrado: {path}"}
    entries = []
    for name in sorted(os.listdir(target)):
        full = os.path.join(target, name)
        entries.append(f"{name}/" if os.path.isdir(full) else name)
    return {"ok": True, "entries": entries or ["(vazio)"]}


# --------------------------------------------------------------------------- #
# Ferramentas de instalacao de pacotes (pip / npm)
# --------------------------------------------------------------------------- #
def _tool_pip(packages: str) -> dict:
    pkgs = [p for p in re.split(r"[\s,]+", packages.strip()) if p]
    if not pkgs:
        return {"ok": False, "error": "nenhum pacote informado"}
    proc = subprocess.run(
        ["pip", "install", "--quiet", "--disable-pip-version-check", *pkgs],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300,
    )
    return {
        "ok": proc.returncode == 0,
        "info": proc.stdout[:CODE_MAX_OUTPUT].decode("utf-8", "replace").strip()
        or ("pacotes instalados: " + " ".join(pkgs)),
    }


def _tool_npm(packages: str) -> dict:
    pkgs = [p for p in re.split(r"[\s,]+", packages.strip()) if p]
    if not pkgs:
        return {"ok": False, "error": "nenhum pacote informado"}
    proc = subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund", *pkgs],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300, cwd=AGENT_HOME,
    )
    return {
        "ok": proc.returncode == 0,
        "info": proc.stdout[:CODE_MAX_OUTPUT].decode("utf-8", "replace").strip()
        or ("pacotes instalados: " + " ".join(pkgs)),
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
        if action in ("pip", "npm"):
            return {"action": action, "packages": str(obj.get("packages", ""))}
        if action == "write":
            return {"action": "write", "path": str(obj.get("path", "")), "content": str(obj.get("content", ""))}
        if action == "read":
            return {"action": "read", "path": str(obj.get("path", ""))}
        if action == "list":
            return {"action": "list", "path": str(obj.get("path", "."))}
        if action == "reply":
            return {"action": "reply", "text": str(obj.get("text", ""))}

    match = RUN_RE.search(content or "") or FENCE_RE.search(content or "")
    if match:
        return {"action": "run", "lang": _normalize_lang(match.group(1)), "code": match.group(2).strip()}
    return None


async def _dispatch(action: dict, loop) -> dict:
    """Executa a ferramenta pedida pelo agente e devolve um passo normalizado."""
    kind = action["action"]
    if kind == "run":
        run = await loop.run_in_executor(None, _run_code, action["lang"], action["code"], CODE_TIMEOUT)
        run.update({"kind": "run", "lang": action["lang"], "code": action["code"]})
        return run
    if kind == "pip":
        res = await loop.run_in_executor(None, _tool_pip, action["packages"])
        return {"kind": "pip", "ok": res["ok"], "stdout": res.get("info", ""), "stderr": res.get("error", ""),
                "code": f"pip install {action['packages']}", "lang": "pip", "exit_code": 0 if res["ok"] else 1}
    if kind == "npm":
        res = await loop.run_in_executor(None, _tool_npm, action["packages"])
        return {"kind": "npm", "ok": res["ok"], "stdout": res.get("info", ""), "stderr": res.get("error", ""),
                "code": f"npm install {action['packages']}", "lang": "npm", "exit_code": 0 if res["ok"] else 1}
    if kind == "write":
        res = await loop.run_in_executor(None, _tool_write, action["path"], action["content"])
        return {"kind": "write", "ok": res["ok"], "path": action["path"],
                "stdout": res.get("info", ""), "stderr": res.get("error", ""),
                "code": f"write {action['path']}", "lang": "file", "exit_code": 0 if res["ok"] else 1}
    if kind == "read":
        res = await loop.run_in_executor(None, _tool_read, action["path"])
        return {"kind": "read", "ok": res["ok"], "path": action["path"],
                "stdout": res.get("content", ""), "stderr": res.get("error", ""),
                "code": f"read {action['path']}", "lang": "file", "exit_code": 0 if res["ok"] else 1}
    if kind == "list":
        res = await loop.run_in_executor(None, _tool_list, action["path"])
        listing = "\n".join(res.get("entries", [])) if res["ok"] else ""
        return {"kind": "list", "ok": res["ok"], "path": action["path"],
                "stdout": listing, "stderr": res.get("error", ""),
                "code": f"list {action['path']}", "lang": "file", "exit_code": 0 if res["ok"] else 1}
    return {"kind": kind, "ok": False, "stdout": "", "stderr": f"acao desconhecida: {kind}",
            "code": kind, "lang": kind, "exit_code": 1}


def _format_tool_output(step: dict) -> str:
    """Monta a mensagem devolvida ao modelo com o resultado real da ferramenta."""
    kind = step.get("kind", "run")
    ok = step.get("exit_code") == 0
    head = f"RESULTADO ({kind}, exit_code={step.get('exit_code')}):"
    body = []
    if step.get("stdout"):
        body.append("stdout:\n" + step["stdout"].rstrip())
    if step.get("stderr"):
        body.append("stderr:\n" + step["stderr"].rstrip())
    if not body:
        body.append("(sem saida)")

    if not ok:
        advice = f"A ferramenta {kind} falhou. Corrija e chame a ferramenta de novo, ou explique a falha com reply."
    elif kind == "run" and not step.get("stdout") and not step.get("stderr"):
        advice = ('O codigo rodou mas NAO imprimiu nada. Adicione print(...)/console.log(...) e chame "run" '
                  "de novo. Nunca invente um valor que voce nao viu na saida.")
    else:
        advice = ('Agora responda ao usuario com {"action":"reply","text":"..."} usando SOMENTE o resultado '
                  "real acima (nao invente valores), no idioma do usuario.")
    return "\n".join([head, *body, advice])


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
