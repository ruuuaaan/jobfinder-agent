"""Launch with the explicitly selected Python or project virtual environment."""
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
if sys.prefix == sys.base_prefix and (ROOT/'.webdeps').exists():
    sys.path.insert(0,str(ROOT/'.webdeps'))
os.environ.setdefault('PYTHONUTF8','1')
if __name__=='__main__':
    from jobfinder_web.__main__ import main
    main()
