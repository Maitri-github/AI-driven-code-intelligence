import re
import logging
from typing import List, Dict, Any, Optional, Tuple
import httpx
from app.models.schemas import PRAnalysisResponse
from app.services.repo_cloner import RepoCloner
from app.services.watsonx_client import watsonx_client

logger = logging.getLogger(__name__)


class PRAnalyzerService:
    @staticmethod
    def extract_changed_files_from_diff(diff_text: str) -> List[str]:
        """Extract list of modified file paths from unified git diff."""
        files = []
        for line in diff_text.splitlines():
            # Match diff --git a/path b/path
            git_match = re.match(r"^diff --git a/(.*?) b/(.*?)$", line)
            if git_match:
                files.append(git_match.group(2))
                continue
            # Match +++ b/path
            plus_match = re.match(r"^\+\+\+ b/(.*?)$", line)
            if plus_match and plus_match.group(1) != "dev/null":
                files.append(plus_match.group(1))

        # Deduplicate preserving order
        seen = set()
        unique_files = []
        for f in files:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)

        return unique_files if unique_files else ["modified_files"]

    @classmethod
    def _heuristic_impact_analysis(cls, diff_text: str, changed_files: List[str]) -> Tuple[str, str, str, bool, bool]:
        """Heuristically assess PR impact based on diff patterns."""
        has_import_change = bool(re.search(r"^[+-]\s*(import|from|require|require_relative)\s+", diff_text, re.MULTILINE))
        has_new_files = any("--- /dev/null" in diff_text or "new file mode" in diff_text for _ in [1])
        has_deleted_files = any("+++ /dev/null" in diff_text or "deleted file mode" in diff_text for _ in [1])
        
        needs_diagram_update = has_import_change or has_new_files or has_deleted_files
        
        has_sig_change = bool(re.search(r"^[+-]\s*(def|async def|function|class|func|public|router\.|app\.)\s+", diff_text, re.MULTILINE))
        has_doc_change = bool(re.search(r"^[+-]\s*(\"\"\"|/\*\*|\*|#\s*param)", diff_text, re.MULTILINE))
        
        needs_docs_update = has_sig_change or not has_doc_change

        summary_lines = [
            f"- Modified {len(changed_files)} file(s): " + ", ".join([f"`{f}`" for f in changed_files[:5]]) + ("..." if len(changed_files) > 5 else ""),
            f"- Contains {'additions of new module dependencies' if has_import_change else 'internal implementation updates'}.",
            f"- Contains {'public interface / signature modifications' if has_sig_change else 'internal logic adjustments'}."
        ]
        summary = "\n".join(summary_lines)

        if needs_diagram_update:
            arch_impact = "⚠️ **Diagram Update Recommended:** Import statements or subsystem file structures were modified. The repo architecture diagram should be re-rendered to capture new dependencies."
        else:
            arch_impact = "✅ **Diagram Unaffected:** No changes to module imports or high-level subsystem relationships were detected."

        if needs_docs_update:
            docs_impact = "⚠️ **API Docs Update Recommended:** Function, class, or route signatures were changed in this PR. Verify that updated parameter signatures and return types are documented."
        else:
            docs_impact = "✅ **API Docs Unaffected:** No public function/route signatures were altered."

        return summary, arch_impact, docs_impact, needs_diagram_update, needs_docs_update

    @classmethod
    async def analyze_pr_diff(
        cls,
        repo_url: str,
        pr_diff: str,
        pr_title: str = "Pull Request",
        pr_number: Optional[int] = 1,
        github_token: Optional[str] = None,
        post_comment: bool = False
    ) -> PRAnalysisResponse:
        """
        Analyze PR diff with IBM watsonx Granite (or heuristic engine),
        generate markdown review comment, and optionally post to GitHub.
        """
        changed_files = cls.extract_changed_files_from_diff(pr_diff)
        
        # Start with heuristic baseline
        summary, arch_impact, docs_impact, needs_diag, needs_docs = cls._heuristic_impact_analysis(pr_diff, changed_files)

        # If watsonx is available, use Granite to generate intelligent plain-language summary
        if watsonx_client.is_configured() and len(pr_diff.strip()) > 10:
            prompt = f"""You are a GitHub PR intelligence reviewer.
Analyze this Pull Request diff and provide an accurate technical review comment.

Repository: {repo_url}
PR Title: {pr_title}
Changed Files: {', '.join(changed_files)}

Unified Diff:
```diff
{pr_diff[:4000]}
```

Answer in this exact format:
SUMMARY: <2-3 bullet points explaining what functional changes occurred>
ARCH_IMPACT: <1-2 sentences on whether architecture diagrams / imports need updating>
DOCS_IMPACT: <1-2 sentences on whether API docs or signatures need updating>
NEEDS_DIAGRAM_UPDATE: <YES or NO>
NEEDS_DOCS_UPDATE: <YES or NO>
"""
            try:
                resp = await watsonx_client.chat_completion(
                    messages=[
                        {"role": "system", "content": "You are a code review assistant. Output concise reviews."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=800,
                    temperature=0.2
                )
                text = resp.get("content", "")
                
                # Parse structured response
                sum_match = re.search(r"SUMMARY:(.*?)(?=ARCH_IMPACT:|$)", text, re.DOTALL | re.IGNORECASE)
                arch_match = re.search(r"ARCH_IMPACT:(.*?)(?=DOCS_IMPACT:|$)", text, re.DOTALL | re.IGNORECASE)
                doc_match = re.search(r"DOCS_IMPACT:(.*?)(?=NEEDS_DIAGRAM_UPDATE:|$)", text, re.DOTALL | re.IGNORECASE)
                diag_flag = re.search(r"NEEDS_DIAGRAM_UPDATE:\s*(YES|NO)", text, re.IGNORECASE)
                docs_flag = re.search(r"NEEDS_DOCS_UPDATE:\s*(YES|NO)", text, re.IGNORECASE)

                if sum_match and sum_match.group(1).strip():
                    summary = sum_match.group(1).strip()
                if arch_match and arch_match.group(1).strip():
                    arch_impact = arch_match.group(1).strip()
                if doc_match and doc_match.group(1).strip():
                    docs_impact = doc_match.group(1).strip()
                if diag_flag:
                    needs_diag = diag_flag.group(1).upper() == "YES"
                if docs_flag:
                    needs_docs = docs_flag.group(1).upper() == "YES"

            except Exception as e:
                logger.warning(f"watsonx PR diff analysis fallback to heuristics: {e}")

        # Assemble full Markdown comment
        comment_markdown = f"""## 🔍 Schematic Code Intelligence — PR Review Summary

**Pull Request:** #{pr_number or 1} - *{pr_title}*  
**Files Modified:** {len(changed_files)} ({', '.join([f'`{f}`' for f in changed_files[:6]])})

### 📝 What Changed
{summary}

### 🏗️ Architecture & Dependency Impact
- **Diagram Status:** {'⚠️ **Needs Update**' if needs_diag else '✅ **Up to date**'}
- **Details:** {arch_impact}

### 📚 API Documentation Impact
- **Docs Status:** {'⚠️ **Needs Update**' if needs_docs else '✅ **Up to date**'}
- **Details:** {docs_impact}

---
*⚡ Automated by [Schematic](https://github.com/) with IBM watsonx.ai (Granite models)*"""

        posted_to_github = False
        github_error = None

        if post_comment and github_token and pr_number:
            try:
                owner, repo = RepoCloner.parse_repo_url(repo_url)
                post_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
                headers = {
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "Schematic-Code-Intelligence"
                }
                async with httpx.AsyncClient(timeout=15.0) as client:
                    gh_resp = await client.post(post_url, headers=headers, json={"body": comment_markdown})
                    if gh_resp.status_code in (200, 201):
                        posted_to_github = True
                    else:
                        github_error = f"GitHub API error {gh_resp.status_code}: {gh_resp.text[:200]}"
            except Exception as e:
                github_error = str(e)

        return PRAnalysisResponse(
            summary=summary,
            architecture_impact=arch_impact,
            docs_impact=docs_impact,
            needs_diagram_update=needs_diag,
            needs_docs_update=needs_docs,
            changed_files=changed_files,
            comment_markdown=comment_markdown,
            posted_to_github=posted_to_github,
            error=github_error
        )


pr_analyzer = PRAnalyzerService()
