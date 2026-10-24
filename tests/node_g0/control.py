"""Private fixture management, including SSH stdin registration."""

import argparse
import json
import os
import re
import sys
from uuid import UUID


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["bootstrap", "register", "publish", "serve", "report"]
    )
    args = parser.parse_args()
    os.environ["DJANGO_SETTINGS_MODULE"] = "tests.node_g0.settings"
    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.db import transaction
    from .models import Node, Usage
    from .urls import canonical

    root = settings.RUN_DIR
    if args.action == "bootstrap":
        call_command("migrate", run_syncdb=True, verbosity=0)
    elif args.action == "register":
        value = json.loads(sys.stdin.buffer.read(65537))
        if set(value) != {"node_id", "source_id", "token_digest"} or not re.fullmatch(
            r"[a-f0-9]{64}", value["token_digest"]
        ):
            raise ValueError("invalid registration")
        node_id, source_id = UUID(value["node_id"]), UUID(value["source_id"])
        with transaction.atomic():
            current = Node.objects.filter(pk=node_id).first()
            if current is None:
                bundle = json.loads((root / "bundle.json").read_text())
                bundle["node_id"] = str(node_id)
                Node.objects.create(
                    id=node_id,
                    source_id=source_id,
                    token_digest=value["token_digest"],
                    bundle=bundle,
                )
            elif (
                current.source_id != source_id
                or current.token_digest != value["token_digest"]
            ):
                raise ValueError("identity conflict")
        print(json.dumps({"registered": str(node_id)}))
    elif args.action == "publish":
        with transaction.atomic():
            node = Node.objects.select_for_update().get()
            bundle = json.loads((root / "bundle-next.json").read_text())
            if bundle["generation"] != node.generation + 1:
                raise ValueError("generation conflict")
            bundle["node_id"] = str(node.pk)
            node.bundle, node.generation = bundle, bundle["generation"]
            node.save(update_fields=["bundle", "generation"])
    elif args.action == "report":
        print(
            json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": str(n.pk),
                            "generation": n.generation,
                            "applied_generation": n.applied_generation,
                            "invocation": n.invocation,
                        }
                        for n in Node.objects.all()
                    ],
                    "usage": [
                        {
                            "credential_id": str(u.credential_id),
                            "usage_date": str(u.usage_date),
                            "version": u.version,
                            "upload": u.upload,
                            "download": u.download,
                            "digest": u.digest,
                        }
                        for u in Usage.objects.all()
                    ],
                }
            )
        )
    else:
        config = json.loads((root / "control.json").read_text())
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-m",
                "gunicorn",
                "--workers",
                "2",
                "--bind",
                f"127.0.0.1:{config['https_port']}",
                "--certfile",
                str(root / "control.crt"),
                "--keyfile",
                str(root / "control.key"),
                "tests.node_g0.wsgi:application",
            ],
        )


if __name__ == "__main__":
    main()
