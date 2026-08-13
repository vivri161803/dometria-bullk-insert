"""Category definitions and hierarchy graph for Bulk Insert pipeline."""

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from bulk_insert.config import Settings
from bulk_insert.config import settings as _default_settings


class Level1Category(StrEnum):
    """Top-level category codes."""

    COMMERCIAL_ACTIVITIES = "commercial-activities"
    BUSINESSES = "businesses"
    REAL_ESTATE = "real-estate"


Level1Literal = Literal[
    "commercial-activities",
    "businesses",
    "real-estate",
]

CATEGORY_NAMES: dict[str, str] = {
    "commercial-activities": "Attività commerciali",
    "food-beverage": "Food & Beverage",
    "bar": "Bar",
    "cafe": "Caffetteria",
    "wine-bar": "Enoteca / Wine bar",
    "gastronomy": "Gastronomia",
    "ice-cream": "Gelateria",
    "bakery-pastry": "Panificio / Pasticceria",
    "pizzeria": "Pizzeria",
    "pub-brewery": "Pub / Birreria",
    "restaurant": "Ristorante",
    "trattoria-osteria": "Trattoria / Osteria",
    "entertainment-leisure": "Intrattenimento & Tempo Libero",
    "bowling": "Bowling",
    "betting-center": "Centro scommesse",
    "escape-room": "Escape room",
    "gaming-hall": "Sala giochi",
    "mobility-automotive-services": "Mobilità & Servizi Automotive",
    "car-workshop": "Autofficina",
    "car-wash": "Autolavaggio",
    "body-shop": "Carrozzeria",
    "dealership": "Concessionaria",
    "tire-center": "Gommista",
    "specialized-retail": "Retail Specializzato",
    "clothing": "Abbigliamento",
    "home-goods": "Articoli per la casa",
    "footwear": "Calzature",
    "newsstand": "Edicola",
    "consumer-electronics": "Elettronica di consumo",
    "jewelry": "Gioielleria",
    "optician": "Ottica",
    "perfumery": "Profumeria",
    "tobacco-shop": "Tabaccheria",
    "phone-shop": "Telefonia",
    "health-wellness": "Salute & Benessere",
    "physiotherapy-center": "Centro fisioterapico",
    "multi-specialist-medical-center": "Centro medico polispecialistico",
    "pharmacy": "Farmacia",
    "gym-fitness": "Palestra / fitness",
    "parapharmacy": "Parafarmacia",
    "medical-orthopedics": "Sanitaria / ortopedia",
    "personal-services": "Servizi alla Persona",
    "travel-agency": "Agenzia viaggi",
    "nail-center": "Centro unghie",
    "beauty": "Estetica",
    "laundry": "Lavanderia",
    "hairdresser": "Parrucchiere",
    "tailor": "Sartoria",
    "private-school-courses": "Scuola privata / corsi",
    "professional-services-public": "Servizi Professionali al Pubblico",
    "insurance-agency": "Agenzia assicurativa",
    "real-estate-agency": "Agenzia immobiliare",
    "caf-patronato": "CAF / Patronato",
    "retail-financial-services": "Servizi finanziari al dettaglio",
    "tourism-light-hospitality": "Turismo & Ricettività leggera",
    "guest-rooms": "Affittacamere",
    "bnb": "B&B",
    "hostel": "Ostello",
    "businesses": "Aziende e operazioni straordinarie",
    "company-macro-sector": "Macro Settore",
    "company-agro-food": "Agroindustria & Food",
    "company-environment-circular-esg": "Ambiente, Economia Circolare & ESG",
    "company-construction-infrastructure": "Costruzioni & Infrastrutture",
    "company-energy-utilities": "Energia & Utilities",
    "company-hospitality-tourism": "Hospitality & Turismo",
    "company-ict-media-technology": "ICT, Media & Tecnologia",
    "company-industry-manufacturing": "Industria & Manifattura",
    "company-logistics-transport": "Logistica & Trasporti",
    "company-retail-distribution": "Retail & Distribuzione",
    "company-healthcare-personal-services": "Sanità & Servizi alla Persona",
    "company-business-services": "Servizi alle Imprese",
    "company-special-other": "Settori Speciali / Altro",
    "real-estate": "Immobili",
    "specialist-destinations": "Destinazioni specialistiche",
    "buildable-area": "Area edificabile",
    "garage": "Autorimessa",
    "agricultural-business": "Azienda agricola",
    "care-home": "Casa di cura",
    "barracks": "Caserma",
    "sports-complex": "Complesso sportivo",
    "convent": "Convento",
    "senior-living": "Senior living",
    "bank-branch": "Sportello bancario",
    "beach-establishment": "Stabilimento balneare",
    "student-housing": "Student housing",
    "agricultural-land": "Terreno agricolo",
    "public-office": "Ufficio pubblico",
    "non-residential-real-estate": "Immobili non residenziali",
    "agriturismo": "Agriturismo",
    "camping": "Campeggio",
    "craft-shed": "Capannone artigianale",
    "commercial-shed": "Capannone commerciale",
    "industrial-shed": "Capannone industriale",
    "logistics-shed": "Capannone logistico",
    "shopping-center": "Centro commerciale",
    "hotel": "Hotel",
    "laboratory": "Laboratorio",
    "warehouse": "Magazzino",
    "shop": "Negozio",
    "outlet-park": "Outlet Park",
    "directional-building": "Palazzina direzionale",
    "residence": "Residence",
    "retail-park": "Retail Park",
    "office-unit": "Ufficio",
    "holiday-village": "Villaggio turistico",
    "prestige-residential-properties": "Immobili residenziali di prestigio",
    "apartment": "Appartamento",
    "penthouse": "Attico",
    "farmhouse": "Casale",
    "castle": "Castello",
    "palace": "Palazzo",
    "villa": "Villa",
}

