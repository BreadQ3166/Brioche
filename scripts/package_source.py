"""Export reviewable source, excluding secrets, caches and captured live desktops."""
from pathlib import Path
import zipfile
import runpy

root = Path(__file__).resolve().parents[1]
version = runpy.run_path(str(root / "src/brioche/__init__.py"))["__version__"]
release = root / f"releases/brioche-{version}-source.zip"
release.parent.mkdir(exist_ok=True)
files = [root / name for name in ("pyproject.toml", ".gitignore", "README.md", "DESIGN.md",
                                "THIRD_PARTY.md", "ui-concept.svg")]
for directory in ("src/brioche", "scripts", "tests", "assets", "network-helper", "docs", ".github"):
    files += [p for p in (root / directory).rglob("*") if p.is_file()
              and "__pycache__" not in p.parts and p.suffix in (".py", ".sh", ".ps1", ".svg", ".html", ".js", ".css", ".go", ".mod", ".sum", ".md", ".yml", ".yaml")]
files += [root / "artifacts" / name for name in ("VALIDATION.md", "tests.txt", "gui-smoke.txt",
                                                "bundle-smoke.txt", "real-displays-02.json", "network-02.json",
                                                "browser-smoke-04.json", "browser-performance-04.json", "helper-tests-05.txt", "embedded-login-05.json")]
with zipfile.ZipFile(release, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(set(files)):
        if not path.is_file():
            continue
        archive.write(path, Path("Brioche") / path.relative_to(root))
print(release)
