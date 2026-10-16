"""Read project metadata without loading credential values."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectAnalysis:
    root: Path
    name: str
    framework: str
    runtime: str
    package_manager: str | None
    build_command: tuple[str, ...] | None
    test_commands: tuple[tuple[str, ...], ...]
    start_command: tuple[str, ...] | None
    output_dir: str | None
    static: bool
    api_routes: bool
    database_dependencies: tuple[str, ...]
    env_names: tuple[str, ...]
    hosting_configs: tuple[str, ...]
    services: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["root"] = str(self.root)
        return result


_FRAMEWORKS = (
    ("next", "Next.js", False, ".next"),
    ("nuxt", "Nuxt", False, ".output"),
    ("@sveltejs/kit", "SvelteKit", False, "build"),
    ("@remix-run/react", "Remix", False, "build"),
    ("react-router", "React Router", False, "build"),
    ("@angular/core", "Angular", True, "dist"),
    ("astro", "Astro", True, "dist"),
    ("vite", "Vite", True, "dist"),
    ("vue", "Vue", True, "dist"),
    ("react", "React", True, "build"),
    ("@nestjs/core", "NestJS", False, "dist"),
    ("fastify", "Fastify", False, None),
    ("express", "Express", False, None),
)


def _read_json(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _read_small(path: Path) -> str:
    try:
        if path.is_file() and path.stat().st_size < 262144:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def _npm_command(manager: str, script: str) -> tuple[str, ...]:
    return (manager, script) if manager in {"yarn", "pnpm"} else (manager, "run", script)


def analyze(root: Path) -> ProjectAnalysis:
    root = root.resolve()
    package = _read_json(root / "package.json")
    declared = package.get("dependencies")
    development = package.get("devDependencies")
    dependencies = {
        **(declared if isinstance(declared, dict) else {}),
        **(development if isinstance(development, dict) else {}),
    }
    scripts = package.get("scripts", {}) if isinstance(package.get("scripts", {}), dict) else {}
    framework = "Unknown"
    runtime = "unknown"
    static = False
    output: str | None = None
    api_routes = False
    build: tuple[str, ...] | None = None
    tests: list[tuple[str, ...]] = []
    start: tuple[str, ...] | None = None
    manager: str | None = None
    if package:
        runtime = "node"
        manager = (
            "pnpm"
            if (root / "pnpm-lock.yaml").exists()
            else "yarn"
            if (root / "yarn.lock").exists()
            else "bun"
            if (root / "bun.lockb").exists() or (root / "bun.lock").exists()
            else "npm"
        )
        for dep, label, is_static, directory in _FRAMEWORKS:
            if dep in dependencies:
                framework, static, output = label, is_static, directory
                break
        if framework == "Vite" and "react" in dependencies:
            framework = "React + Vite"
        elif framework == "Vite" and "vue" in dependencies:
            framework = "Vue + Vite"
        if framework == "Astro":
            astro_config = "\n".join(
                _read_small(root / name) for name in ("astro.config.mjs", "astro.config.js", "astro.config.ts")
            )
            if re.search(r"output\s*:\s*['\"](?:server|hybrid)['\"]", astro_config):
                static = False
        if "build" in scripts:
            build = _npm_command(manager, "build")
        if "test" in scripts:
            tests.append(_npm_command(manager, "test"))
        if "start" in scripts:
            start = _npm_command(manager, "start")
        if framework == "Unknown":
            framework = "Node.js"
        api_routes = any((root / p).exists() for p in ("app/api", "pages/api", "src/app/api", "src/pages/api"))
    elif (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        runtime = "python"
        manifest = _read_small(root / "pyproject.toml") + _read_small(root / "requirements.txt")
        for token, name in (("fastapi", "FastAPI"), ("django", "Django"), ("flask", "Flask")):
            if re.search(rf"(?i)\b{token}\b", manifest):
                framework = name
                break
        if framework == "Unknown":
            framework = "Python"
        api_routes = framework in {"FastAPI", "Django", "Flask"}
        if (root / "pytest.ini").exists() or "pytest" in manifest:
            tests.append(("python", "-m", "pytest"))
    elif (root / "go.mod").exists():
        framework, runtime = "Go", "go"
        build = ("go", "build", "./...")
        tests.append(("go", "test", "./..."))
    elif (root / "Cargo.toml").exists():
        framework, runtime = "Rust", "rust"
        build = ("cargo", "build", "--release")
        tests.append(("cargo", "test"))
    elif (root / "index.html").exists():
        framework, runtime, static, output = "Static HTML", "static", True, "."
    if (root / "Dockerfile").exists() and framework == "Unknown":
        framework, runtime = "Dockerized application", "container"

    configs = tuple(
        p
        for p in (
            "wrangler.toml",
            "wrangler.json",
            "wrangler.jsonc",
            "vercel.json",
            ".vercel/project.json",
            "netlify.toml",
            "amplify.yml",
            "Dockerfile",
            "compose.yaml",
            "docker-compose.yml",
            "terraform.tf",
            "main.tf",
        )
        if (root / p).exists()
    )
    names: set[str] = set()
    # Read only example variable names; never ingest .env values.
    for path in (root / ".env.example", root / ".env.sample", root / "example.env"):
        for line in _read_small(path).splitlines():
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match:
                names.add(match.group(1))
    db = set()
    dep_names = set(dependencies)
    for key, markers in {
        "PostgreSQL": {"pg", "postgres", "psycopg", "psycopg2", "asyncpg"},
        "MySQL": {"mysql", "mysql2", "pymysql"},
        "Redis": {"redis", "ioredis"},
        "SQLite": {"sqlite3", "better-sqlite3"},
    }.items():
        if dep_names & markers:
            db.add(key)
    text = _read_small(root / "requirements.txt") + _read_small(root / "pyproject.toml")
    for key, pattern in (
        ("PostgreSQL", r"psycopg|asyncpg"),
        ("MySQL", r"pymysql|mysqlclient"),
        ("Redis", r"\bredis\b"),
    ):
        if re.search(pattern, text, re.I):
            db.add(key)
    services = []
    if package.get("workspaces"):
        services.append("package workspaces")
    for folder in ("apps", "services", "packages"):
        if (root / folder).is_dir():
            services.extend(str(p.relative_to(root)) for p in (root / folder).iterdir() if p.is_dir())
    return ProjectAnalysis(
        root,
        str(package.get("name") or root.name),
        framework,
        runtime,
        manager,
        build,
        tuple(tests),
        start,
        output,
        static,
        api_routes,
        tuple(sorted(db)),
        tuple(sorted(names)),
        configs,
        tuple(services),
    )
