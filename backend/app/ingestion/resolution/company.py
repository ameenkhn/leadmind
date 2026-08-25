"""Company resolution.

A company is keyed on the registrable domain of an *owned* website. Link aggregators, social
profiles and shorteners never key a company — thousands of unrelated businesses share
``linktr.ee``, and using it as an identity would merge strangers into one organisation.

The relationship is one-to-many by design, and it is the mechanism that lets a shared website be
recorded as a fact without being treated as a duplicate. Five Pumo Technovation branches become
one company with five leads attached; nothing is lost and the relationship is queryable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.ingestion.resolution.merge import MergedLead

logger = get_logger(__name__)


@dataclass(slots=True)
class ResolvedCompany:
    domain: str
    name: str | None
    website_url: str | None
    lead_indexes: list[int] = field(default_factory=list)

    @property
    def branch_count(self) -> int:
        return len(self.lead_indexes)

    @property
    def is_multi_branch(self) -> bool:
        return self.branch_count > 1


def _shared_prefix(names: list[str]) -> str | None:
    """Longest common leading word sequence, used to name a multi-branch company.

    ``Pumo Technovation Kanchipuram`` + ``Pumo Technovation Tirupati`` yields
    ``Pumo Technovation``. When branches share no prefix — and they often do not, because one
    branch writes ``Pumotechnovation`` as a single word — the caller falls back to the domain
    rather than inheriting one arbitrary branch's name.
    """
    if not names:
        return None
    token_lists = [name.split() for name in names]
    prefix: list[str] = []
    for position in range(min(len(tokens) for tokens in token_lists)):
        candidate = token_lists[0][position]
        if all(tokens[position].casefold() == candidate.casefold() for tokens in token_lists):
            prefix.append(candidate)
        else:
            break
    return " ".join(prefix) if prefix else None


def _name_from_domain(domain: str) -> str:
    """Derive a company label from its own domain.

    Used only when multiple branches share a domain but no common name prefix. This is a
    derivation from data the company itself published, not an invented name, and it beats
    picking one branch's name and presenting it as the parent's.
    """
    label = domain.split(".")[0].replace("-", " ").strip()
    return label.title() if label else domain


def resolve_companies(leads: list[MergedLead]) -> list[ResolvedCompany]:
    """Group leads by owned domain into companies."""
    by_domain: dict[str, list[int]] = defaultdict(list)
    for index, lead in enumerate(leads):
        if lead.company_domain:
            by_domain[lead.company_domain].append(index)

    companies: list[ResolvedCompany] = []
    for domain, lead_indexes in sorted(by_domain.items()):
        names = [leads[i].display_name for i in lead_indexes]
        name = names[0] if len(names) == 1 else _shared_prefix(names) or _name_from_domain(domain)
        website = next(
            (
                identifier.value
                for i in lead_indexes
                for identifier in leads[i].identifiers
                if identifier.attributes.get("is_owned_domain")
            ),
            None,
        )
        companies.append(
            ResolvedCompany(
                domain=domain, name=name, website_url=website, lead_indexes=lead_indexes
            )
        )

    multi = sum(1 for c in companies if c.is_multi_branch)
    logger.info(
        "companies_resolved",
        companies=len(companies),
        multi_branch=multi,
        leads_with_domain=sum(c.branch_count for c in companies),
    )
    return companies
