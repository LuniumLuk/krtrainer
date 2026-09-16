"""`python -m krcheat …` entry point (§10)."""

import sys

from krcheat.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
