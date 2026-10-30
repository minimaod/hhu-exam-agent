"""进程内查题服务的离线 CLI；只用于自检/评估，不注册 HTTP 路由。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.question_bank.config import COURSE, DEFAULT_BANK_ROOT
from app.question_bank.models import SearchRequest
from app.question_bank.service import QuestionBankService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="题目库查题 CLI")
    parser.add_argument("--bank-root", default=str(DEFAULT_BANK_ROOT))
    parser.add_argument("--course", default=COURSE)
    parser.add_argument("--query", default="")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--type", dest="question_type", choices=("choice", "fill", "code"))
    parser.add_argument("--exam", dest="exam_type", choices=("midterm", "final"))
    parser.add_argument("--paper")
    parser.add_argument("--kp", action="append", default=[])
    parser.add_argument("--mode", choices=("auto", "keyword_only", "vector_only"), default="auto")
    parser.add_argument("--preview", action="store_true", help="显式预览未审核候选")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--detail")
    parser.add_argument("--include-answer", action="store_true")
    parser.add_argument("--include-code", action="store_true")
    args = parser.parse_args(argv)

    try:
        with QuestionBankService(Path(args.bank_root)) as service:
            if args.detail:
                detail = service.detail(
                    args.detail,
                    include_answer=args.include_answer,
                    include_reference_code=args.include_code,
                    include_code_blocks=args.include_code,
                    include_annotations=args.include_code,
                )
                if detail is None:
                    print("未找到该题", file=sys.stderr)
                    return 1
                print(detail.model_dump_json(indent=2))
                return 0
            request = SearchRequest(
                course=args.course,
                query=args.query,
                limit=args.limit,
                question_type=args.question_type,
                exam_type=args.exam_type,
                paper_id=args.paper,
                knowledge_points=args.kp,
                preview_candidates=args.preview,
            )
            response = service.search(
                request,
                force_mode=None if args.mode == "auto" else args.mode,
            )
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.json:
        print(response.model_dump_json(indent=2))
        return 0
    print(
        f"status={response.status} mode={response.mode} degraded={response.degraded} "
        f"matched_total={response.matched_total} returned={response.returned}"
    )
    if response.message:
        print(response.message)
    for item in response.items:
        print(
            f"[{item.rank}] {item.question_id} {item.question_type} "
            f"{item.paper.academic_year} {item.paper.exam_type} 第{item.number_label}题 "
            f"({item.matched_by})"
        )
        print("   " + item.statement.replace("\n", " ")[:160])
        if item.options:
            print("   " + " / ".join(f"{opt.label}.{opt.text}" for opt in item.options))
        print(
            f"   答案:{item.answer.state} 审核:{item.review.status} "
            f"知识点:{','.join(point.name for point in item.knowledge_points) or '无'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())