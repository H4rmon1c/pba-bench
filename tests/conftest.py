import os
import sys

# Make the project root importable so tests can `import safety`, `import construction`, etc.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
