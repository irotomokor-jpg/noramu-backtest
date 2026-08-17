from __future__ import annotations

"""SOR V1.0 frozen live executor.

Implementation is split into ordered source parts only to keep the live module
reviewable and maintainable in the repository. Running this file executes the
parts in one shared module namespace; part6 contains main().
"""

from pathlib import Path

_here = Path(__file__).resolve().parent
for _name in (
    "sor_toss_live_v100_part1.py",
    "sor_toss_live_v100_part2.py",
    "sor_toss_live_v100_part3.py",
    "sor_toss_live_v100_part4.py",
    "sor_toss_live_v100_part5.py",
    "sor_toss_live_v100_part6.py",
):
    _path = _here / _name
    exec(compile(_path.read_text(encoding="utf-8"), str(_path), "exec"), globals(), globals())
