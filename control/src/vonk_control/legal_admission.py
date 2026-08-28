"""Fail-closed territorial license admission for immutable model authorities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_ISO_ALPHA2_JURISDICTIONS = frozenset(
    """AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL
    BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW
    CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF
    GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ
    IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU
    LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC
    NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE
    RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD
    TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU
    WF WS YE YT ZA ZM ZW""".split()  # noqa: SIM905 - auditable ISO table
) | {"EU"}
_EU_MEMBER_JURISDICTIONS = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }
)


@dataclass(frozen=True, slots=True)
class TerritorialAdmissionDecision:
    blocker: tuple[str, str] | None
    warning: tuple[str, str] | None


def operator_jurisdiction(value: str | None) -> str | None:
    """Validate one explicit ISO-style operator jurisdiction code.

    `EU` is the one regional pseudo-code. Operators in an EU member country may
    configure either their ISO alpha-2 country code or `EU`.
    """

    if value is None or value == "":
        return None
    if value != value.strip() or len(value) != 2 or not value.isascii():
        raise ValueError("operator jurisdiction must be one uppercase two-letter code")
    if not value.isalpha() or value != value.upper():
        raise ValueError("operator jurisdiction must be one uppercase two-letter code")
    if value not in _ISO_ALPHA2_JURISDICTIONS:
        raise ValueError("operator jurisdiction must be an ISO alpha-2 code or EU")
    return value


def territorial_admission(
    model_version: Mapping[str, object],
    configured_jurisdiction: str | None,
    *,
    operation: str,
) -> TerritorialAdmissionDecision:
    """Evaluate the model license for install or run admission."""

    if operation not in {"install", "run"}:
        raise ValueError("territorial admission operation is invalid")
    license_document = model_version.get("license")
    restrictions = (
        license_document.get("territorial_restrictions")
        if isinstance(license_document, Mapping)
        else None
    )
    if restrictions is None:
        return TerritorialAdmissionDecision(None, None)
    if not isinstance(restrictions, Mapping):
        raise TypeError("model territorial restrictions are invalid")
    denied = restrictions.get("denied_jurisdictions")
    notice = restrictions.get("notice")
    if (
        not isinstance(denied, list)
        or not denied
        or not all(isinstance(value, str) for value in denied)
        or not isinstance(notice, str)
        or not notice
    ):
        raise TypeError("model territorial restrictions are invalid")
    prefix = f"{operation}.license"
    if configured_jurisdiction is None:
        return TerritorialAdmissionDecision(
            (
                f"{prefix}_jurisdiction_required",
                (
                    "This model has territorial license restrictions. Configure "
                    "VONK_OPERATOR_JURISDICTION before installation or execution; "
                    "admission fails closed while it is unset."
                ),
            ),
            None,
        )
    jurisdiction = operator_jurisdiction(configured_jurisdiction)
    assert jurisdiction is not None
    matched = jurisdiction if jurisdiction in denied else None
    if matched is None and "EU" in denied and jurisdiction in _EU_MEMBER_JURISDICTIONS:
        matched = "EU"
    if matched is not None:
        return TerritorialAdmissionDecision(
            (
                f"{prefix}_territory_denied",
                (
                    f"{notice} Configured operator jurisdiction {jurisdiction} "
                    f"matches denied jurisdiction {matched}; {operation} is prohibited."
                ),
            ),
            None,
        )
    return TerritorialAdmissionDecision(
        None,
        (
            f"{prefix}_territory_checked",
            (
                "Territorial license restrictions were checked against configured "
                f"operator jurisdiction {jurisdiction}."
            ),
        ),
    )
