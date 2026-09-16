"""Operations layer.

Everything that *does* something lives here. Front-ends render; they never decide.
See §9.5 of the foundation document.

Import rule: this package depends on the standard library only (§13). No `argparse`,
no printing, no `sys.exit` — errors travel as `KrcheatError` with an exit code
attached (§10.5), and results travel as `Result`.
"""
