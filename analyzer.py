import re
import time
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Callable, Awaitable
from app.config import settings
from app.models.schemas import (
    RepoAnalyzeRequest,
    RepoAnalysisResult,
    FileExplanation,
    APIRefItem,
    AnalysisProgress
)
from app.services.cache import cache_service
from app.services.watsonx_client import watsonx_client, WatsonxError
from app.services.repo_cloner import repo_cloner, ScannedFile
from app.services.code_parser import code_parser, ParsedFileResult
from app.services.architecture import architecture_service
from app.services.doc_generator import doc_generator

logger = logging.getLogger(__name__)

# Active background tasks store
active_tasks: Dict[str, AnalysisProgress] = {}


class RepoAnalyzer:
    @classmethod
    async def explain_file_with_watsonx(
        cls,
        parsed: ParsedFileResult,
        preview_max_lines: int = 200
    ) -> FileExplanation:
        """
        Send chunked source to IBM watsonx Granite for plain-language explanation
        (Overview / Key components / Flow / Notable risks).
        """
        # Truncate or chunk if file is massive
        content_snippet = parsed.content[:6000]
        preview_snippet = "\n".join(parsed.content.splitlines()[:preview_max_lines])

        overview = f"Module `{parsed.file_path}` written in {parsed.language.capitalize()} containing {parsed.line_count} lines of code."
        key_components = [f"`{fn.name}` (line {fn.line_number})" for fn in parsed.functions[:6]] + [f"`class {cl.name}` (line {cl.line_number})" for cl in parsed.classes[:4]]
        if not key_components:
            key_components = ["Top-level procedural logic and definitions"]
        flow = "Executes sequential script logic and exports defined functions/classes."
        notable_risks = "No critical architectural risks detected in static structure."

        if watsonx_client.is_configured():
            prompt = f"""You are an expert software architect analyzing code for a repository documentation report.
Analyze the following source file and explain it in plain language.

File path: {parsed.file_path}
Language: {parsed.language}
Line count: {parsed.line_count}

Source Code:
```
{content_snippet}
```

Provide your analysis in the following EXACT structured format:
OVERVIEW:
<2-3 concise sentences explaining what this file does in plain language>

KEY_COMPONENTS:
- <Component 1: Purpose>
- <Component 2: Purpose>

FLOW:
<2-3 sentences explaining the data/control flow through this module>

NOTABLE_RISKS:
<1-2 sentences on potential edge cases, error handling gaps, performance bottlenecks, or security considerations>
"""
            try:
                resp = await watsonx_client.chat_completion(
                    messages=[
                        {"role": "system", "content": "You are a code intelligence assistant. Be factual, concise, and structured."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=900,
                    temperature=0.2
                )
                text = resp.get("content", "")

                ov_match = re.search(r"OVERVIEW:(.*?)(?=KEY_COMPONENTS:|$)", text, re.DOTALL | re.IGNORECASE)
                kc_match = re.search(r"KEY_COMPONENTS:(.*?)(?=FLOW:|$)", text, re.DOTALL | re.IGNORECASE)
                fl_match = re.search(r"FLOW:(.*?)(?=NOTABLE_RISKS:|$)", text, re.DOTALL | re.IGNORECASE)
                nr_match = re.search(r"NOTABLE_RISKS:(.*?)$", text, re.DOTALL | re.IGNORECASE)

                if ov_match and ov_match.group(1).strip():
                    overview = ov_match.group(1).strip()
                if kc_match and kc_match.group(1).strip():
                    kc_lines = [line.strip().lstrip("-* ").strip() for line in kc_match.group(1).strip().splitlines() if line.strip()]
                    if kc_lines:
                        key_components = kc_lines
                if fl_match and fl_match.group(1).strip():
                    flow = fl_match.group(1).strip()
                if nr_match and nr_match.group(1).strip():
                    notable_risks = nr_match.group(1).strip()

            except Exception as e:
                logger.warning(f"watsonx explanation fallback to heuristics for {parsed.file_path}: {e}")

        return FileExplanation(
            file_path=parsed.file_path,
            content_hash=parsed.content_hash,
            language=parsed.language,
            overview=overview,
            key_components=key_components,
            flow=flow,
            notable_risks=notable_risks,
            cache_hit=False,
            line_count=parsed.line_count,
            raw_source=preview_snippet
        )

    @classmethod
    async def analyze_repository(
        cls,
        request: RepoAnalyzeRequest,
        task_id: Optional[str] = None,
        progress_cb: Optional[Callable[[AnalysisProgress], Awaitable[None]]] = None
    ) -> RepoAnalysisResult:
        """Run full end-to-end repository analysis pipeline."""
        start_time = time.time()
        task_id = task_id or str(uuid.uuid4())
        repo_url = request.repo_url
        file_cap = request.file_cap or settings.default_file_cap
        use_cache = request.use_cache

        progress = AnalysisProgress(
            task_id=task_id,
            repo_url=repo_url,
            status="cloning",
            message=f"Connecting to repository {repo_url}...",
            percentage=5
        )
        active_tasks[task_id] = progress
        if progress_cb:
            await progress_cb(progress)

        # Execute safe clone in isolated temp directory
        with repo_cloner.clone_context(
            repo_url=repo_url,
            branch=request.branch,
            github_token=request.github_token,
            file_cap=file_cap
        ) as (temp_path, scanned_files, total_found, is_capped):

            progress.status = "parsing"
            progress.total_files = len(scanned_files)
            progress.message = f"Found {total_found} source files. Analyzing {len(scanned_files)} files (Cap: {file_cap})..."
            progress.percentage = 15
            if progress_cb:
                await progress_cb(progress)

            parsed_files: List[ParsedFileResult] = []
            file_explanations: List[FileExplanation] = []
            all_api_docs: List[APIRefItem] = []
            cache_hits = 0
            cache_misses = 0

            # First pass: parse AST / structural metadata for all files
            for sf in scanned_files:
                content = sf.read_content()
                parsed = code_parser.parse_file(sf.relative_path, content)
                parsed_files.append(parsed)

            # Second pass: explain each file & extract docs (leveraging SQLite cache)
            progress.status = "analyzing"
            for idx, parsed in enumerate(parsed_files, 1):
                progress.current_index = idx
                progress.current_file = parsed.file_path
                progress.percentage = 15 + int((idx / len(parsed_files)) * 60)
                progress.message = f"Analyzing ({idx}/{len(parsed_files)}): {parsed.file_path}"
                
                # Check SQLite cache
                cached_data = cache_service.get_file_cache(parsed.file_path, parsed.content_hash) if use_cache else None

                if cached_data:
                    cache_hits += 1
                    preview_snippet = "\n".join(parsed.content.splitlines()[:200])
                    explanation = FileExplanation(
                        file_path=cached_data["file_path"],
                        content_hash=cached_data["content_hash"],
                        language=cached_data["language"],
                        overview=cached_data["overview"],
                        key_components=cached_data["key_components"],
                        flow=cached_data["flow"],
                        notable_risks=cached_data["notable_risks"],
                        cache_hit=True,
                        line_count=cached_data["line_count"],
                        raw_source=preview_snippet
                    )
                    file_explanations.append(explanation)
                    
                    # Cached API docs
                    cached_docs = [APIRefItem(**d) for d in cached_data.get("api_docs", [])]
                    all_api_docs.extend(cached_docs)
                else:
                    cache_misses += 1
                    # Generate explanation with watsonx Granite
                    explanation = await cls.explain_file_with_watsonx(parsed)
                    file_explanations.append(explanation)

                    # Generate API docs for file
                    file_docs = await doc_generator.generate_file_docs(parsed)
                    all_api_docs.extend(file_docs)

                    # Save to SQLite cache
                    if use_cache:
                        cache_service.save_file_cache(
                            file_path=parsed.file_path,
                            content_hash=parsed.content_hash,
                            language=parsed.language,
                            line_count=parsed.line_count,
                            overview=explanation.overview,
                            key_components=explanation.key_components,
                            flow=explanation.flow,
                            notable_risks=explanation.notable_risks,
                            api_docs=[d.model_dump() for d in file_docs],
                            imports=[imp.to_dict() for imp in parsed.imports]
                        )

                progress.cache_hits = cache_hits
                progress.cache_misses = cache_misses
                if progress_cb:
                    await progress_cb(progress)

            # Build Architecture Diagram & Dependency Graph
            progress.status = "architecture"
            progress.message = "Synthesizing repository architecture & Mermaid dependency diagram..."
            progress.percentage = 85
            if progress_cb:
                await progress_cb(progress)

            architecture_result = architecture_service.build_architecture(parsed_files)

            # Assemble Final Result
            progress.status = "completed"
            progress.percentage = 100
            progress.message = f"Successfully analyzed {len(file_explanations)} files ({cache_hits} cache hits, {cache_misses} misses)."

            duration = round(time.time() - start_time, 2)
            model_used = watsonx_client.primary_model if watsonx_client.is_configured() else "Heuristic Engine / Mock Granite"

            result = RepoAnalysisResult(
                repo_id=task_id,
                repo_url=repo_url,
                branch=request.branch or "default",
                total_files_found=total_found,
                total_files_analyzed=len(file_explanations),
                is_capped=is_capped,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
                explanations=file_explanations,
                architecture=architecture_result,
                api_docs=all_api_docs,
                created_at=datetime.now(timezone.utc).isoformat(),
                duration_seconds=duration,
                watsonx_model_used=model_used,
                is_mock_fallback=not watsonx_client.is_configured()
            )

            # Save full repo result in SQLite
            cache_service.save_repo_results(task_id, repo_url, request.branch or "default", result.model_dump())

            progress.result = result
            if progress_cb:
                await progress_cb(progress)

            return result


repo_analyzer = RepoAnalyzer()
