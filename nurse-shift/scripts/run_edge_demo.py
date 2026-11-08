"""互換ラッパ: 本命は scripts/run_demo_edge.py。"""

from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).with_name("run_demo_edge.py")), run_name="__main__")
