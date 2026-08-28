import re
import logging
from pathlib import Path
from typing import List, Dict, Any, Set, Tuple, Optional
from app.models.schemas import ArchitectureResult, SubsystemCluster, DependencyEdge
from app.services.code_parser import ParsedFileResult

logger = logging.getLogger(__name__)

SUBSYSTEM_PATTERNS = [
    ("API & Controllers", [r"\b(router|routes|controller|handler|api|endpoint|views)\b"]),
    ("Core Business Services", [r"\b(service|manager|engine|worker|core|logic|pipeline)\b"]),
    ("Data & Models", [r"\b(model|schema|entity|db|database|repository|dao|cache|store)\b"]),
    ("Utilities & Helpers", [r"\b(util|helper|common|lib|pkg|tool|client|connector)\b"]),
    ("UI & Frontend", [r"\b(component|ui|frontend|page|layout|hook|style|view)\b"]),
    ("Entry & Configuration", [r"^(main|app|server|index|config|settings|run)(\.[a-z0-9]+)?$"]),
]


class ArchitectureService:
    @staticmethod
    def _sanitize_node_id(path: str) -> str:
        """Convert a file path to a valid Mermaid node identifier."""
        clean = re.sub(r"[^a-zA-Z0-9_]", "_", path)
        return f"node_{clean}"

    @classmethod
    def _resolve_internal_target(cls, source_path: str, import_module: str, all_files: Set[str]) -> Optional[str]:
        """Try to resolve an import module to an existing repository file path."""
        clean_mod = import_module.lstrip("./").replace(".", "/")
        
        for f in all_files:
            f_stem = Path(f).with_suffix("").as_posix()
            if f.endswith(clean_mod) or f_stem.endswith(clean_mod) or f == clean_mod:
                return f

        source_dir = Path(source_path).parent
        candidate = (source_dir / clean_mod).as_posix()
        for f in all_files:
            f_stem = Path(f).with_suffix("").as_posix()
            if f == candidate or f_stem == candidate:
                return f

        return None

    @classmethod
    def cluster_files(cls, parsed_files: List[ParsedFileResult]) -> List[SubsystemCluster]:
        """Group files into architecture subsystems."""
        clusters_map: Dict[str, List[str]] = {name: [] for name, _ in SUBSYSTEM_PATTERNS}
        clusters_map["General Modules"] = []

        for pf in parsed_files:
            rel_lower = pf.file_path.lower()
            file_name = Path(pf.file_path).name.lower()
            assigned = False

            # Check specific subsystem directory/path patterns first
            for cluster_name, patterns in SUBSYSTEM_PATTERNS:
                if cluster_name == "Entry & Configuration":
                    if any(re.search(pat, file_name) for pat in patterns):
                        clusters_map[cluster_name].append(pf.file_path)
                        assigned = True
                        break
                else:
                    if any(re.search(pat, rel_lower) for pat in patterns):
                        clusters_map[cluster_name].append(pf.file_path)
                        assigned = True
                        break

            if not assigned:
                clusters_map["General Modules"].append(pf.file_path)

        clusters: List[SubsystemCluster] = []
        for name, files in clusters_map.items():
            if files:
                desc = f"Contains {len(files)} module(s) governing {name.lower()}."
                clusters.append(SubsystemCluster(name=name, description=desc, files=files))

        return clusters

    @classmethod
    def build_architecture(cls, parsed_files: List[ParsedFileResult]) -> ArchitectureResult:
        """Construct dependency graph and generate Mermaid diagram."""
        all_file_paths = {pf.file_path for pf in parsed_files}
        dependencies: List[DependencyEdge] = []
        internal_edges: Set[Tuple[str, str]] = set()

        for pf in parsed_files:
            for imp in pf.imports:
                target_file = cls._resolve_internal_target(pf.file_path, imp.module, all_file_paths)
                is_internal = bool(target_file and target_file != pf.file_path)

                dependencies.append(DependencyEdge(
                    source_file=pf.file_path,
                    target_module=target_file or imp.module,
                    import_names=imp.imported_names,
                    is_internal=is_internal
                ))

                if is_internal and target_file:
                    internal_edges.add((pf.file_path, target_file))

        # Cluster into subsystems
        clusters = cls.cluster_files(parsed_files)

        # Build Mermaid graph
        mermaid_lines = [
            "graph TD",
            "  %% Styling definitions",
            "  classDef default fill:#1e293b,stroke:#475569,stroke-width:1px,color:#f8fafc;",
            "  classDef entryNode fill:#1e3a8a,stroke:#3b82f6,stroke-width:2px,color:#ffffff,font-weight:bold;",
            "  classDef apiNode fill:#14532d,stroke:#22c55e,stroke-width:1px,color:#ffffff;",
            "  classDef serviceNode fill:#312e81,stroke:#6366f1,stroke-width:1px,color:#ffffff;",
            "  classDef dataNode fill:#701a75,stroke:#d946ef,stroke-width:1px,color:#ffffff;",
            "  classDef utilNode fill:#334155,stroke:#64748b,stroke-width:1px,color:#e2e8f0;",
            ""
        ]

        # Generate subgraphs
        for idx, cluster in enumerate(clusters, 1):
            sub_id = f"sub_{idx}"
            mermaid_lines.append(f'  subgraph {sub_id} ["{cluster.name}"]')
            for f in cluster.files:
                node_id = cls._sanitize_node_id(f)
                basename = Path(f).name
                display_label = f"{f}" if len(f) <= 28 else f".../{basename}"
                mermaid_lines.append(f'    {node_id}["{display_label}"]')
            mermaid_lines.append("  end")
            mermaid_lines.append("")

        # Add directed dependency connections (internal edges)
        if internal_edges:
            mermaid_lines.append("  %% Dependencies between modules")
            for src, tgt in sorted(internal_edges):
                src_id = cls._sanitize_node_id(src)
                tgt_id = cls._sanitize_node_id(tgt)
                mermaid_lines.append(f"  {src_id} --> {tgt_id}")
        else:
            # If no internal import links detected, connect logically by cluster flow
            if len(clusters) > 1:
                mermaid_lines.append("  %% Subsystem hierarchy")
                for i in range(len(clusters) - 1):
                    if clusters[i].files and clusters[i+1].files:
                        src_id = cls._sanitize_node_id(clusters[i].files[0])
                        tgt_id = cls._sanitize_node_id(clusters[i+1].files[0])
                        mermaid_lines.append(f"  {src_id} -.-> {tgt_id}")

        raw_mermaid = "\n".join(mermaid_lines)

        return ArchitectureResult(
            mermaid_diagram=raw_mermaid,
            raw_mermaid=raw_mermaid,
            subsystems=clusters,
            dependencies=dependencies,
            total_modules=len(parsed_files),
            total_dependencies=len(internal_edges)
        )


architecture_service = ArchitectureService()
