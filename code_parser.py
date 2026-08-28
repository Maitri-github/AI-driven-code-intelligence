import re
import ast
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript_react",
    ".ts": "typescript",
    ".tsx": "typescript_react",
    ".java": "java",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".php": "php"
}


class ParsedFunction:
    def __init__(self, name: str, signature: str, docstring: str, line_number: int, is_route: bool = False, http_method: Optional[str] = None, http_path: Optional[str] = None):
        self.name = name
        self.signature = signature
        self.docstring = docstring
        self.line_number = line_number
        self.is_route = is_route
        self.http_method = http_method
        self.http_path = http_path


class ParsedClass:
    def __init__(self, name: str, docstring: str, line_number: int, methods: List[ParsedFunction] = None):
        self.name = name
        self.docstring = docstring
        self.line_number = line_number
        self.methods = methods or []


class ParsedImport:
    def __init__(self, module: str, imported_names: List[str], is_relative: bool = False):
        self.module = module
        self.imported_names = imported_names
        self.is_relative = is_relative

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module": self.module,
            "imported_names": self.imported_names,
            "is_relative": self.is_relative
        }


class ParsedFileResult:
    def __init__(
        self,
        file_path: str,
        content: str,
        content_hash: str,
        language: str,
        line_count: int,
        imports: List[ParsedImport],
        classes: List[ParsedClass],
        functions: List[ParsedFunction]
    ):
        self.file_path = file_path
        self.content = content
        self.content_hash = content_hash
        self.language = language
        self.line_count = line_count
        self.imports = imports
        self.classes = classes
        self.functions = functions


