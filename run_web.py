"""
Run the web interface for the pipeline.

Usage:
    python run_web.py [--host HOST] [--port PORT]
    
Examples:
    python run_web.py
    python run_web.py --port 8080
    python run_web.py --host 0.0.0.0 --port 8000
"""

import argparse
from src.__version__ import __version__


def main():
    # Load config to get default port
    try:
        from src.shared.config.config_loader import get_config
        config = get_config()
        default_port = config.web.port
    except Exception:
        default_port = 8000
    
    parser = argparse.ArgumentParser(description="Run the pipeline web interface")
    parser.add_argument(
        "--version", "-v", action="version",
        version=f"genie-x {__version__}",
        help="Show version and exit"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=default_port, help=f"Port to bind to (default: {default_port})")
    args = parser.parse_args()
    
    # Import here to allow for cleaner error messages if deps are missing
    try:
        from src.web.server_runner import run_web_server
    except ImportError as error:
        if error.name != "uvicorn":
            raise
        print("Error: uvicorn not installed. Run: uv sync")
        return 1
    
    # Show database location before starting server
    try:
        from src.web.shared_state import get_run_dir, get_db_path
        run_dir = get_run_dir()
        db_path = get_db_path()
        print(f"🔬 Copilot Chat Extractor starting at http://{args.host}:{args.port}")
        print(f"   → System Overview: http://{args.host}:{args.port}/")
        print(f"   → Browse Chats:    http://{args.host}:{args.port}/browse")
        print(f"   → API Docs:        http://{args.host}:{args.port}/docs")
        print()
        print(f"📁 Run Directory: {run_dir}")
        print(f"💾 Database:      {db_path} {'✓ exists' if db_path.exists() else '✗ not found'}")
        print()
    except Exception as e:
        print(f"⚠️  Warning: Could not load database path: {e}")
        print(f"🔬 Copilot Chat Extractor starting at http://{args.host}:{args.port}")
        print()
    
    return run_web_server("src.web.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