# Tree structure: Level 1 -> Level 2 -> list of Level 3
CATEGORY_TREE: dict[str, dict[str, list[str]]] = {
    "commercial-activities": {
        "food-beverage": [
            "bar",
            "cafe",
            "wine-bar",
            "gastronomy",
            "ice-cream",
            "bakery-pastry",
            "pizzeria",
            "pub-brewery",
            "restaurant",
            "trattoria-osteria",
        ],
        "entertainment-leisure": ["bowling", "betting-center", "escape-room", "gaming-hall"],
        "mobility-automotive-services": [
            "car-workshop",
            "car-wash",
            "body-shop",
            "dealership",
            "tire-center",
        ],
        "specialized-retail": [
            "clothing",
            "home-goods",
            "footwear",
            "newsstand",
            "consumer-electronics",
            "jewelry",
            "optician",
            "perfumery",
            "tobacco-shop",
            "phone-shop",
        ],
        "health-wellness": [
            "physiotherapy-center",
            "multi-specialist-medical-center",
            "pharmacy",
            "gym-fitness",
            "parapharmacy",
            "medical-orthopedics",
        ],
        "personal-services": [
            "travel-agency",
            "nail-center",
            "beauty",
            "laundry",
            "hairdresser",
            "tailor",
            "private-school-courses",
        ],
        "professional-services-public": [
            "insurance-agency",
            "real-estate-agency",
            "caf-patronato",
            "retail-financial-services",
        ],
        "tourism-light-hospitality": ["guest-rooms", "bnb", "hostel"],
    },
    "businesses": {
        "company-macro-sector": [
            "company-agro-food",
            "company-environment-circular-esg",
            "company-construction-infrastructure",
            "company-energy-utilities",
            "company-hospitality-tourism",
            "company-ict-media-technology",
            "company-industry-manufacturing",
            "company-logistics-transport",
            "company-retail-distribution",
            "company-healthcare-personal-services",
            "company-business-services",
            "company-special-other",
        ],
    },
    "real-estate": {
        "specialist-destinations": [
            "buildable-area",
            "garage",
            "agricultural-business",
            "care-home",
            "barracks",
            "sports-complex",
            "convent",
            "senior-living",
            "bank-branch",
            "beach-establishment",
            "student-housing",
            "agricultural-land",
            "public-office",
        ],
        "non-residential-real-estate": [
            "agriturismo",
            "camping",
            "craft-shed",
            "commercial-shed",
            "industrial-shed",
            "logistics-shed",
            "shopping-center",
            "hotel",
            "laboratory",
            "warehouse",
            "shop",
            "outlet-park",
            "directional-building",
            "residence",
            "retail-park",
            "office-unit",
            "holiday-village",
        ],
        "prestige-residential-properties": [
            "apartment",
            "penthouse",
            "farmhouse",
            "castle",
            "palace",
            "villa",
        ],
    },
}

