import sys
from pathlib import Path

# Previously inserted repo root; now insert only backend to avoid polluting sys.path
sys.path.insert(0, str(Path(__file__).parent / "backend"))
