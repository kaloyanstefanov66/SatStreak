"""Allow ``python -m satstreak``."""

import sys

from satstreak.cli import main

if __name__ == "__main__":
    sys.exit(main())
