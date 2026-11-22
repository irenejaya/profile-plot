"""Build an installable plugin ZIP (Plugins > Manage and Install > Install from ZIP).

Usage:  python3 scripts/package.py [OUT_DIR]
"""

import configparser
import os
import sys
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PKG = os.path.join(ROOT, "profile_plot")


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    meta = configparser.ConfigParser()
    meta.read(os.path.join(PKG, "metadata.txt"), encoding="utf-8")
    version = meta["general"]["version"]
    target = os.path.join(out_dir, "profile_plot-{}.zip".format(version))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder, dirs, files in os.walk(PKG):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(folder, name)
                zf.write(full, os.path.relpath(full, ROOT))
        zf.write(os.path.join(ROOT, "LICENSE"), "profile_plot/LICENSE")
    print(target)


if __name__ == "__main__":
    main()
