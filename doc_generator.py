import re
import json
import logging
from typing import List, Dict, Any, Optional
from app.models.schemas import APIRefItem, APIRefParameter
from app.services.code_parser import ParsedFileResult, ParsedFunction, ParsedClass
from app.services.watsonx_client import watsonx_client

logger = logging.getLogger(__name__)


class DocGeneratorService:
    @staticmethod
    def _parse_params_from_signature(sig: str) -> List[APIRefParameter]:
        """Extract parameter names and type hints from function signature."""
        params: List[APIRefParameter] = []
        match = re.search(r"\((.*?)\)", sig)
        if not match:
            return params

        raw_args = match.group(1)
        if not raw_args.strip():
            return params

        parts = [p.strip() for p in raw_args.split(",") if p.strip()]
        for p in parts:
            if p in ("self", "cls"):
                continue
            
            p_name = p
            p_type = None
            p_default = None

            if "=" in p:
                p_name, p_default = [x.strip() for x in p.split("=", 1)]
            if ":" in p_name:
                p_name, p_type = [x.strip() for x in p_name.split(":", 1)]

            params.append(APIRefParameter(
                name=p_name,
                type=p_type,
                required=p_default is None,
                default=p_default,
                description=f"Parameter `{p_name}` passed to invocation."
            ))

        return params

    @classmethod
    def generate_heuristic_docs(cls, parsed_file: ParsedFileResult) -> List[APIRefItem]:
        """Generate accurate, grounded API documentation strictly derived from AST / code metadata."""
        items: List[APIRefItem] = []

        # 1. Classes
        for cl in parsed_file.classes:
            purpose = cl.docstring.strip().split("\n")[0] if cl.docstring else f"Class definition `{cl.name}` declared in module."
            items.append(APIRefItem(
                file_path=parsed_file.file_path,
                name=cl.name,
                type="class",
                signature=f"class {cl.name}",
                purpose=purpose,
                parameters=[],
                returns=f"Instance of `{cl.name}`",
                errors=[],
                line_number=cl.line_number
            ))

            # Class methods
            for m in cl.methods:
                if m.name.startswith("__") and m.name not in ("__init__", "__call__"):
                    continue
                m_purpose = m.docstring.strip().split("\n")[0] if m.docstring else f"Method `{m.name}` of class `{cl.name}`."
                m_params = cls._parse_params_from_signature(m.signature)
                items.append(APIRefItem(
                    file_path=parsed_file.file_path,
                    name=f"{cl.name}.{m.name}",
                    type="method",
                    signature=m.signature,
                    purpose=m_purpose,
                    parameters=m_params,
                    returns="Method return value (see implementation for details)" if not m.name == "__init__" else None,
                    errors=[],
                    line_number=m.line_number
                ))

        # 2. Standalone Functions & Routes
        for fn in parsed_file.functions:
            # Skip private/internal helpers if desired, but keep public functions
            if fn.name.startswith("_") and not fn.is_route:
                continue

            item_type = "route" if fn.is_route else "function"
            if fn.is_route:
                purpose = f"HTTP {fn.http_method} endpoint handler for route `{fn.http_path}`."
            elif fn.docstring:
                purpose = fn.docstring.strip().split("\n")[0]
            else:
                purpose = f"Function `{fn.name}` implementing core module logic."

            params = cls._parse_params_from_signature(fn.signature)
            
            # Detect error raises in code snippet
            errors = []
            if "raise " in parsed_file.content or "throw " in parsed_file.content:
                error_matches = re.findall(r"(?:raise|throw)\s+([A-Za-z0-9_]+)", parsed_file.content)
                errors = list(set(error_matches[:3]))

            items.append(APIRefItem(
                file_path=parsed_file.file_path,
                name=fn.name,
                type=item_type,
                signature=fn.signature,
                purpose=purpose,
                parameters=params,
                returns="Value returned upon successful execution.",
                errors=errors,
                line_number=fn.line_number,
                http_method=fn.http_method,
                http_path=fn.http_path
            ))

        return items

    @classmethod
    async def generate_file_docs(cls, parsed_file: ParsedFileResult) -> List[APIRefItem]:
        """Generate API reference entries using IBM watsonx Granite or AST heuristic fallback."""
        heuristic_items = cls.generate_heuristic_docs(parsed_file)
        
        # If no public symbols or watsonx not configured, return grounded heuristic items
        if not heuristic_items or not watsonx_client.is_configured():
            return heuristic_items

        # Limit symbols per prompt to avoid overflowing context
        symbols_to_doc = heuristic_items[:12]
        symbols_summary = [
            f"- Name: {item.name}, Type: {item.type}, Signature: `{item.signature}`, Line: {item.line_number}"
            for item in symbols_to_doc
        ]

        prompt = f"""You are a precise technical API documentation generator.
Analyze the following source code file and refine the reference entries for these detected symbols:
File: {parsed_file.file_path}
Language: {parsed_file.language}

Symbols to document:
{chr(10).join(symbols_summary)}

Source code:
```
{parsed_file.content[:3500]}
```

Instructions:
1. Provide a concise, 1-line factual purpose for each symbol strictly based on the code.
2. If purpose is unclear, state "Behavior depends on dynamic runtime inputs." (Do NOT invent functionality).
3. Return a valid JSON array of objects with keys:
   - "name": string
   - "purpose": string
   - "returns": string or null
   - "errors": array of strings

Output ONLY the raw JSON array.
"""
        try:
            resp = await watsonx_client.chat_completion(
                messages=[
                    {"role": "system", "content": "You are a code documentation assistant. Output only JSON."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1024,
                temperature=0.1
            )
            raw_text = resp.get("content", "").strip()
            
            # Extract JSON array
            json_match = re.search(r"\[\s*\{.*\}\s*\]", raw_text, re.DOTALL)
            if json_match:
                enriched_data = json.loads(json_match.group(0))
                enriched_map = {item.get("name"): item for item in enriched_data if isinstance(item, dict)}
                
                # Merge enriched explanations back
                for item in heuristic_items:
                    if item.name in enriched_map:
                        en = enriched_map[item.name]
                        if en.get("purpose"):
                            item.purpose = en["purpose"]
                        if en.get("returns"):
                            item.returns = en["returns"]
                        if en.get("errors") and isinstance(en["errors"], list):
                            item.errors = list(set(item.errors + en["errors"]))

            return heuristic_items
        except Exception as e:
            logger.warning(f"watsonx doc generation fallback to heuristics for {parsed_file.file_path}: {e}")
            return heuristic_items


doc_generator = DocGeneratorService()