# Ancestry mapping: code -> {"parent": parent_code, "level": level}
CATEGORY_ANCESTRY: dict[str, dict[str, str | int]] = {
    "food-beverage": {"parent": "commercial-activities", "level": 2},
    "bar": {"parent": "food-beverage", "level": 3},
    "cafe": {"parent": "food-beverage", "level": 3},
    "wine-bar": {"parent": "food-beverage", "level": 3},
    "gastronomy": {"parent": "food-beverage", "level": 3},
    "ice-cream": {"parent": "food-beverage", "level": 3},
    "bakery-pastry": {"parent": "food-beverage", "level": 3},
    "pizzeria": {"parent": "food-beverage", "level": 3},
    "pub-brewery": {"parent": "food-beverage", "level": 3},
    "restaurant": {"parent": "food-beverage", "level": 3},
    "trattoria-osteria": {"parent": "food-beverage", "level": 3},
    "entertainment-leisure": {"parent": "commercial-activities", "level": 2},
    "bowling": {"parent": "entertainment-leisure", "level": 3},
    "betting-center": {"parent": "entertainment-leisure", "level": 3},
    "escape-room": {"parent": "entertainment-leisure", "level": 3},
    "gaming-hall": {"parent": "entertainment-leisure", "level": 3},
    "mobility-automotive-services": {"parent": "commercial-activities", "level": 2},
    "car-workshop": {"parent": "mobility-automotive-services", "level": 3},
    "car-wash": {"parent": "mobility-automotive-services", "level": 3},
    "body-shop": {"parent": "mobility-automotive-services", "level": 3},
    "dealership": {"parent": "mobility-automotive-services", "level": 3},
    "tire-center": {"parent": "mobility-automotive-services", "level": 3},
    "specialized-retail": {"parent": "commercial-activities", "level": 2},
    "clothing": {"parent": "specialized-retail", "level": 3},
    "home-goods": {"parent": "specialized-retail", "level": 3},
    "footwear": {"parent": "specialized-retail", "level": 3},
    "newsstand": {"parent": "specialized-retail", "level": 3},
    "consumer-electronics": {"parent": "specialized-retail", "level": 3},
    "jewelry": {"parent": "specialized-retail", "level": 3},
    "optician": {"parent": "specialized-retail", "level": 3},
    "perfumery": {"parent": "specialized-retail", "level": 3},
    "tobacco-shop": {"parent": "specialized-retail", "level": 3},
    "phone-shop": {"parent": "specialized-retail", "level": 3},
    "health-wellness": {"parent": "commercial-activities", "level": 2},
    "physiotherapy-center": {"parent": "health-wellness", "level": 3},
    "multi-specialist-medical-center": {"parent": "health-wellness", "level": 3},
    "pharmacy": {"parent": "health-wellness", "level": 3},
    "gym-fitness": {"parent": "health-wellness", "level": 3},
    "parapharmacy": {"parent": "health-wellness", "level": 3},
    "medical-orthopedics": {"parent": "health-wellness", "level": 3},
    "personal-services": {"parent": "commercial-activities", "level": 2},
    "travel-agency": {"parent": "personal-services", "level": 3},
    "nail-center": {"parent": "personal-services", "level": 3},
    "beauty": {"parent": "personal-services", "level": 3},
    "laundry": {"parent": "personal-services", "level": 3},
    "hairdresser": {"parent": "personal-services", "level": 3},
    "tailor": {"parent": "personal-services", "level": 3},
    "private-school-courses": {"parent": "personal-services", "level": 3},
    "professional-services-public": {"parent": "commercial-activities", "level": 2},
    "insurance-agency": {"parent": "professional-services-public", "level": 3},
    "real-estate-agency": {"parent": "professional-services-public", "level": 3},
    "caf-patronato": {"parent": "professional-services-public", "level": 3},
    "retail-financial-services": {"parent": "professional-services-public", "level": 3},
    "tourism-light-hospitality": {"parent": "commercial-activities", "level": 2},
    "guest-rooms": {"parent": "tourism-light-hospitality", "level": 3},
    "bnb": {"parent": "tourism-light-hospitality", "level": 3},
    "hostel": {"parent": "tourism-light-hospitality", "level": 3},
    "company-macro-sector": {"parent": "businesses", "level": 2},
    "company-agro-food": {"parent": "company-macro-sector", "level": 3},
    "company-environment-circular-esg": {"parent": "company-macro-sector", "level": 3},
    "company-construction-infrastructure": {"parent": "company-macro-sector", "level": 3},
    "company-energy-utilities": {"parent": "company-macro-sector", "level": 3},
    "company-hospitality-tourism": {"parent": "company-macro-sector", "level": 3},
    "company-ict-media-technology": {"parent": "company-macro-sector", "level": 3},
    "company-industry-manufacturing": {"parent": "company-macro-sector", "level": 3},
    "company-logistics-transport": {"parent": "company-macro-sector", "level": 3},
    "company-retail-distribution": {"parent": "company-macro-sector", "level": 3},
    "company-healthcare-personal-services": {"parent": "company-macro-sector", "level": 3},
    "company-business-services": {"parent": "company-macro-sector", "level": 3},
    "company-special-other": {"parent": "company-macro-sector", "level": 3},
    "specialist-destinations": {"parent": "real-estate", "level": 2},
    "buildable-area": {"parent": "specialist-destinations", "level": 3},
    "garage": {"parent": "specialist-destinations", "level": 3},
    "agricultural-business": {"parent": "specialist-destinations", "level": 3},
    "care-home": {"parent": "specialist-destinations", "level": 3},
    "barracks": {"parent": "specialist-destinations", "level": 3},
    "sports-complex": {"parent": "specialist-destinations", "level": 3},
    "convent": {"parent": "specialist-destinations", "level": 3},
    "senior-living": {"parent": "specialist-destinations", "level": 3},
    "bank-branch": {"parent": "specialist-destinations", "level": 3},
    "beach-establishment": {"parent": "specialist-destinations", "level": 3},
    "student-housing": {"parent": "specialist-destinations", "level": 3},
    "agricultural-land": {"parent": "specialist-destinations", "level": 3},
    "public-office": {"parent": "specialist-destinations", "level": 3},
    "non-residential-real-estate": {"parent": "real-estate", "level": 2},
    "agriturismo": {"parent": "non-residential-real-estate", "level": 3},
    "camping": {"parent": "non-residential-real-estate", "level": 3},
    "craft-shed": {"parent": "non-residential-real-estate", "level": 3},
    "commercial-shed": {"parent": "non-residential-real-estate", "level": 3},
    "industrial-shed": {"parent": "non-residential-real-estate", "level": 3},
    "logistics-shed": {"parent": "non-residential-real-estate", "level": 3},
    "shopping-center": {"parent": "non-residential-real-estate", "level": 3},
    "hotel": {"parent": "non-residential-real-estate", "level": 3},
    "laboratory": {"parent": "non-residential-real-estate", "level": 3},
    "warehouse": {"parent": "non-residential-real-estate", "level": 3},
    "shop": {"parent": "non-residential-real-estate", "level": 3},
    "outlet-park": {"parent": "non-residential-real-estate", "level": 3},
    "directional-building": {"parent": "non-residential-real-estate", "level": 3},
    "residence": {"parent": "non-residential-real-estate", "level": 3},
    "retail-park": {"parent": "non-residential-real-estate", "level": 3},
    "office-unit": {"parent": "non-residential-real-estate", "level": 3},
    "holiday-village": {"parent": "non-residential-real-estate", "level": 3},
    "prestige-residential-properties": {"parent": "real-estate", "level": 2},
    "apartment": {"parent": "prestige-residential-properties", "level": 3},
    "penthouse": {"parent": "prestige-residential-properties", "level": 3},
    "farmhouse": {"parent": "prestige-residential-properties", "level": 3},
    "castle": {"parent": "prestige-residential-properties", "level": 3},
    "palace": {"parent": "prestige-residential-properties", "level": 3},
    "villa": {"parent": "prestige-residential-properties", "level": 3},
}

