raise SystemExit("fixture: scanned as text, never executed")

import os
import shutil

shutil.rmtree(os.path.expanduser("~/example-workspace"))
