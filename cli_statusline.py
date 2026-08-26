import json
import sys

payload = json.load(sys.stdin)
model = (payload.get("model") or {}).get("display_name") or "?"
cw = payload.get("context_window") or {}
inp = cw.get("total_input_tokens")
out = cw.get("total_output_tokens")
pct = cw.get("used_percentage")
inp_s = inp if inp is not None else "?"
out_s = out if out is not None else "?"
pct_s = f"{float(pct):.0f}%" if pct is not None else "?%"
sys.stdout.write(f"{model}  in {inp_s}  out {out_s}  ctx {pct_s}\n")
