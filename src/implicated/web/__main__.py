"""Run the web app: ``python -m implicated.web [--host H] [--port P] [--reload]``."""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m implicated.web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run(
        "implicated.web:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
