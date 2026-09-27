#!/usr/bin/env python3
"""
Drone IDS - Direct Runner
Run directly from project directory without installation.
"""
import sys
from pathlib import Path

# Add src to path so drone_ids package is found
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from drone_ids.scripts.run_ids import main

if __name__ == '__main__':
    main()