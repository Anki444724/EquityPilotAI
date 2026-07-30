"""Knowledge graph over extracted entities.

The graph answers questions the chunk index cannot: which subsidiaries does
this group hold, who sits on the board of both these companies, which risks are
named across every filing in the sector. Those are traversals, not searches.

Design choices worth stating:

* **Edges carry pages.** An edge is a claim about the world, and a claim needs
  evidence. Every edge records the pages on which the relationship was
  observed, so a graph answer can cite like any other answer.
* **Identity is the normalised name.** "Acme Ltd.", "ACME Limited" and "Acme"
  are one node. This over-merges occasionally — two genuinely different
  companies with the same short name would collide — which is recorded as a
  known limitation rather than papered over.
* **Nothing is inferred transitively.** If A is a subsidiary of B and B of C,
  the graph does not assert A is a subsidiary of C. It may well be, but the
  document did not say so, and a graph that invents edges is a graph that
  fabricates evidence.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.domain.documents.types import (
    EntityKind, ExtractedEntity, GraphEdge, GraphNode, RelationKind,
    normalise_entity,
)

#: Which relation an entity kind implies with respect to the subject company.
#: The subject is always the *source* of the edge, so direction is consistent.
_RELATION_FOR_KIND: dict[EntityKind, RelationKind] = {
    EntityKind.SUBSIDIARY: RelationKind.SUBSIDIARY_OF,
    EntityKind.PROMOTER: RelationKind.PROMOTER_OF,
    EntityKind.DIRECTOR: RelationKind.DIRECTOR_OF,
    EntityKind.COMPETITOR: RelationKind.COMPETES_WITH,
    EntityKind.SUPPLIER: RelationKind.SUPPLIES_TO,
    EntityKind.CUSTOMER: RelationKind.CUSTOMER_OF,
    EntityKind.PRODUCT: RelationKind.SELLS_PRODUCT,
    EntityKind.SEGMENT: RelationKind.OPERATES_SEGMENT,
    EntityKind.COUNTRY: RelationKind.OPERATES_IN,
    EntityKind.RISK: RelationKind.EXPOSED_TO_RISK,
    EntityKind.ACQUISITION: RelationKind.ACQUIRED,
    EntityKind.AUDITOR: RelationKind.AUDITED_BY,
    EntityKind.GUIDANCE: RelationKind.GUIDES,
    EntityKind.CAPEX: RelationKind.INVESTS_IN,
    EntityKind.DEBT: RelationKind.INVESTS_IN,
}

#: Relations whose natural direction runs *into* the subject company.
_INBOUND = {
    RelationKind.SUBSIDIARY_OF,
    RelationKind.PROMOTER_OF,
    RelationKind.DIRECTOR_OF,
    RelationKind.SUPPLIES_TO,
}


def node_key(kind: EntityKind, name: str) -> str:
    """Stable node identity: kind plus normalised name.

    Kind is part of the key because a person and a company can share a name,
    and merging "Mr Tata" the director with "Tata" the company would be worse
    than keeping two nodes.
    """
    return f"{kind.value}:{normalise_entity(name)}"


@dataclass(slots=True)
class KnowledgeGraph:
    """An adjacency-list graph with evidence on every edge."""

    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: dict[tuple[str, str, RelationKind], GraphEdge] = field(default_factory=dict)

    # -- construction --------------------------------------------------
    def add_node(
        self, kind: EntityKind, label: str, *, weight: float = 1.0, **attributes: str
    ) -> str:
        key = node_key(kind, label)
        existing = self.nodes.get(key)
        if existing is None:
            self.nodes[key] = GraphNode(
                key=key, kind=kind, label=label, weight=weight,
                attributes={k: str(v) for k, v in attributes.items()},
            )
        else:
            # Repeated observation strengthens a node without ever making it
            # certain; the label already recorded is kept as canonical.
            existing.weight += weight
            existing.attributes.update({k: str(v) for k, v in attributes.items()})
        return key

    def add_edge(
        self,
        source: str,
        target: str,
        relation: RelationKind,
        *,
        pages: Sequence[int] = (),
        confidence: float = 0.5,
        weight: float = 1.0,
    ) -> GraphEdge:
        key = (source, target, relation)
        edge = self.edges.get(key)
        if edge is None:
            edge = GraphEdge(
                source=source, target=target, relation=relation,
                weight=weight, pages=sorted(set(pages)), confidence=confidence,
            )
            self.edges[key] = edge
            return edge
        edge.weight += weight
        edge.pages = sorted(set(edge.pages) | set(pages))
        edge.confidence = max(edge.confidence, confidence)
        return edge

    # -- queries -------------------------------------------------------
    def neighbours(
        self, key: str, *, relation: RelationKind | None = None
    ) -> list[tuple[GraphNode, GraphEdge]]:
        """Adjacent nodes in either direction — the graph is queried undirected."""
        out: list[tuple[GraphNode, GraphEdge]] = []
        for (source, target, kind), edge in self.edges.items():
            if relation is not None and kind is not relation:
                continue
            if source == key and target in self.nodes:
                out.append((self.nodes[target], edge))
            elif target == key and source in self.nodes:
                out.append((self.nodes[source], edge))
        out.sort(key=lambda pair: (-pair[1].confidence, -pair[1].weight, pair[0].label))
        return out

    def nodes_of_kind(self, kind: EntityKind) -> list[GraphNode]:
        found = [n for n in self.nodes.values() if n.kind is kind]
        found.sort(key=lambda n: (-n.weight, n.label))
        return found

    def degree(self, key: str) -> int:
        return sum(1 for (s, t, _) in self.edges if s == key or t == key)

    def central_nodes(self, limit: int = 10) -> list[GraphNode]:
        """Highest-degree nodes — a crude but useful 'what is this document about'."""
        ranked = sorted(
            self.nodes.values(),
            key=lambda n: (-self.degree(n.key), -n.weight, n.label),
        )
        return ranked[:limit]

    def relation_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for (_, _, relation) in self.edges:
            counts[relation.value] += 1
        return dict(sorted(counts.items()))

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    def to_dict(self) -> dict[str, object]:
        """Serialisable form for the API and the UI's graph view."""
        return {
            "nodes": [
                {
                    "key": n.key, "kind": n.kind.value, "label": n.label,
                    "weight": round(n.weight, 3), "degree": self.degree(n.key),
                    "attributes": n.attributes,
                }
                for n in sorted(self.nodes.values(), key=lambda n: (-self.degree(n.key), n.label))
            ],
            "edges": [
                {
                    "source": e.source, "target": e.target,
                    "relation": e.relation.value, "weight": round(e.weight, 3),
                    "pages": e.pages, "confidence": round(e.confidence, 4),
                }
                for e in sorted(self.edges.values(), key=lambda e: -e.confidence)
            ],
            "stats": {
                "nodes": self.node_count,
                "edges": self.edge_count,
                "relations": self.relation_counts(),
            },
        }


