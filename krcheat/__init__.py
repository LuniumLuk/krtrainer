"""krcheat — a command-line trainer and save editor for *Kingdom Rush* on macOS.

This package is the implementation of `KRCHEAT_FOUNDATION.md`. The document is the
authoritative specification; where this code and the document disagree, the document
is right and this code is a bug.

Layering (§9.5, D7):

    core/     operations, the safety model, schema knowledge, the codec, transports
    cli.py    argument parsing, `Result` -> text/--json rendering, exit codes
    gui/      tkinter widgets over the same `core/` functions

`core/` must never import `argparse`, print to stdout, or call `sys.exit`. Both
front-ends are renderers over one implementation, so they cannot diverge.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
