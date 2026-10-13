#!/usr/bin/env bash
# terminalIA - inicializa llama.cpp + llama-server + site web.
# Uso:  bash scripts/start.sh
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${WORK_DIR:-$(dirname "$ROOT")}"          # diretorio de trabalho (fora do repo)
LLAMA_DIR="${LLAMA_DIR:-$WORK/llama.cpp}"
MODELS_DIR="${MODELS_DIR:-$WORK/models}"
# Quantizacao do modelo. Q4_K_M e o padrao: o Q2_K e menor, mas degrada demais o
# raciocinio e a "skill" de codigo do agente. Troque com MODEL_QUANT=Q2_K etc.
MODEL_QUANT="${MODEL_QUANT:-Q4_K_M}"
MODEL_FILE="${MODEL_FILE:-$MODELS_DIR/model-$MODEL_QUANT.gguf}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/lemuralabs/Llama-3.2-3B-Uncensored-GGUF/resolve/main/model-$MODEL_QUANT.gguf}"

PORT="${PORT:-12000}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
LLAMA_THREADS="${LLAMA_THREADS:-$(nproc)}"
LLAMA_CTX="${LLAMA_CTX:-4096}"

log(){ printf '\033[1;32m[terminalIA]\033[0m %s\n' "$*"; }

# --- 1. cmake ---------------------------------------------------------------
if ! command -v cmake >/dev/null 2>&1; then
  log "instalando cmake..."
  pip install --quiet cmake || { apt-get update -y && apt-get install -y cmake; }
fi
export PATH="$HOME/.local/bin:$PATH"

# --- 2. llama.cpp -----------------------------------------------------------
if [ ! -x "$LLAMA_DIR/build/bin/llama-server" ]; then
  log "clonando e compilando llama.cpp..."
  [ -d "$LLAMA_DIR" ] || git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DGGML_NATIVE=ON -DCMAKE_BUILD_TYPE=Release
  cmake --build "$LLAMA_DIR/build" --config Release -j"$LLAMA_THREADS" --target llama-server
fi

# --- 3. modelo --------------------------------------------------------------
mkdir -p "$MODELS_DIR"
if [ ! -f "$MODEL_FILE" ]; then
  log "baixando modelo Q2_K (~1.3GB)..."
  wget -q --show-progress -O "$MODEL_FILE" "$MODEL_URL"
fi

# --- 4. llama-server --------------------------------------------------------
if curl -s --max-time 3 "http://127.0.0.1:$LLAMA_PORT/health" | grep -q '"status":"ok"'; then
  log "llama-server ja esta no ar na porta $LLAMA_PORT"
else
  log "iniciando llama-server (porta $LLAMA_PORT, $LLAMA_THREADS threads, ctx $LLAMA_CTX)..."
  nohup "$LLAMA_DIR/build/bin/llama-server" \
    -m "$MODEL_FILE" --host 127.0.0.1 --port "$LLAMA_PORT" \
    -c "$LLAMA_CTX" -t "$LLAMA_THREADS" -ngl 0 --no-webui \
    > /tmp/llama_server.log 2>&1 &
  for _ in $(seq 1 60); do
    curl -s --max-time 2 "http://127.0.0.1:$LLAMA_PORT/health" | grep -q '"status":"ok"' && break
    sleep 2
  done
fi

# --- 5. site ----------------------------------------------------------------
log "iniciando site na porta $PORT..."
cd "$ROOT"
LLAMA_URL="http://127.0.0.1:$LLAMA_PORT" PORT="$PORT" \
  nohup python3 server.py > /tmp/web_server.log 2>&1 &
sleep 3

log "pronto. site em http://localhost:$PORT  (usuario: ${BASIC_USER:-admin})"
