"""Index the meetings in MINUTEMIND_DATA_DIR with the real embedding model and
ask questions with the real language model, printing answers and citations.

Usage, from backend/ (after run_pipeline.py has built the test meeting):
    MINUTEMIND_DATA_DIR=<scratch folder> .venv/Scripts/python.exe scripts/ask_check.py ["question" ...]
"""

import sys
import time

from minutemind import gpu_env

gpu_env.prepare()

from minutemind import hardware, index, models, store  # noqa: E402
from minutemind.llm.llamacpp import LlamaCppModel  # noqa: E402
from minutemind.minutes import clock  # noqa: E402
from minutemind.qa import Asker  # noqa: E402

QUESTIONS = [
    "How many testers get the beta invite?",
    "Who is finishing the onboarding videos, and by when?",
    "When does the mobile app launch?",
    "How big is the ads budget?",
    "What happened to dark mode?",
    "What is the capital of France?",
    "What did we decide about the pricing page?",
]


def main() -> None:
    db = store.Store()
    indexer = index.Indexer(db, index.OnnxEmbedder())
    started = time.time()
    for meeting_id in db.meetings_to_index(indexer.version):
        indexer.index_meeting(meeting_id)
        print(f"indexed meeting {meeting_id}: {db.count_chunks(meeting_id)} passages")
    print(f"indexing took {time.time() - started:.1f} s\n")

    info = hardware.detect()
    asker = Asker(
        db, indexer, LlamaCppModel(models.ModelManager()),
        lambda: hardware.choose_profile(info, "auto"),
    )
    for question in sys.argv[1:] or QUESTIONS:
        started = time.time()
        first_token = None
        done = None
        for event in asker.ask(question):
            if event["type"] == "token" and first_token is None:
                first_token = time.time() - started
            if event["type"] in ("done", "error"):
                done = event
        print(f"Q: {question}")
        if done["type"] == "error":
            print(f"   ERROR {done['message']}\n")
            continue
        print(f"A: {done['answer']}")
        for s in done["sources"]:
            if s["n"] in done["cited"]:
                at = "no line" if s["at_ms"] is None else clock(s["at_ms"])
                print(f"   [{s['n']}] {s['meeting_title']}, {s['kind']} passage, opens at {at}")
        ft = f"{first_token:.1f}" if first_token is not None else "-"
        print(f"   ({len(done['sources'])} sources, first token {ft} s, total {time.time() - started:.1f} s)\n")


if __name__ == "__main__":
    main()