LEVEL_1_CODES: list[str] = ["commercial-activities", "businesses", "real-estate"]
LEVEL_2_CODES: list[str] = [
    "food-beverage",
    "entertainment-leisure",
    "mobility-automotive-services",
    "specialized-retail",
    "health-wellness",
    "personal-services",
    "professional-services-public",
    "tourism-light-hospitality",
    "company-macro-sector",
    "specialist-destinations",
    "non-residential-real-estate",
    "prestige-residential-properties",
]
LEVEL_3_CODES: list[str] = [
    "bar",
    "cafe",
    "wine-bar",
    "gastronomy",
    "ice-cream",
    "bakery-pastry",
    "pizzeria",
    "pub-brewery",
    "restaurant",
    "trattoria-osteria",
    "bowling",
    "betting-center",
    "escape-room",
    "gaming-hall",
    "car-workshop",
    "car-wash",
    "body-shop",
    "dealership",
    "tire-center",
    "clothing",
    "home-goods",
    "footwear",
    "newsstand",
    "consumer-electronics",
    "jewelry",
    "optician",
    "perfumery",
    "tobacco-shop",
    "phone-shop",
    "physiotherapy-center",
    "multi-specialist-medical-center",
    "pharmacy",
    "gym-fitness",
    "parapharmacy",
    "medical-orthopedics",
    "travel-agency",
    "nail-center",
    "beauty",
    "laundry",
    "hairdresser",
    "tailor",
    "private-school-courses",
    "insurance-agency",
    "real-estate-agency",
    "caf-patronato",
    "retail-financial-services",
    "guest-rooms",
    "bnb",
    "hostel",
    "company-agro-food",
    "company-environment-circular-esg",
    "company-construction-infrastructure",
    "company-energy-utilities",
    "company-hospitality-tourism",
    "company-ict-media-technology",
    "company-industry-manufacturing",
    "company-logistics-transport",
    "company-retail-distribution",
    "company-healthcare-personal-services",
    "company-business-services",
    "company-special-other",
    "buildable-area",
    "garage",
    "agricultural-business",
    "care-home",
    "barracks",
    "sports-complex",
    "convent",
    "senior-living",
    "bank-branch",
    "beach-establishment",
    "student-housing",
    "agricultural-land",
    "public-office",
    "agriturismo",
    "camping",
    "craft-shed",
    "commercial-shed",
    "industrial-shed",
    "logistics-shed",
    "shopping-center",
    "hotel",
    "laboratory",
    "warehouse",
    "shop",
    "outlet-park",
    "directional-building",
    "residence",
    "retail-park",
    "office-unit",
    "holiday-village",
    "apartment",
    "penthouse",
    "farmhouse",
    "castle",
    "palace",
    "villa",
]


