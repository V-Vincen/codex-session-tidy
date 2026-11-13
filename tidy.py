#!/usr/bin/env python3
"""本批已确认迁移的简短入口；不自动选择或执行其他计划。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
from migrate_confirmed import main

if __name__ == '__main__':
    main()
