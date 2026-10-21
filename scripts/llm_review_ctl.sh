#!/bin/bash
# Pause / resume / status for the LLM review run (called by the Mac dashboard over ssh).
cd ~/lab/llm_review || exit 1
running() { pgrep -f "python3 llm_review_v[2].py run" >/dev/null; }
case "$1" in
  pause)  touch out_v2/PAUSE
          # not running (e.g. after a reboot): unload any loaded model now; a running one unloads after its signal
          running || python3 llm_review_v2.py unload
          echo pausing ;;
  resume) rm -f out_v2/PAUSE
          if running; then echo already-running
          else setsid -f nohup python3 llm_review_v2.py run prompts_v2.jsonl out_v2 >> run_v2.log 2>&1 < /dev/null; echo started; fi ;;
  status) loaded=$(curl -s -m 5 http://127.0.0.1:11434/api/ps | python3 -c "import json,sys; print(','.join(m['name'] for m in json.load(sys.stdin).get('models', [])) or 'none')" 2>/dev/null)
          if running && [ -f out_v2/PAUSE ]; then echo "pausing $loaded"
          elif running; then echo "running $loaded"
          elif [ -f out_v2/PAUSE ]; then echo "paused $loaded"
          else echo "stopped $loaded"; fi ;;
esac
