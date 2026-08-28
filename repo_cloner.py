import os
import re
import io
import shutil
import zipfile
import tempfile
import fnmatch
import logging
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Generator
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

# Standard build / dependency folders to unconditionally ignore
ALWAYS_IGNORE_DIRS = {
    ".git", ".github", "node_modules", "venv", ".venv", "env", ".env",
    "__pycache__", "dist", "build", ".next", ".nuxt", "out", ".cache",
    "target", "bin", "obj", ".idea", ".vscode", "vendor", "coverage",
    ".pytest_cache", ".mypy_cache", ".tox", "eggs", ".eggs", "site-packages"
}

# Standard binary or generated file patterns to ignore
ALWAYS_IGNORE_PATTERNS = {
    "*.min.js", "*.min.css", "*.bundle.js", "*.map", "*.lock", "package-lock.json",
    "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "*.pyc", "*.pyo",
    "*.pyd", "*.so", "*.dll", "*.dylib", "*.exe", "*.class", "*.jar", "*.war",
    "*.svg", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.pdf", "*.zip", "*.tar.gz"
}


class ScannedFile:
    def __init__(self, relative_path: str, absolute_path: Path, extension: str, size_bytes: int, line_count: int):
        self.relative_path = relative_path.replace("\\", "/")
        self.absolute_path = absolute_path
        self.extension = extension.lower()
        self.size_bytes = size_bytes
        self.line_count = line_count

    def read_content(self, max_bytes: int = 1_000_000) -> str:
        """Safely read file content with encoding fallbacks."""
        try:
            with open(self.absolute_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(max_bytes)
        except Exception as e:
            logger.warning(f"Error reading file {self.relative_path}: {e}")
            return ""


class RepoCloner:
    @staticmethod
    def parse_repo_url(url: str) -> Tuple[str, str]:
        """Extract owner and repo name from GitHub URL."""
        cleaned = url.strip().rstrip("/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        
        # Match github.com/owner/repo or owner/repo
        match = re.search(r"github\.com[/:]([\w\.-]+)/([\w\.-]+)", cleaned)
        if match:
            return match.group(1), match.group(2)
        
        parts = [p for p in cleaned.split("/") if p]
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        
        raise ValueError(f"Invalid GitHub repository URL: {url}")

    @staticmethod
    def _is_git_available() -> bool:
        try:
            res = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=3)
            return res.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _parse_gitignore(repo_root: Path) -> List[str]:
        """Read and parse root .gitignore file."""
        gitignore_file = repo_root / ".gitignore"
        patterns = []
        if gitignore_file.exists():
            try:
                with open(gitignore_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            patterns.append(line)
            except Exception as e:
                logger.warning(f"Failed to read .gitignore: {e}")
        return patterns

    @staticmethod
    def _should_ignore(rel_path: str, gitignore_patterns: List[str]) -> bool:
        """Check if file should be ignored based on default rules or .gitignore."""
        parts = rel_path.replace("\\", "/").split("/")
        
        # Check directory names
        for part in parts[:-1]:
            if part in ALWAYS_IGNORE_DIRS:
                return True

        filename = parts[-1]
        # Check standard ignored patterns
        for pattern in ALWAYS_IGNORE_PATTERNS:
            if fnmatch.fnmatch(filename, pattern):
                return True

        # Check gitignore patterns
        for pattern in gitignore_patterns:
            pattern = pattern.rstrip("/")
            if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(filename, pattern):
                return True
            if pattern.startswith("/") and fnmatch.fnmatch("/" + rel_path, pattern):
                return True

        return False

    @classmethod
    def clone_or_download(
        cls,
        repo_url: str,
        target_dir: Path,
        branch: Optional[str] = None,
        github_token: Optional[str] = None
    ) -> Path:
        """
        Clone via git CLI if available, or download archive zip via GitHub API.
        Never logs credentials.
        """
        owner, repo = cls.parse_repo_url(repo_url)
        git_available = cls._is_git_available()

        if git_available:
            try:
                clone_url = f"https://github.com/{owner}/{repo}.git"
                if github_token:
                    clone_url = f"https://{github_token}@github.com/{owner}/{repo}.git"

                cmd = ["git", "clone", "--depth", "1"]
                if branch:
                    cmd.extend(["--branch", branch])
                cmd.extend([clone_url, str(target_dir)])

                logger.info(f"Cloning repository {owner}/{repo} via git CLI...")
                # Do NOT log cmd directly because it could contain github_token
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    return target_dir
                logger.warning(f"git clone returned non-zero code ({result.returncode}), falling back to zip download...")
            except Exception as e:
                logger.warning(f"git clone failed: {e}. Falling back to zip download...")

        # Fallback: Download ZIP archive directly from GitHub
        logger.info(f"Downloading archive ZIP for {owner}/{repo}...")
        headers = {
            "User-Agent": "Schematic-Code-Intelligence-App",
            "Accept": "application/vnd.github.v3+json"
        }
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"

        # Try branch zip or default zipball
        zip_urls = []
        if branch:
            zip_urls.append(f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}")
            zip_urls.append(f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip")
        zip_urls.append(f"https://api.github.com/repos/{owner}/{repo}/zipball")
        zip_urls.append(f"https://github.com/{owner}/{repo}/archive/refs/heads/main.zip")
        zip_urls.append(f"https://github.com/{owner}/{repo}/archive/refs/heads/master.zip")

        download_success = False
        last_error = ""

        for zip_url in zip_urls:
            try:
                with httpx.Client(follow_redirects=True, timeout=45.0) as client:
                    resp = client.get(zip_url, headers=headers)
                    if resp.status_code == 200:
                        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                            # Extract all files
                            z.extractall(target_dir)
                        
                        # GitHub zip archives contain a root directory like owner-repo-hash
                        extracted_children = list(target_dir.iterdir())
                        if len(extracted_children) == 1 and extracted_children[0].is_dir():
                            root_extracted = extracted_children[0]
                            # Move files up to target_dir
                            temp_move_dir = target_dir.parent / f"{target_dir.name}_temp"
                            root_extracted.rename(temp_move_dir)
                            shutil.rmtree(target_dir, ignore_errors=True)
                            temp_move_dir.rename(target_dir)
                        
                        download_success = True
                        break
                    else:
                        last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as e:
                last_error = str(e)

        if not download_success:
            raise RuntimeError(f"Could not clone or download repository {owner}/{repo}: {last_error}")

        return target_dir

    @classmethod
    def scan_repository(
        cls,
        repo_dir: Path,
        file_cap: int = 40,
        allowed_extensions: Optional[List[str]] = None
    ) -> Tuple[List[ScannedFile], int, bool]:
        """
        Walk repository files, apply extension filtering, respect .gitignore,
        and return (capped_files, total_matched_count, is_capped).
        """
        exts = allowed_extensions or settings.extension_list
        gitignore_patterns = cls._parse_gitignore(repo_dir)
        matched_files: List[ScannedFile] = []

        for root, dirs, files in os.walk(repo_dir):
            rel_root = os.path.relpath(root, repo_dir).replace("\\", "/")
            if rel_root == ".":
                rel_root = ""

            # Filter out ignored directories in-place
            dirs[:] = [
                d for d in dirs
                if d not in ALWAYS_IGNORE_DIRS and not cls._should_ignore(f"{rel_root}/{d}".strip("/"), gitignore_patterns)
            ]

            for file in files:
                rel_file = f"{rel_root}/{file}".strip("/").replace("\\", "/")
                file_path = Path(root) / file

                if cls._should_ignore(rel_file, gitignore_patterns):
                    continue

                ext = file_path.suffix.lower()
                if ext in exts:
                    try:
                        size = file_path.stat().st_size
                        # Count lines
                        line_count = 0
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            for _ in f:
                                line_count += 1
                        
                        matched_files.append(ScannedFile(
                            relative_path=rel_file,
                            absolute_path=file_path,
                            extension=ext,
                            size_bytes=size,
                            line_count=line_count
                        ))
                    except Exception as e:
                        logger.warning(f"Could not stat file {rel_file}: {e}")

        # Sort files deterministically (top-level and core modules first, then alphabetically)
        def file_sort_key(f: ScannedFile):
            depth = f.relative_path.count("/")
            # Prioritize entry points like main, app, index
            is_entry = any(f.relative_path.lower().startswith(p) for p in ["main", "app", "index", "server", "src/index", "src/main"])
            return (0 if is_entry else 1, depth, f.relative_path)

        matched_files.sort(key=file_sort_key)
        total_matched = len(matched_files)
        is_capped = total_matched > file_cap
        capped_files = matched_files[:file_cap]

        return capped_files, total_matched, is_capped

    @classmethod
    @contextmanager
    def clone_context(
        cls,
        repo_url: str,
        branch: Optional[str] = None,
        github_token: Optional[str] = None,
        file_cap: int = 40,
        allowed_extensions: Optional[List[str]] = None
    ) -> Generator[Tuple[Path, List[ScannedFile], int, bool], None, None]:
        """
        Safe context manager:
        Creates a temporary directory, clones/downloads the repo, scans files,
        yields (temp_path, scanned_files, total_count, is_capped),
        and safely deletes the temporary folder on exit.
        """
        temp_dir = Path(tempfile.mkdtemp(prefix="schematic_repo_"))
        try:
            cls.clone_or_download(repo_url, temp_dir, branch=branch, github_token=github_token)
            files, total_found, is_capped = cls.scan_repository(
                temp_dir,
                file_cap=file_cap,
                allowed_extensions=allowed_extensions
            )
            yield temp_dir, files, total_found, is_capped
        finally:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.info(f"Cleaned up temporary clone directory {temp_dir}")
            except Exception as e:
                logger.warning(f"Error cleaning up {temp_dir}: {e}")


repo_cloner = RepoCloner()
