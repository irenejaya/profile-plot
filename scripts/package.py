"""Build the installable plugin zip:  python scripts/package.py [OUT_DIR]

Creates dist/profile_plot-<version>.zip containing a top-level
'profile_plot' folder, ready for Plugins > Manage and Install Plugins >
Install from ZIP.  Tests, docs and development files are left out.
"""

import configparser
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "profile_plot"
INCLUDE = ("__init__.py", "plugin.py", "metadata.txt", "icon.svg", "LICENSE",
           "README.md", "core", "ui", "icons")


def version():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(os.path.join(ROOT, "metadata.txt"), encoding="utf-8")
    return cfg["general"]["version"]


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, "{}-{}.zip".format(PACKAGE, version()))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in INCLUDE:
            path = os.path.join(ROOT, item)
            if os.path.isfile(path):
                zf.write(path, "{}/{}".format(PACKAGE, item))
                continue
            for folder, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for name in sorted(files):
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    full = os.path.join(folder, name)
                    rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
                    zf.write(full, "{}/{}".format(PACKAGE, rel))
    print(target)


if __name__ == "__main__":
    main()
