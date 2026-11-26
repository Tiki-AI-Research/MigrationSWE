#!/usr/bin/env python3
import pathlib
import runpy
import sys

probe = pathlib.Path(__file__).with_name("p2p_probe.py")
sys.exit(runpy.run_path(str(probe), run_name="__main__"))
