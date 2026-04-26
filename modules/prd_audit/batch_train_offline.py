# -*- coding: utf-8 -*-
import argparse
import glob
import os
from typing import List


def _read_text(path: str, encoding: str) -> str:
    with open(path, "r", encoding=encoding) as f:
        return f.read()


def _iter_files(input_dir: str, pattern: str) -> List[str]:
    p = os.path.join(os.path.abspath(input_dir), pattern)
    files = glob.glob(p, recursive=True)
    files = [x for x in files if os.path.isfile(x)]
    files.sort()
    return files


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="PRD 审计离线批量训练：不调用大模型，写入学习快照与看板数据。")
    parser.add_argument("--input-dir", required=True, help="PRD 文本目录（包含 .txt/.md 等）")
    parser.add_argument("--glob", default="**/*.txt", help="文件匹配模式（默认：**/*.txt）")
    parser.add_argument("--encoding", default="utf-8", help="文本编码（默认：utf-8）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理文件数（0 表示不限制）")
    parser.add_argument("--timeout", type=int, default=90, help="单个 PRD 超时秒数（默认：90）")
    parser.add_argument("--out-dir", default="", help="可选：输出报告目录（写入 L1/L2/L3 markdown）")
    args = parser.parse_args(argv)

    from . import pipeline

    llm_disabled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__llm_disabled__.json")
    out_dir = os.path.abspath(args.out_dir) if args.out_dir else ""
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    files = _iter_files(args.input_dir, args.glob)
    if args.limit and args.limit > 0:
        files = files[: args.limit]
    if not files:
        print("No files matched.")
        return 1

    ok = 0
    failed = 0
    for i, path in enumerate(files, start=1):
        try:
            text = _read_text(path, args.encoding)
            text = (text or "").strip()
            if not text:
                print(f"[SKIP] empty: {path}")
                continue
            report_l3, stage1, stage2, stage3 = pipeline.run_prd_audit_sync(
                prd_text=text,
                llm_config_path=llm_disabled,
                timeout=int(args.timeout or 90),
            )
            if out_dir:
                base = os.path.splitext(os.path.basename(path))[0]
                l1 = pipeline._build_l1_local_report(stage3)
                l2 = pipeline._build_l2_local_report(stage1, stage3, prd_content=text)
                with open(os.path.join(out_dir, f"{base}.L3.md"), "w", encoding="utf-8") as f:
                    f.write(report_l3 or "")
                with open(os.path.join(out_dir, f"{base}.L2.md"), "w", encoding="utf-8") as f:
                    f.write(l2 or "")
                with open(os.path.join(out_dir, f"{base}.L1.md"), "w", encoding="utf-8") as f:
                    f.write(l1 or "")
            ok += 1
            print(f"[OK] ({i}/{len(files)}) {path}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] ({i}/{len(files)}) {path}: {e}")
    print(f"Done. ok={ok}, failed={failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
