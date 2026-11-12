#!/usr/bin/env python3
"""Run the suite with the spawn start method forced, so spawn-safety claims
(DeepSeek 0038 R1 / Instinct 0048 D) are exercised the way a fresh machine's
default (spawn, Python 3.14+) runs. Fork default: python3 test_stage0.py.
The exit code follows the FAIL list: suite() itself returns None."""
import multiprocessing as mp, sys

if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    import test_stage0
    test_stage0.suite()
    print(f"RESULT: {len(test_stage0.PASS)} passed, "
          f"{len(test_stage0.XFAIL)} expected-red, {len(test_stage0.FAIL)} failed")
    sys.exit(1 if test_stage0.FAIL else 0)
