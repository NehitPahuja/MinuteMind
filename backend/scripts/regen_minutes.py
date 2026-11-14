"""Write minutes again for the newest meeting in MINUTEMIND_DATA_DIR, N times (default 1), to
compare prompt changes. Usage: .venv/Scripts/python.exe scripts/regen_minutes.py [N]"""

import json, sys
from minutemind import gpu_env
gpu_env.prepare()
from minutemind import hardware, minutes, models, store
from minutemind.llm.llamacpp import LlamaCppModel
db = store.Store(); mid = db.list_meetings()[0].id
info = hardware.detect(); prof = hardware.choose_profile(info, "auto")
w = minutes.MinutesWriter(LlamaCppModel(models.ModelManager()))
ctx = minutes.build_context(db, mid)
for i in range(int(sys.argv[1]) if len(sys.argv) > 1 else 1):
    print(json.dumps(w.write(ctx, prof), indent=1)); print("=====")
