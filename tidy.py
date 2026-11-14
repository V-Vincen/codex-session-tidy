#!/usr/bin/env python3
"""会话整理短入口；run 编排自动流程，其余动作兼容原接口。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
from migrate_confirmed import main

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'run':
        from auto_run import main as auto_main
        auto_main(sys.argv[2:])
    else:
        if len(sys.argv) == 2 and sys.argv[1] in ('-h', '--help'):
            print('自动流程：tidy.py run --help；原有动作说明如下。\n')
        main()
