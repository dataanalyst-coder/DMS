"""Backup and restore of the records AND the attachments (documents, photos).

    python -m app.backup create [folder]            # writes die_mgmt_backup_<time>.zip
    python -m app.backup verify <backup.zip>        # checks every file against its recorded SHA-256
    python -m app.backup restore <backup.zip> <folder> [--overwrite]

The database is copied with SQLite's online backup API, so it is consistent even while the system is running.
The archive carries a manifest of SHA-256 hashes; restore refuses a damaged archive and never overwrites an
existing database unless told to. Schedule `create` (for example with Windows Task Scheduler) and keep copies off
the server."""
import hashlib
import json
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .database import BASE_DIR, DB_PATH, STORAGE_DIR
from .services.errors import ValidationError

DEFAULT_DIR = BASE_DIR / "backups"


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(dest_dir: Path | str = DEFAULT_DIR, *, db_path: Path = DB_PATH, storage_dir: Path = STORAGE_DIR) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    out = dest_dir / f"die_mgmt_backup_{stamp}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "die_mgmt.db"
        src, dst = sqlite3.connect(db_path), sqlite3.connect(snap)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        files = {"die_mgmt.db": snap}
        if Path(storage_dir).exists():
            for p in sorted(Path(storage_dir).rglob("*")):
                if p.is_file():
                    files[f"storage/{p.relative_to(storage_dir).as_posix()}"] = p
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(),
                    "files": {n: {"sha256": _sha(p), "size": p.stat().st_size} for n, p in files.items()}}
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            for name, p in files.items():
                z.write(p, name)
    return out


def verify_backup(zip_path: Path | str) -> dict:
    """Raise ValidationError unless every file in the archive matches its recorded hash."""
    with zipfile.ZipFile(zip_path) as z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except KeyError:
            raise ValidationError("Not a backup of this system: manifest.json is missing")
        for name, meta in manifest["files"].items():
            if name != "die_mgmt.db" and not name.startswith("storage/") or ".." in name or name.startswith("/"):
                raise ValidationError(f"Unsafe file name in backup: {name}")
            if hashlib.sha256(z.read(name)).hexdigest() != meta["sha256"]:
                raise ValidationError(f"Backup is damaged: {name} does not match its recorded hash")
        if "die_mgmt.db" not in manifest["files"]:
            raise ValidationError("The backup has no database")
    return manifest


def restore_backup(zip_path: Path | str, target_dir: Path | str, *, overwrite: bool = False) -> dict:
    manifest = verify_backup(zip_path)
    target = Path(target_dir)
    if (target / "die_mgmt.db").exists() and not overwrite:
        raise ValidationError(f"{target / 'die_mgmt.db'} already exists; restore into an empty folder or pass --overwrite")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for name in manifest["files"]:
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(name))
    return {"db": str(target / "die_mgmt.db"), "storage": str(target / "storage"), "files": len(manifest["files"]),
            "created_at": manifest["created_at"]}


def main(argv: list[str]) -> int:
    try:
        if argv and argv[0] == "create":
            print(create_backup(argv[1] if len(argv) > 1 else DEFAULT_DIR))
        elif argv and argv[0] == "verify" and len(argv) > 1:
            m = verify_backup(argv[1])
            print(f"OK: {len(m['files'])} files, created {m['created_at']}")
        elif argv and argv[0] == "restore" and len(argv) > 2:
            print(restore_backup(argv[1], argv[2], overwrite="--overwrite" in argv))
        else:
            print(__doc__)
            return 2
    except ValidationError as e:
        print(f"Error: {e.message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