# ---------------------------------------------------------------------------
# Helper functions for Block 2 (Contract Determination)
# ---------------------------------------------------------------------------


def _runtime_category_tree(cfg: Settings | None = None) -> dict[str, dict[str, list[str]]]:
    effective_cfg = cfg or _default_settings
    payload = _load_runtime_categories_payload(str(effective_cfg.CATEGORY_CATALOG_PATH))
    indices = payload.get("indices")
    if isinstance(indices, dict):
        category_tree = indices.get("category_tree")
        if isinstance(category_tree, dict):
            return category_tree
    return CATEGORY_TREE


def _runtime_category_names(cfg: Settings | None = None) -> dict[str, str]:
    effective_cfg = cfg or _default_settings
    payload = _load_runtime_categories_payload(str(effective_cfg.CATEGORY_CATALOG_PATH))
    indices = payload.get("indices")
    if isinstance(indices, dict):
        category_names = indices.get("category_names")
        if isinstance(category_names, dict):
            return category_names
    return CATEGORY_NAMES


@lru_cache(maxsize=8)
def _load_runtime_categories_payload(path_str: str) -> dict[str, object]:
    path = Path(path_str)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload
    return {}


def clear_categories_cache() -> None:
    """Invalidate cached runtime categories after catalog swaps."""
    _load_runtime_categories_payload.cache_clear()


def get_candidate_paths(lv1: str, cfg: Settings | None = None) -> list[tuple[str, str]]:
    """Return all valid ``(lv2, lv3)`` pairs under the given *lv1*.

    Parameters
    ----------
    lv1:
        A level-1 category code (e.g. ``"commercial-activities"``).

    Returns
    -------
    list[tuple[str, str]]
        Every ``(lv2_code, lv3_code)`` pair in ``CATEGORY_TREE[lv1]``.

    Raises
    ------
    KeyError
        If *lv1* is not a valid level-1 code.
    """
    subtree = _runtime_category_tree(cfg)[lv1]  # raises KeyError if invalid
    paths: list[tuple[str, str]] = []
    for lv2_code, lv3_codes in subtree.items():
        for lv3_code in lv3_codes:
            paths.append((lv2_code, lv3_code))
    return paths


def get_candidate_labels(
    lv1: str,
    cfg: Settings | None = None,
) -> list[tuple[tuple[str, str], str]]:
    """Return ``((lv2, lv3), "Lv2Name > Lv3Name")`` for all candidates under *lv1*.

    The label string uses the human-readable names from :data:`CATEGORY_NAMES`,
    formatted as ``"<lv2_name> > <lv3_name>"`` (e.g. ``"Food & Beverage > Ristorante"``).

    Parameters
    ----------
    lv1:
        A level-1 category code.

    Returns
    -------
    list[tuple[tuple[str, str], str]]
        Each element is ``((lv2_code, lv3_code), label_text)``.

    Raises
    ------
    KeyError
        If *lv1* is not a valid level-1 code.
    """
    paths = get_candidate_paths(lv1, cfg=cfg)
    category_names = _runtime_category_names(cfg)
    labelled: list[tuple[tuple[str, str], str]] = []
    for lv2_code, lv3_code in paths:
        lv2_name = category_names[lv2_code]
        lv3_name = category_names[lv3_code]
        label = f"{lv2_name} > {lv3_name}"
        labelled.append(((lv2_code, lv3_code), label))
    return labelled