class CodeParser:
    @staticmethod
    def compute_hash(content: str) -> str:
        """Compute SHA-256 hash of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def detect_language(file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        return LANGUAGE_MAP.get(ext, "unknown")

    @classmethod
    def parse_file(cls, file_path: str, content: str) -> ParsedFileResult:
        """Parse source code for imports, functions, classes, and routes."""
        lang = cls.detect_language(file_path)
        content_hash = cls.compute_hash(content)
        line_count = content.count("\n") + 1 if content else 0

        imports: List[ParsedImport] = []
        classes: List[ParsedClass] = []
        functions: List[ParsedFunction] = []

        if lang == "python":
            imports, classes, functions = cls._parse_python(content)
        elif lang in ("javascript", "javascript_react", "typescript", "typescript_react"):
            imports, classes, functions = cls._parse_javascript(content)
        elif lang == "go":
            imports, classes, functions = cls._parse_go(content)
        elif lang == "java":
            imports, classes, functions = cls._parse_java(content)
        elif lang == "ruby":
            imports, classes, functions = cls._parse_ruby(content)
        else:
            imports, classes, functions = cls._parse_generic(content)

        return ParsedFileResult(
            file_path=file_path,
            content=content,
            content_hash=content_hash,
            language=lang,
            line_count=line_count,
            imports=imports,
            classes=classes,
            functions=functions
        )

    # -------------------------------------------------------------
    # Python Parser (AST with regex fallback)
    # -------------------------------------------------------------
    @classmethod
    def _parse_python(cls, content: str) -> Tuple[List[ParsedImport], List[ParsedClass], List[ParsedFunction]]:
        imports: List[ParsedImport] = []
        classes: List[ParsedClass] = []
        functions: List[ParsedFunction] = []

        try:
            tree = ast.parse(content)
            for node in tree.body:
                # Imports
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(ParsedImport(module=alias.name, imported_names=[alias.asname or alias.name]))
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    names = [a.name for a in node.names]
                    imports.append(ParsedImport(module=mod, imported_names=names, is_relative=bool(node.level > 0)))

                # Classes
                elif isinstance(node, ast.ClassDef):
                    cls_doc = ast.get_docstring(node) or ""
                    cls_methods: List[ParsedFunction] = []
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            m_doc = ast.get_docstring(item) or ""
                            # Format signature
                            args_str = ", ".join([a.arg for a in item.args.args])
                            sig = f"def {item.name}({args_str})"
                            cls_methods.append(ParsedFunction(name=item.name, signature=sig, docstring=m_doc, line_number=item.lineno))
                    classes.append(ParsedClass(name=node.name, docstring=cls_doc, line_number=node.lineno, methods=cls_methods))

                # Top-level Functions & Routes
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn_doc = ast.get_docstring(node) or ""
                    args_str = ", ".join([a.arg for a in node.args.args])
                    sig = f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}({args_str})"
                    
                    # Detect web route decorators (FastAPI, Flask, etc.)
                    is_route = False
                    http_method = None
                    http_path = None
                    for dec in node.decorator_list:
                        dec_str = ast.unparse(dec) if hasattr(ast, "unparse") else ""
                        route_match = re.search(r"\.(get|post|put|delete|patch|options|head)\s*\(\s*['\"]([^'\"]+)['\"]", dec_str, re.IGNORECASE)
                        if route_match:
                            is_route = True
                            http_method = route_match.group(1).upper()
                            http_path = route_match.group(2)
                            break
                    
                    functions.append(ParsedFunction(
                        name=node.name,
                        signature=sig,
                        docstring=fn_doc,
                        line_number=node.lineno,
                        is_route=is_route,
                        http_method=http_method,
                        http_path=http_path
                    ))
            return imports, classes, functions
        except Exception:
            # Fallback to regex parser if AST parsing encounters syntax errors (e.g. template files)
            return cls._parse_python_regex(content)

    @classmethod
    def _parse_python_regex(cls, content: str) -> Tuple[List[ParsedImport], List[ParsedClass], List[ParsedFunction]]:
        imports = []
        classes = []
        functions = []

        lines = content.splitlines()
        for idx, line in enumerate(lines, 1):
            line_str = line.strip()
            # Imports
            if line_str.startswith("import "):
                mod = line_str.replace("import ", "").split()[0].rstrip(",")
                imports.append(ParsedImport(module=mod, imported_names=[mod]))
            elif line_str.startswith("from "):
                match = re.match(r"from\s+([.\w]+)\s+import\s+(.*)", line_str)
                if match:
                    mod, names_str = match.groups()
                    names = [n.strip() for n in names_str.split(",") if n.strip()]
                    imports.append(ParsedImport(module=mod, imported_names=names, is_relative=mod.startswith(".")))

            # Classes
            cls_match = re.match(r"class\s+([A-Za-z0-9_]+)(\(.*?\))?:", line_str)
            if cls_match:
                classes.append(ParsedClass(name=cls_match.group(1), docstring="", line_number=idx))

            # Functions
            fn_match = re.match(r"(async\s+)?def\s+([A-Za-z0-9_]+)\s*\((.*?)\):", line_str)
            if fn_match:
                prefix = "async " if fn_match.group(1) else ""
                name = fn_match.group(2)
                args = fn_match.group(3)
                functions.append(ParsedFunction(name=name, signature=f"{prefix}def {name}({args})", docstring="", line_number=idx))

        return imports, classes, functions

    # -------------------------------------------------------------
    # JavaScript & TypeScript Parser (Regex / Pattern matching)
    # -------------------------------------------------------------
    @classmethod
    def _parse_javascript(cls, content: str) -> Tuple[List[ParsedImport], List[ParsedClass], List[ParsedFunction]]:
        imports: List[ParsedImport] = []
        classes: List[ParsedClass] = []
        functions: List[ParsedFunction] = []

        lines = content.splitlines()
        for idx, line in enumerate(lines, 1):
            line_str = line.strip()

            # ES6 Imports: import X from 'y' or import { a, b } from './z'
            imp_match = re.search(r"import\s+(?:\{([^}]+)\}|([A-Za-z0-9_]+))\s+from\s+['\"]([^'\"]+)['\"]", line_str)
            if imp_match:
                bracket_names = imp_match.group(1)
                default_name = imp_match.group(2)
                module_path = imp_match.group(3)
                names = [n.strip() for n in bracket_names.split(",")] if bracket_names else ([default_name] if default_name else [])
                imports.append(ParsedImport(module=module_path, imported_names=names, is_relative=module_path.startswith(".")))

            # CommonJS require: const x = require('./y')
            req_match = re.search(r"(?:const|let|var)\s+(?:\{([^}]+)\}|([A-Za-z0-9_]+))\s*=\s*require\(['\"]([^'\"]+)['\"]\)", line_str)
            if req_match:
                bracket_names = req_match.group(1)
                var_name = req_match.group(2)
                module_path = req_match.group(3)
                names = [n.strip() for n in bracket_names.split(",")] if bracket_names else ([var_name] if var_name else [])
                imports.append(ParsedImport(module=module_path, imported_names=names, is_relative=module_path.startswith(".")))

            # Classes
            cls_match = re.match(r"(?:export\s+)?(?:default\s+)?class\s+([A-Za-z0-9_]+)", line_str)
            if cls_match:
                classes.append(ParsedClass(name=cls_match.group(1), docstring="", line_number=idx))

            # Functions: function foo(x) or const foo = (x) => or export const foo = async (x) =>
            fn_match = re.match(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z0-9_]+)\s*\((.*?)\)", line_str)
            if fn_match:
                name = fn_match.group(1)
                args = fn_match.group(2)
                functions.append(ParsedFunction(name=name, signature=f"function {name}({args})", docstring="", line_number=idx))
            else:
                arrow_match = re.match(r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_]+)\s*=\s*(?:async\s*)?\((.*?)\)\s*=>", line_str)
                if arrow_match:
                    name = arrow_match.group(1)
                    args = arrow_match.group(2)
                    functions.append(ParsedFunction(name=name, signature=f"const {name} = ({args}) =>", docstring="", line_number=idx))

            # HTTP Routes: router.get('/path', ...), app.post('/path', ...)
            route_match = re.search(r"(?:app|router)\.(get|post|put|delete|patch|options)\s*\(\s*['\"]([^'\"]+)['\"]", line_str, re.IGNORECASE)
            if route_match:
                method = route_match.group(1).upper()
                path = route_match.group(2)
                functions.append(ParsedFunction(
                    name=f"{method} {path}",
                    signature=f"{method} {path}",
                    docstring="",
                    line_number=idx,
                    is_route=True,
                    http_method=method,
                    http_path=path
                ))

        return imports, classes, functions

    # -------------------------------------------------------------
    # Go Parser
    # -------------------------------------------------------------
    @classmethod
    def _parse_go(cls, content: str) -> Tuple[List[ParsedImport], List[ParsedClass], List[ParsedFunction]]:
        imports: List[ParsedImport] = []
        classes: List[ParsedClass] = []
        functions: List[ParsedFunction] = []

        lines = content.splitlines()
        in_import_block = False

        for idx, line in enumerate(lines, 1):
            line_str = line.strip()

            if line_str == "import (":
                in_import_block = True
                continue
            if in_import_block:
                if line_str == ")":
                    in_import_block = False
                    continue
                match = re.search(r"['\"]([^'\"]+)['\"]", line_str)
                if match:
                    mod = match.group(1)
                    imports.append(ParsedImport(module=mod, imported_names=[mod.split("/")[-1]]))
                continue

            # Single line import
            single_imp = re.match(r"import\s+['\"]([^'\"]+)['\"]", line_str)
            if single_imp:
                mod = single_imp.group(1)
                imports.append(ParsedImport(module=mod, imported_names=[mod.split("/")[-1]]))

            # Structs / Interfaces as classes
            type_match = re.match(r"type\s+([A-Za-z0-9_]+)\s+(struct|interface)", line_str)
            if type_match:
                classes.append(ParsedClass(name=type_match.group(1), docstring="", line_number=idx))

            # Functions: func (s *Service) Method(args) or func Handler(args)
            fn_match = re.match(r"func\s+(?:\((.*?)\)\s+)?([A-Za-z0-9_]+)\s*\((.*?)\)(?:\s*(.+))?", line_str)
            if fn_match:
                receiver = fn_match.group(1)
                name = fn_match.group(2)
                args = fn_match.group(3)
                ret = fn_match.group(4) or ""
                rec_prefix = f"({receiver}) " if receiver else ""
                sig = f"func {rec_prefix}{name}({args}) {ret}".strip()
                functions.append(ParsedFunction(name=name, signature=sig, docstring="", line_number=idx))

        return imports, classes, functions

    # -------------------------------------------------------------
    # Java Parser
    # -------------------------------------------------------------
    @classmethod
    def _parse_java(cls, content: str) -> Tuple[List[ParsedImport], List[ParsedClass], List[ParsedFunction]]:
        imports: List[ParsedImport] = []
        classes: List[ParsedClass] = []
        functions: List[ParsedFunction] = []

        lines = content.splitlines()
        for idx, line in enumerate(lines, 1):
            line_str = line.strip()

            if line_str.startswith("import "):
                mod = line_str.replace("import ", "").rstrip(";").strip()
                imports.append(ParsedImport(module=mod, imported_names=[mod.split(".")[-1]]))

            cls_match = re.match(r"(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?(?:class|interface|enum)\s+([A-Za-z0-9_]+)", line_str)
            if cls_match:
                classes.append(ParsedClass(name=cls_match.group(1), docstring="", line_number=idx))

            # Java Method: public String getName(int id) {
            method_match = re.match(r"(?:public|protected|private)\s+(?:static\s+)?([A-Za-z0-9_<>[\]]+)\s+([A-Za-z0-9_]+)\s*\((.*?)\)", line_str)
            if method_match and not any(kw in line_str for kw in ["class", "interface", "enum", "if", "for", "while"]):
                ret_type = method_match.group(1)
                name = method_match.group(2)
                params = method_match.group(3)
                functions.append(ParsedFunction(name=name, signature=f"{ret_type} {name}({params})", docstring="", line_number=idx))

        return imports, classes, functions

    # -------------------------------------------------------------
    # Ruby Parser
    # -------------------------------------------------------------
    @classmethod
    def _parse_ruby(cls, content: str) -> Tuple[List[ParsedImport], List[ParsedClass], List[ParsedFunction]]:
        imports: List[ParsedImport] = []
        classes: List[ParsedClass] = []
        functions: List[ParsedFunction] = []

        lines = content.splitlines()
        for idx, line in enumerate(lines, 1):
            line_str = line.strip()

            req_match = re.match(r"(?:require|require_relative)\s+['\"]([^'\"]+)['\"]", line_str)
            if req_match:
                mod = req_match.group(1)
                is_rel = line_str.startswith("require_relative")
                imports.append(ParsedImport(module=mod, imported_names=[mod], is_relative=is_rel))

            cls_match = re.match(r"class\s+([A-Za-z0-9_:]+)", line_str)
            if cls_match:
                classes.append(ParsedClass(name=cls_match.group(1), docstring="", line_number=idx))

            fn_match = re.match(r"def\s+([A-Za-z0-9_!?=.]+)(?:\s*\((.*?)\))?", line_str)
            if fn_match:
                name = fn_match.group(1)
                params = fn_match.group(2) or ""
                functions.append(ParsedFunction(name=name, signature=f"def {name}({params})", docstring="", line_number=idx))

        return imports, classes, functions

    # -------------------------------------------------------------
    # Generic Parser
    # -------------------------------------------------------------
    @classmethod
    def _parse_generic(cls, content: str) -> Tuple[List[ParsedImport], List[ParsedClass], List[ParsedFunction]]:
        imports: List[ParsedImport] = []
        classes: List[ParsedClass] = []
        functions: List[ParsedFunction] = []

        lines = content.splitlines()
        for idx, line in enumerate(lines, 1):
            line_str = line.strip()
            if line_str.startswith(("#include", "using", "use ")):
                imports.append(ParsedImport(module=line_str, imported_names=[]))
        return imports, classes, functions

    @classmethod
    def chunk_content(cls, content: str, max_chunk_lines: int = 150) -> List[str]:
        """
        Split large file into logical chunks at function/class boundaries
        so it fits comfortably within Granite context windows.
        """
        lines = content.splitlines()
        if len(lines) <= max_chunk_lines:
            return [content]

        chunks = []
        current_chunk: List[str] = []

        for line in lines:
            # Check if this line looks like a top-level boundary
            is_boundary = bool(re.match(r"^(class|def|async def|function|export|func|public|type)\s+", line.strip()))
            
            if is_boundary and len(current_chunk) >= max_chunk_lines:
                chunks.append("\n".join(current_chunk))
                current_chunk = [line]
            else:
                current_chunk.append(line)

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        return chunks


code_parser = CodeParser()