class KnowledgeGraphBuilder:
    """Turns extracted entities into a graph anchored on the subject company."""

    def __init__(self, company_name: str, company_ticker: str | None = None) -> None:
        self.company_name = company_name
        self.company_ticker = company_ticker
        self.graph = KnowledgeGraph()
        self.subject = self.graph.add_node(
            EntityKind.COMPANY, company_name, weight=5.0,
            ticker=company_ticker or "", subject="true",
        )

    def add_entities(
        self, entities: Iterable[ExtractedEntity], *, document_id: int | None = None
    ) -> KnowledgeGraph:
        for entity in entities:
            if entity.kind is EntityKind.COMPANY and entity.normalised == \
                    normalise_entity(self.company_name):
                continue
            relation = _RELATION_FOR_KIND.get(entity.kind)
            if relation is None:
                continue

            attributes = dict(entity.attributes)
            if document_id is not None:
                attributes["document_id"] = str(document_id)
            key = self.graph.add_node(
                entity.kind, entity.name, weight=entity.confidence, **attributes
            )
            # Direction follows the relation's natural reading, so an edge can
            # be rendered as a sentence without the UI having to special-case.
            if relation in _INBOUND:
                source, target = key, self.subject
            else:
                source, target = self.subject, key
            self.graph.add_edge(
                source, target, relation,
                pages=[entity.page], confidence=entity.confidence,
                weight=entity.confidence,
            )
        return self.graph

    def merge(self, other: KnowledgeGraph) -> KnowledgeGraph:
        """Fold another graph in — how a company's graph spans many documents."""
        for key, node in other.nodes.items():
            existing = self.graph.nodes.get(key)
            if existing is None:
                self.graph.nodes[key] = node
            else:
                existing.weight += node.weight
                existing.attributes.update(node.attributes)
        for key, edge in other.edges.items():
            self.graph.add_edge(
                edge.source, edge.target, edge.relation,
                pages=edge.pages, confidence=edge.confidence, weight=edge.weight,
            )
        return self.graph
