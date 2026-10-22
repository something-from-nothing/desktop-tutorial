"""Entry point for Desk Agent. PyInstaller builds the EXE from this file."""

import logging

from deskagent import config


def main():
    log_path = config.app_dir() / "deskagent.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )
    # MCP servers write their own logs here too; a windowed EXE has no console to show them.
    errlog = open(config.app_dir() / "mcp_servers.log", "a", encoding="utf-8", buffering=1)

    from deskagent.gui import App

    App(errlog).run()


if __name__ == "__main__":
    main()
