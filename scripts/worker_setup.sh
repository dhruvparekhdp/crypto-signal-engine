#!/usr/bin/env bash
# Join the review cluster from a spare laptop (Linux or macOS). Run it like this:
#   curl -fsSL http://100.71.216.94:8765/setup.sh -o setup.sh && bash setup.sh laptop2
# Windows: install WSL (Ubuntu) first and run it inside that.
set -u
NAME="${1:-}"
[ -n "$NAME" ] || { echo "usage: bash setup.sh LAPTOP_NAME   (any short name, e.g. laptop2)"; exit 1; }
COORD_USER=dhruv
COORD=100.71.216.94
JOB=gate_main
DIR="$HOME/lab-worker"
say() { printf '\n== %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 - $2"; exit 1; }; }

say "1/7 checks"
need python3 "install Python 3 (sudo apt install python3 on Debian/Ubuntu)"
need curl "install curl"
need ssh "install the OpenSSH client"
OS="$(uname -s)"
if [ "$OS" = "Linux" ]; then RAM_GB=$(awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo); else RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 )); fi
echo "system: $OS, ${RAM_GB} GB RAM, python $(python3 --version 2>&1 | cut -d' ' -f2)"

say "2/7 reach the coordinator over Tailscale"
if ! (command -v nc >/dev/null && nc -z -w 5 "$COORD" 22) 2>/dev/null && ! curl -s -m 8 -o /dev/null "http://$COORD:8765/"; then
  echo "cannot reach $COORD. Install Tailscale on this laptop and sign in with the SAME account:"
  [ "$OS" = "Linux" ] && echo "  curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up" || echo "  install the Tailscale app from the Mac App Store, open it and sign in"
  echo "then run this script again."; exit 1
fi
echo "coordinator reachable"

say "3/7 Ollama"
if ! command -v ollama >/dev/null 2>&1; then
  if [ "$OS" = "Linux" ]; then echo "installing Ollama (asks for your sudo password)"; curl -fsSL https://ollama.com/install.sh | sh
  else echo "install Ollama from https://ollama.com/download , open it once, then run this script again"; exit 1; fi
fi
if ! curl -s -m 5 http://127.0.0.1:11434/api/tags >/dev/null; then
  echo "starting the Ollama server"; (nohup ollama serve >/tmp/ollama-serve.log 2>&1 &); sleep 6
fi
curl -s -m 5 http://127.0.0.1:11434/api/tags >/dev/null || { echo "Ollama is not answering on port 11434"; exit 1; }

say "4/7 models (about 5 GB each; both only if this laptop has 12+ GB RAM)"
MODELS="qwen3:8b"
[ "$RAM_GB" -ge 12 ] && MODELS="qwen3:8b deepseek-r1:8b"
[ "$RAM_GB" -lt 8 ] && { echo "only ${RAM_GB} GB RAM: 8B models will not fit. Tell Claude and a smaller model will be set up instead."; exit 1; }
for m in $MODELS; do ollama list | grep -q "^$m" && echo "have $m" || ollama pull "$m"; done

say "5/7 SSH access to the coordinator (you type its password once)"
[ -f "$HOME/.ssh/id_ed25519" ] || ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519" -C "worker-$NAME" >/dev/null
if ! ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$COORD_USER@$COORD" true 2>/dev/null; then
  ssh-copy-id -o StrictHostKeyChecking=accept-new -i "$HOME/.ssh/id_ed25519.pub" "$COORD_USER@$COORD" </dev/tty
fi
ssh -o BatchMode=yes "$COORD_USER@$COORD" "ls \$HOME/lab/work/$JOB/meta.json" >/dev/null || { echo "cannot read job $JOB on the coordinator"; exit 1; }

say "6/7 worker program"
mkdir -p "$DIR"
curl -fsSL "http://$COORD:8765/worker.py" -o "$DIR/work_worker.py"
echo "saved $DIR/work_worker.py"
echo "quick model test (should answer in under a minute)..."
curl -s -m 180 http://127.0.0.1:11434/api/generate -d '{"model":"qwen3:8b","prompt":"say ok","stream":false,"think":false,"options":{"num_predict":5}}' | python3 -c "import sys,json;print('model answered:',repr(json.load(sys.stdin)['response'][:20]))"

say "7/7 start"
cat > "$DIR/run.sh" <<RUN
#!/usr/bin/env bash
cd "$DIR"
while true; do
  python3 work_worker.py --name "$NAME" --job "$JOB" --coordinator "$COORD_USER@$COORD" && break
  echo "worker stopped, restarting in 30s"; sleep 30
done
RUN
chmod +x "$DIR/run.sh"
(nohup "$DIR/run.sh" > "$DIR/worker.log" 2>&1 &)
sleep 2
if [ "$OS" = "Darwin" ]; then (nohup caffeinate -dims -w "$(pgrep -f 'lab-worker/run.sh' | head -1)" >/dev/null 2>&1 &); fi
echo
echo "laptop '$NAME' is working. Watch it:   tail -f $DIR/worker.log"
echo "Or on your phone: http://$COORD:8765  (the laptop appears in the workers table within a few minutes)"
echo
echo "KEEP THIS LAPTOP: plugged in, lid open (or sleep disabled), and connected to Tailscale."
[ "$OS" = "Linux" ] && echo "Linux tip: gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing"
