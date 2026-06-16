import argparse

import uvicorn

from sim import config


def main():
    parser = argparse.ArgumentParser(description="Reticulum network simulator")
    parser.add_argument("--host", default=config.HTTP_HOST)
    parser.add_argument("--port", type=int, default=config.HTTP_PORT)
    parser.add_argument("--hub-host", default=config.HUB_HOST)
    parser.add_argument("--hub-port", type=int, default=config.HUB_PORT)
    args = parser.parse_args()

    config.HTTP_HOST = args.host
    config.HTTP_PORT = args.port
    config.HUB_HOST = args.hub_host
    config.HUB_PORT = args.hub_port

    uvicorn.run("sim.server:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
