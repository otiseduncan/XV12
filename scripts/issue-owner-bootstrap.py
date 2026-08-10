from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.enrollment import EnrollmentStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue a one-time verified-Google Owner bootstrap URL.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expires-minutes", type=int, default=10)
    args = parser.parse_args()

    settings = Settings.load()
    if not settings.tailscale_serve_origin.startswith("https://"):
        raise RuntimeError("XV12_TAILSCALE_SERVE_ORIGIN must be the private HTTPS origin")
    store = EnrollmentStore(settings.database_path, settings.owner_google_sub, settings)
    store.initialize()
    _, token = store.issue_owner_bootstrap(args.expires_minutes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        f"{settings.tailscale_serve_origin}/owner-bootstrap/{token}",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
