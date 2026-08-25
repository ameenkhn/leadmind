"""Data quality scoring.

Deterministic, config-driven, and fully explained. Every factor's raw value, weight and
contribution is persisted with the score, so the "Why 87?" panel in the UI reads stored facts
rather than re-deriving them — and so a score computed under rubric v1.0 stays interpretable
after v1.1 ships.

There is no LLM anywhere in this module, and there should not be. Asking a language model to
rate data completeness would be slower, non-reproducible, unexplainable, and worse than
counting fields.

**Unmeasured is not zero.** A factor that cannot be evaluated — mailbox reachability before
verification has run, website liveness for a lead with no website — returns ``None`` and drops
out of both the numerator and the denominator. Scoring it zero would punish leads for work the
operator has not done yet, and scoring it 0.5 would invent a measurement. The number of factors
actually evaluated is stored with the score, so a partially-measured 80 is visibly different
from a fully-measured one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.config import get_settings
from app.core.errors import ConfigurationError
from app.ingestion.normalizers.category import UNCLASSIFIED
from app.ingestion.normalizers.email import FREEMAIL_DOMAINS
from app.ingestion.resolution.merge import MergedLead
from app.models.enums import IdentifierKind
from app.verification.types import VerificationStatus

_PENALTY_CODES: dict[str, str] = {
    "email_domain_website_mismatch": "duplicate_email_domain_mismatch",
    "thin_record": "thin_record",
    "name_placeholder": "placeholder_name",
    "zero_followers": "zero_followers",
    "email_typo_domain_corrected": "email_typo_corrected",
    "city_address_fragment": "city_unusable",
    "city_not_a_place": "city_unusable",
}

Factor = tuple[float | None, str]


@dataclass(frozen=True, slots=True)
class VerificationSignals:
    """What Phase 1b measured about one lead. All fields optional: absent means unmeasured."""

    mailbox_domain_status: VerificationStatus | None = None
    mailbox_accepts_mail: bool = False
    mail_provider: str | None = None
    website_status: VerificationStatus | None = None
    website_is_live: bool = False
    website_is_parked: bool = False

    @classmethod
    def from_mapping(cls, payload: dict[str, Any] | None) -> VerificationSignals:
        if not payload:
            return cls()

        def status(key: str) -> VerificationStatus | None:
            raw = payload.get(key)
            return VerificationStatus(raw) if raw else None

        return cls(
            mailbox_domain_status=status("mailbox_domain_status"),
            mailbox_accepts_mail=bool(payload.get("mailbox_accepts_mail")),
            mail_provider=payload.get("mail_provider"),
            website_status=status("website_status"),
            website_is_live=bool(payload.get("website_is_live")),
            website_is_parked=bool(payload.get("website_is_parked")),
        )


@dataclass(frozen=True, slots=True)
class Rubric:
    version: str
    weights: dict[str, float]
    descriptions: dict[str, str]
    penalties: dict[str, float]
    penalty_descriptions: dict[str, str]
    max_total_penalty: float
    followers_reference: float
    channel_kinds: tuple[IdentifierKind, ...]
    managed_mail_providers: frozenset[str]

    @property
    def total_weight(self) -> float:
        return sum(self.weights.values())


@dataclass(slots=True)
class QualityScore:
    score: float
    rubric_version: str
    factors: dict[str, Any] = field(default_factory=dict)

    @property
    def rounded(self) -> int:
        return round(self.score)

    @property
    def factors_evaluated(self) -> int:
        return int(self.factors.get("factors_evaluated", 0))


def load_rubric(path: Path | None = None) -> Rubric:
    source = path or get_settings().config_file("quality.yaml")
    data: dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8"))

    factors = data.get("factors") or {}
    if not factors:
        raise ConfigurationError("quality rubric defines no factors", file=str(source))

    parameters = data.get("parameters") or {}
    try:
        channels = tuple(IdentifierKind(name) for name in parameters.get("channel_kinds") or [])
    except ValueError as exc:
        raise ConfigurationError(
            "quality rubric references an unknown identifier kind", file=str(source)
        ) from exc

    penalties = data.get("penalties") or {}
    return Rubric(
        version=str(data.get("version", "0")),
        weights={name: float(spec["weight"]) for name, spec in factors.items()},
        descriptions={name: str(spec.get("description", "")) for name, spec in factors.items()},
        penalties={name: float(spec["value"]) for name, spec in penalties.items()},
        penalty_descriptions={
            name: str(spec.get("description", "")) for name, spec in penalties.items()
        },
        max_total_penalty=float(data.get("max_total_penalty", 100)),
        followers_reference=float(parameters.get("followers_reference", 100_000)),
        channel_kinds=channels,
        managed_mail_providers=frozenset(parameters.get("managed_mail_providers") or []),
    )


@lru_cache(maxsize=4)
def get_rubric(path: Path | None = None) -> Rubric:
    return load_rubric(path)


def _contact_factor(lead: MergedLead) -> Factor:
    email = lead.identifier(IdentifierKind.EMAIL)
    phone = lead.identifier(IdentifierKind.PHONE)
    if email is None and phone is None:
        return 0.0, "no email or phone"
    if email is None:
        return 0.5, "phone only"

    domain = str(email.attributes.get("domain", ""))
    is_free = bool(email.attributes.get("is_freemail")) or domain in FREEMAIL_DOMAINS
    is_role = bool(email.attributes.get("is_role_based"))

    if not is_free and not is_role:
        return 1.0, "corporate domain, personally addressed"
    if not is_free and is_role:
        return 0.8, "corporate domain, role account"
    if is_free and not is_role:
        return 0.6, "free provider, personally addressed"
    return 0.45, "free provider, role account"


def _audience_factor(lead: MergedLead, rubric: Rubric) -> Factor:
    followers = lead.latest_followers
    if followers is None:
        return 0.0, "no follower count"
    if followers <= 0:
        return 0.0, "zero followers"
    ratio = math.log1p(followers) / math.log1p(rubric.followers_reference)
    return min(1.0, ratio), f"{followers:,} followers (log-scaled)"


def _mailbox_factor(lead: MergedLead, signals: VerificationSignals, rubric: Rubric) -> Factor:
    if lead.identifier(IdentifierKind.EMAIL) is None:
        return None, "no email address to verify"
    status = signals.mailbox_domain_status
    if status is None or not status.is_measurement:
        return None, "mailbox domain not verified yet"
    if not signals.mailbox_accepts_mail:
        return 0.0, "domain publishes no MX record; mail cannot be delivered"
    if signals.mail_provider in rubric.managed_mail_providers:
        return 1.0, f"MX confirmed, managed by {signals.mail_provider}"
    return 0.85, "MX confirmed"


def _website_live_factor(lead: MergedLead, signals: VerificationSignals) -> Factor:
    if not lead.company_domain:
        return None, "no owned website to check"
    status = signals.website_status
    if status is None or not status.is_measurement:
        return None, "website liveness not verified yet"
    if signals.website_is_parked:
        return 0.0, "parked or placeholder page, not a business website"
    if signals.website_is_live:
        return 1.0, "website answers"
    return 0.0, "website does not answer"


def score_lead(
    lead: MergedLead,
    *,
    rubric: Rubric | None = None,
    signals: VerificationSignals | None = None,
) -> QualityScore:
    """Compute a 0-100 data quality score and every reason behind it."""
    rules = rubric or get_rubric()
    measured = signals or VerificationSignals()

    channels_present = [kind for kind in rules.channel_kinds if lead.has(kind)]
    facebook = lead.identifier(IdentifierKind.FACEBOOK)
    numeric_only = bool(facebook and facebook.attributes.get("handle_is_numeric_id"))
    inferred_source = any(q.source_is_inferred for q in lead.source_queries)

    raw: dict[str, Factor] = {
        "identity_present": (
            0.0 if lead.is_placeholder_name else 1.0,
            "placeholder advertiser name" if lead.is_placeholder_name else "name present",
        ),
        "contact_reachable": _contact_factor(lead),
        "mailbox_verified": _mailbox_factor(lead, measured, rules),
        "owned_website": (
            1.0 if lead.company_domain else 0.0,
            f"owned domain {lead.company_domain}" if lead.company_domain else "no owned domain",
        ),
        "website_live": _website_live_factor(lead, measured),
        "channel_breadth": (
            len(channels_present) / len(rules.channel_kinds) if rules.channel_kinds else 0.0,
            f"{len(channels_present)}/{len(rules.channel_kinds)} channels present",
        ),
        "audience_evidence": _audience_factor(lead, rules),
        "location_resolved": (
            lead.location_confidence or 0.0,
            f"resolved to {lead.location.name}" if lead.location else "location unresolved",
        ),
        "category_known": (
            0.0 if lead.vertical_slug in (None, UNCLASSIFIED) else 1.0,
            f"vertical {lead.vertical_slug}"
            if lead.vertical_slug not in (None, UNCLASSIFIED)
            else "no controlled vertical",
        ),
        "identity_verifiable": (
            0.0 if numeric_only else 1.0,
            "numeric-only page ID" if numeric_only else "vanity handle present",
        ),
        "provenance_observed": (
            0.0 if inferred_source else 1.0,
            "source inferred, not observed" if inferred_source else "source observed",
        ),
    }

    factor_detail: dict[str, Any] = {}
    earned = 0.0
    available = 0.0
    evaluated = 0
    for name, weight in rules.weights.items():
        value, reason = raw.get(name, (None, "factor not computed"))
        if value is None:
            factor_detail[name] = {
                "value": None,
                "weight": weight,
                "contribution": 0.0,
                "reason": reason,
                "measured": False,
            }
            continue
        contribution = value * weight
        earned += contribution
        available += weight
        evaluated += 1
        factor_detail[name] = {
            "value": round(value, 4),
            "weight": weight,
            "contribution": round(contribution, 3),
            "reason": reason,
            "measured": True,
        }

    base = (earned / available) * 100 if available else 0.0

    applied: dict[str, Any] = {}
    penalty_total = 0.0

    def apply(name: str, trigger: str) -> None:
        nonlocal penalty_total
        amount = rules.penalties.get(name)
        if amount is None or name in applied:
            return
        applied[name] = {
            "amount": amount,
            "triggered_by": trigger,
            "reason": rules.penalty_descriptions.get(name, ""),
        }
        penalty_total += amount

    for code in sorted({issue.code for issue in lead.issues}):
        mapped = _PENALTY_CODES.get(code)
        if mapped:
            apply(mapped, code)

    if raw["mailbox_verified"][0] == 0.0:
        apply("mailbox_domain_dead", "mx_absent")
    if raw["website_live"][0] == 0.0:
        apply("website_dead", "website_parked" if measured.website_is_parked else "website_down")

    capped = min(penalty_total, rules.max_total_penalty)
    score = max(0.0, min(100.0, base - capped))

    return QualityScore(
        score=round(score, 2),
        rubric_version=rules.version,
        factors={
            "base_score": round(base, 2),
            "penalty_total": round(penalty_total, 2),
            "penalty_applied": round(capped, 2),
            "factors_evaluated": evaluated,
            "factors_total": len(rules.weights),
            "weight_available": available,
            "factors": factor_detail,
            "penalties": applied,
        },
    )
