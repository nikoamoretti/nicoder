"""Make the fixture root importable without installing anything."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
