from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings


class BackupService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create(self) -> Path:
        timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%dT%H%M%S")
        target = self.settings.runtime_dir / "backups" / timestamp
        target.mkdir(parents=True, exist_ok=False)
        files = {}
        for name, source in (
            ("ai_quant_v2.sqlite3", self.settings.database_path),
            ("market.sqlite3", self.settings.resolved_market_database_path),
        ):
            destination = target / name
            _sqlite_backup(source, destination)
            files[name] = {
                "sha256": _sha256(destination),
                "size": destination.stat().st_size,
            }
        for source, prefix in (
            (self.settings.runtime_dir / "runs", Path("runtime") / "runs"),
            (self.settings.runtime_dir / "snapshots", Path("runtime") / "snapshots"),
            (self.settings.runtime_dir / "scheduler", Path("runtime") / "scheduler"),
        ):
            if not source.exists():
                continue
            for item in sorted(path for path in source.rglob("*") if path.is_file()):
                relative = prefix / item.relative_to(source)
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
                key = relative.as_posix()
                files[key] = {"sha256": _sha256(destination), "size": destination.stat().st_size}
        for name in ("system.json", "account.json"):
            source = self.settings.workspace / "config" / name
            if not source.exists():
                continue
            relative = Path("config") / name
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            files[relative.as_posix()] = {
                "sha256": _sha256(destination), "size": destination.stat().st_size,
            }
        manifest = {
            "version": "backup-manifest-v2",
            "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "files": files,
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @staticmethod
    def verify(directory: Path) -> dict[str, object]:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        findings = []
        for name, expected in manifest["files"].items():
            path = directory / name
            if not path.exists():
                findings.append(f"缺少文件: {name}")
                continue
            if _sha256(path) != expected["sha256"]:
                findings.append(f"哈希不一致: {name}")
                continue
            if path.suffix == ".sqlite3":
                with closing(sqlite3.connect(path)) as connection:
                    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    findings.append(f"SQLite 完整性失败 {name}: {result}")
        return {
            "directory": str(directory.resolve()),
            "valid": not findings,
            "findings": findings,
            "manifest_version": manifest.get("version"),
        }

    @staticmethod
    def verify_and_record(directory: Path) -> dict[str, object]:
        result = BackupService.verify(directory)
        receipt = {
            "version": "backup-verification-v1",
            "verified_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
                timespec="seconds"
            ),
            "manifest_sha256": _sha256(directory / "manifest.json"),
            **result,
        }
        (directory / "verification-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    @staticmethod
    def verification_status(directory: Path) -> dict[str, object]:
        receipt_path = directory / "verification-receipt.json"
        manifest_path = directory / "manifest.json"
        if not receipt_path.exists() or not manifest_path.exists():
            return {
                "directory": str(directory.resolve()), "valid": False,
                "findings": ["缺少完整校验回执"],
            }
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        findings = []
        if receipt.get("version") != "backup-verification-v1":
            findings.append("备份校验回执版本不受支持")
        if receipt.get("manifest_sha256") != _sha256(manifest_path):
            findings.append("备份清单在完整校验后发生变化")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, expected in manifest.get("files", {}).items():
            path = directory / name
            if not path.exists():
                findings.append(f"缺少文件: {name}")
            elif path.stat().st_size != expected["size"]:
                findings.append(f"文件大小变化: {name}")
        if not receipt.get("valid"):
            findings.extend(str(item) for item in receipt.get("findings", []))
        return {
            "directory": str(directory.resolve()),
            "valid": not findings,
            "findings": findings,
            "manifest_version": manifest.get("version"),
            "verified_at": receipt.get("verified_at"),
        }

    @staticmethod
    def restore(directory: Path, destination: Path) -> dict[str, object]:
        verification = BackupService.verify(directory)
        if not verification["valid"]:
            raise ValueError("备份校验失败，拒绝恢复")
        if destination.exists():
            raise FileExistsError(f"恢复目标已存在: {destination}")
        destination.mkdir(parents=True)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        restored = {}
        for name, expected in manifest["files"].items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"备份清单包含不安全路径: {name}")
            target_relative = (
                Path("runtime") / relative if len(relative.parts) == 1 else relative
            )
            source = directory / relative
            target = destination / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            restored[target_relative.as_posix()] = {
                "sha256": _sha256(target), "size": target.stat().st_size,
            }
            if restored[target_relative.as_posix()]["sha256"] != expected["sha256"]:
                raise ValueError(f"恢复后哈希不一致: {target_relative.as_posix()}")
            if target.suffix == ".sqlite3":
                with closing(sqlite3.connect(target)) as connection:
                    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise ValueError(
                        f"恢复后 SQLite 完整性失败 {target_relative.as_posix()}: {result}"
                    )
        report = {
            "version": "restore-report-v1",
            "source_backup": str(directory.resolve()),
            "destination": str(destination.resolve()),
            "restored_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "valid": True,
            "files": restored,
        }
        (destination / "restore-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report

    @staticmethod
    def verify_restore(destination: Path) -> dict[str, object]:
        report_path = destination / "restore-report.json"
        if not report_path.exists():
            return {
                "destination": str(destination.resolve()), "valid": False,
                "findings": ["缺少 restore-report.json"],
            }
        report = json.loads(report_path.read_text(encoding="utf-8"))
        findings = []
        for name, expected in report.get("files", {}).items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                findings.append(f"恢复报告包含不安全路径: {name}")
                continue
            path = destination / relative
            if not path.exists():
                findings.append(f"恢复文件缺失: {name}")
                continue
            if _sha256(path) != expected["sha256"]:
                findings.append(f"恢复文件哈希不一致: {name}")
                continue
            if path.suffix == ".sqlite3":
                with closing(sqlite3.connect(path)) as connection:
                    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    findings.append(f"恢复后 SQLite 完整性失败 {name}: {result}")
        return {
            "destination": str(destination.resolve()),
            "valid": not findings,
            "findings": findings,
            "restore_version": report.get("version"),
            "source_backup": report.get("source_backup"),
            "restored_at": report.get("restored_at"),
            "file_count": len(report.get("files", {})),
        }

    @staticmethod
    def restore_status(destination: Path) -> dict[str, object]:
        report_path = destination / "restore-report.json"
        if not report_path.exists():
            return {
                "destination": str(destination.resolve()), "valid": False,
                "findings": ["缺少 restore-report.json"],
            }
        report = json.loads(report_path.read_text(encoding="utf-8"))
        findings = []
        if report.get("version") != "restore-report-v1" or not report.get("valid"):
            findings.append("恢复演练报告无效")
        for name, expected in report.get("files", {}).items():
            path = destination / name
            if not path.exists():
                findings.append(f"恢复文件缺失: {name}")
            elif path.stat().st_size != expected["size"]:
                findings.append(f"恢复文件大小变化: {name}")
        return {
            "destination": str(destination.resolve()),
            "valid": not findings,
            "findings": findings,
            "restore_version": report.get("version"),
            "source_backup": report.get("source_backup"),
            "restored_at": report.get("restored_at"),
            "file_count": len(report.get("files", {})),
        }


def _sqlite_backup(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(source)
    with closing(sqlite3.connect(source)) as source_connection, closing(
        sqlite3.connect(destination)
    ) as target:
        source_connection.backup(target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
