"""Strict action-specific CLI arguments, checked before configuration is opened."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID


@dataclass
class AccountCopyCommand:
    """Copy operator-selected accounts without issuing or activating subscription links."""

    action: Literal["export", "authorize", "preview", "apply", "close"]
    snapshot: Path | None = None
    report: Path | None = None
    approved_report: Path | None = None
    auth_file: Path | None = None
    proxy_config: Path | None = None
    namespace: str | None = None
    source_identity: str | None = None
    sequence: int | None = None

    def options(self) -> dict:
        supplied = {
            key: value
            for key, value in vars(self).items()
            if key != "action" and value is not None
        }
        required = {
            "export": {
                "snapshot",
                "auth_file",
                "proxy_config",
                "namespace",
                "source_identity",
                "sequence",
            },
            "authorize": {"namespace", "source_identity"},
            "close": {"namespace", "source_identity"},
            "preview": {"snapshot", "report"},
            "apply": {"snapshot", "approved_report"},
        }
        if set(supplied) != required.get(self.action) or (
            self.sequence is not None
            and (type(self.sequence) is not int or not 1 <= self.sequence <= 2**63 - 1)
        ):
            raise ValueError("invalid_account_copy_arguments")
        for key in ("namespace", "source_identity"):
            if key in supplied:
                parsed = UUID(supplied[key])
                if str(parsed) != supplied[key]:
                    raise ValueError("invalid_account_copy_arguments")
                supplied[key] = parsed
        return supplied
