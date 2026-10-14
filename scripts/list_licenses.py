"""List every installed distribution with its declared license (input for NOTICE.md).

Standard library only. Run it in the environment built from the requirements files:

  python scripts/list_licenses.py
"""

from __future__ import annotations

from importlib.metadata import PackageMetadata, distributions


def declared_license(meta: PackageMetadata) -> str:
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [
        value.rsplit("::", 1)[-1].strip()
        for value in meta.get_all("Classifier") or []
        if value.startswith("License ::")
    ]
    if classifiers:
        return "; ".join(classifiers)
    text = (meta.get("License") or "").strip()
    return text.splitlines()[0][:80] if text else "UNKNOWN"


def main() -> None:
    rows = {
        (dist.metadata["Name"], dist.version, declared_license(dist.metadata))
        for dist in distributions()
        if dist.metadata["Name"]
    }
    for name, version, license_name in sorted(rows, key=lambda row: row[0].lower()):
        print(f"{name}\t{version}\t{license_name}")


if __name__ == "__main__":
    main()
