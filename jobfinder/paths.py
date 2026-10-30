"""Separate installed code/resources from the caller's writable workspace."""
import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path(os.environ.get('JOBFINDER_WORKSPACE', SOURCE_ROOT)).expanduser().resolve()
