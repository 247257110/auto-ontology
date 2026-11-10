"""Ask the running application a question and dump what it did.

Written because every question costs minutes and the interesting part is *not*
the prose answer: it is which tables the retriever picked, which SQL the model
wrote, and whether the numbers can be reproduced. The output is a plain text
transcript so two runs can be diffed after a semantics change.

::

    python scripts/ask_app.py "2024 年订单金额最高的 5 个行业分别是多少？"
    python scripts/ask_app.py -o /tmp/q2.txt "哪些客户最可能流失？"
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import requests

STREAM = "http://localhost:3001/api/chat/completions"


def ask(question: str, out_path: str, timeout: int, target_db: str | None) -> int:
    started = time.time()
    lines: list[str] = []

    def emit(text: str) -> None:
        print(text, flush=True)
        lines.append(text)
        with open(out_path, "w", encoding="utf8") as handle:
            handle.write("\n".join(lines) + "\n")

    emit(f"Q: {question}")
    emit(f"target_db: {target_db or '(all databases)'}")
    emit(f"started {time.strftime('%H:%M:%S')}")
    emit("-" * 78)

    payload: dict[str, object] = {"question": question}
    if target_db:
        payload["target_db"] = target_db
    response = requests.post(STREAM, json=payload, stream=True, timeout=timeout)
    emit(f"HTTP {response.status_code}")
    if response.status_code != 200:
        emit(response.text[:2000])
        return 1

    def finish() -> int:
        emit("-" * 78)
        emit(f"elapsed {time.time() - started:.0f}s")
        return 0

    for raw in response.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue

        kind = event.get("type")
        if kind == "step":
            emit(f"  · {event.get('node')} {event.get('phase')}")
        elif kind == "sql":
            sql = event.get("sql") or event.get("query") or ""
            emit("SQL> " + " ".join(sql.split())[:1200])
        elif kind == "result":
            answer = event.get("answer") or {}
            emit("\nANSWER:\n" + str(answer.get("response")))
            emit("\nSQL CODE:\n" + str(answer.get("sql_code")))
            rows = answer.get("sql_response_from_db")
            emit("\nROWS:\n" + (json.dumps(rows, ensure_ascii=False, indent=2) if rows else "None"))
        elif kind == "error":
            emit("ERROR: " + str(event.get("message")))

    return finish()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("-o", "--output", default="/tmp/answer.txt")
    parser.add_argument(
        "--target-db",
        default=None,
        help=(
            "scope the question to one connected database (name or catalog UUID). "
            "Without it every connected database competes in the same vector "
            "search, and the one with the densest semantic layer usually wins "
            "even when it cannot answer the question."
        ),
    )
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    return ask(args.question, args.output, args.timeout, args.target_db)


if __name__ == "__main__":
    sys.exit(main())
