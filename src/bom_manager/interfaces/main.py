"""Entry point: TUI by default, CLI with --cli flag."""

from __future__ import annotations

import sys


def main() -> None:
    """
    Default: launch TUI.
    With --cli flag: delegate to the original Click CLI.
    """
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from bom_manager.interfaces.cli import cli
        cli()
    else:
        from bom_manager.interfaces.tui import BOMManagerApp
        BOMManagerApp().run()


if __name__ == "__main__":
    main()
