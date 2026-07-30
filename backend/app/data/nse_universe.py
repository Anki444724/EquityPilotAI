"""The NSE coverage universe — 120 real listed companies.

Selected to span the market rather than to flatter the engines: every sector
in the workbook's `00 Setup` sector table is represented, large caps sit
alongside mid caps, and the list deliberately includes the three business
models the canonical 54-item schema handles least well — banks, NBFCs and
insurers — because a validation sprint that only covers manufacturers proves
nothing about the rest.

Each row is `(ticker, name, sector, industry)`. Sector strings match
`00 Setup` exactly, because Modules 4 and 5 look up sector-median PE, PB,
EV/EBITDA, ROE and beta by that string; a mismatch silently falls back to a
default and the valuation is quietly wrong.

Yahoo Finance is the source: `<TICKER>.NS`. It is a secondary source, not a
filing, and §2 of the validation report says so plainly.
"""
from __future__ import annotations

#: (ticker, name, sector, industry)
NSE_UNIVERSE: tuple[tuple[str, str, str, str], ...] = (
    # ---- Oil, Gas & Refining -------------------------------------------
    ("RELIANCE", "Reliance Industries Ltd", "Oil, Gas & Refining", "Refining & Petrochemicals"),
    ("ONGC", "Oil & Natural Gas Corporation Ltd", "Oil, Gas & Refining", "Exploration & Production"),
    ("IOC", "Indian Oil Corporation Ltd", "Oil, Gas & Refining", "Refining & Marketing"),
    ("BPCL", "Bharat Petroleum Corporation Ltd", "Oil, Gas & Refining", "Refining & Marketing"),
    ("GAIL", "GAIL (India) Ltd", "Oil, Gas & Refining", "Gas Transmission"),
    ("HINDPETRO", "Hindustan Petroleum Corporation Ltd", "Oil, Gas & Refining", "Refining & Marketing"),
    ("PETRONET", "Petronet LNG Ltd", "Oil, Gas & Refining", "LNG Regasification"),

    # ---- IT Services ----------------------------------------------------
    ("TCS", "Tata Consultancy Services Ltd", "IT Services", "IT Consulting"),
    ("INFY", "Infosys Ltd", "IT Services", "IT Consulting"),
    ("HCLTECH", "HCL Technologies Ltd", "IT Services", "IT Consulting"),
    ("WIPRO", "Wipro Ltd", "IT Services", "IT Consulting"),
    ("TECHM", "Tech Mahindra Ltd", "IT Services", "IT Consulting"),
    ("LTIM", "LTIMindtree Ltd", "IT Services", "IT Consulting"),
    ("PERSISTENT", "Persistent Systems Ltd", "IT Services", "IT Consulting"),
    ("COFORGE", "Coforge Ltd", "IT Services", "IT Consulting"),
    ("MPHASIS", "Mphasis Ltd", "IT Services", "IT Consulting"),

    # ---- Banking - Private ----------------------------------------------
    ("HDFCBANK", "HDFC Bank Ltd", "Banking - Private", "Private Sector Bank"),
    ("ICICIBANK", "ICICI Bank Ltd", "Banking - Private", "Private Sector Bank"),
    ("KOTAKBANK", "Kotak Mahindra Bank Ltd", "Banking - Private", "Private Sector Bank"),
    ("AXISBANK", "Axis Bank Ltd", "Banking - Private", "Private Sector Bank"),
    ("INDUSINDBK", "IndusInd Bank Ltd", "Banking - Private", "Private Sector Bank"),
    ("FEDERALBNK", "Federal Bank Ltd", "Banking - Private", "Private Sector Bank"),
    ("IDFCFIRSTB", "IDFC First Bank Ltd", "Banking - Private", "Private Sector Bank"),

    # ---- Banking - PSU ---------------------------------------------------
    ("SBIN", "State Bank of India", "Banking - PSU", "Public Sector Bank"),
    ("BANKBARODA", "Bank of Baroda", "Banking - PSU", "Public Sector Bank"),
    ("PNB", "Punjab National Bank", "Banking - PSU", "Public Sector Bank"),
    ("CANBK", "Canara Bank", "Banking - PSU", "Public Sector Bank"),
    ("UNIONBANK", "Union Bank of India", "Banking - PSU", "Public Sector Bank"),

    # ---- NBFC & Financial Services ---------------------------------------
    ("BAJFINANCE", "Bajaj Finance Ltd", "NBFC & Financial Services", "Diversified NBFC"),
    ("BAJAJFINSV", "Bajaj Finserv Ltd", "NBFC & Financial Services", "Financial Holding"),
    ("CHOLAFIN", "Cholamandalam Investment & Finance Ltd", "NBFC & Financial Services", "Vehicle Finance"),
    ("SHRIRAMFIN", "Shriram Finance Ltd", "NBFC & Financial Services", "Vehicle Finance"),
    ("MUTHOOTFIN", "Muthoot Finance Ltd", "NBFC & Financial Services", "Gold Loans"),
    ("LICHSGFIN", "LIC Housing Finance Ltd", "NBFC & Financial Services", "Housing Finance"),
    ("PFC", "Power Finance Corporation Ltd", "NBFC & Financial Services", "Infrastructure Finance"),
    ("RECLTD", "REC Ltd", "NBFC & Financial Services", "Infrastructure Finance"),

    # ---- Insurance --------------------------------------------------------
    ("SBILIFE", "SBI Life Insurance Company Ltd", "Insurance", "Life Insurance"),
    ("HDFCLIFE", "HDFC Life Insurance Company Ltd", "Insurance", "Life Insurance"),
    ("ICICIGI", "ICICI Lombard General Insurance Ltd", "Insurance", "General Insurance"),
    ("LICI", "Life Insurance Corporation of India", "Insurance", "Life Insurance"),

    # ---- FMCG --------------------------------------------------------------
    ("HINDUNILVR", "Hindustan Unilever Ltd", "FMCG", "Personal & Household Products"),
    ("ITC", "ITC Ltd", "FMCG", "Diversified FMCG"),
    ("NESTLEIND", "Nestle India Ltd", "FMCG", "Packaged Foods"),
    ("BRITANNIA", "Britannia Industries Ltd", "FMCG", "Packaged Foods"),
    ("DABUR", "Dabur India Ltd", "FMCG", "Personal & Household Products"),
    ("GODREJCP", "Godrej Consumer Products Ltd", "FMCG", "Personal & Household Products"),
    ("MARICO", "Marico Ltd", "FMCG", "Packaged Foods"),
    ("COLPAL", "Colgate-Palmolive (India) Ltd", "FMCG", "Personal & Household Products"),
    ("TATACONSUM", "Tata Consumer Products Ltd", "FMCG", "Packaged Foods"),
    ("VBL", "Varun Beverages Ltd", "FMCG", "Beverages"),

    # ---- Pharmaceuticals ----------------------------------------------------
    ("SUNPHARMA", "Sun Pharmaceutical Industries Ltd", "Pharmaceuticals", "Generic Pharma"),
    ("DRREDDY", "Dr Reddy's Laboratories Ltd", "Pharmaceuticals", "Generic Pharma"),
    ("CIPLA", "Cipla Ltd", "Pharmaceuticals", "Generic Pharma"),
    ("DIVISLAB", "Divi's Laboratories Ltd", "Pharmaceuticals", "API & CRAMS"),
    ("TORNTPHARM", "Torrent Pharmaceuticals Ltd", "Pharmaceuticals", "Generic Pharma"),
    ("LUPIN", "Lupin Ltd", "Pharmaceuticals", "Generic Pharma"),
    ("AUROPHARMA", "Aurobindo Pharma Ltd", "Pharmaceuticals", "Generic Pharma"),
    ("ZYDUSLIFE", "Zydus Lifesciences Ltd", "Pharmaceuticals", "Generic Pharma"),
    ("ALKEM", "Alkem Laboratories Ltd", "Pharmaceuticals", "Generic Pharma"),

    # ---- Healthcare & Hospitals ----------------------------------------------
    ("APOLLOHOSP", "Apollo Hospitals Enterprise Ltd", "Healthcare & Hospitals", "Hospitals"),
    ("MAXHEALTH", "Max Healthcare Institute Ltd", "Healthcare & Hospitals", "Hospitals"),
    ("FORTIS", "Fortis Healthcare Ltd", "Healthcare & Hospitals", "Hospitals"),

    # ---- Automobile & Ancillaries ---------------------------------------------
    ("MARUTI", "Maruti Suzuki India Ltd", "Automobile & Ancillaries", "Passenger Vehicles"),
    ("M&M", "Mahindra & Mahindra Ltd", "Automobile & Ancillaries", "Utility Vehicles"),
    ("TATAMOTORS", "Tata Motors Ltd", "Automobile & Ancillaries", "Commercial Vehicles"),
    ("BAJAJ-AUTO", "Bajaj Auto Ltd", "Automobile & Ancillaries", "Two Wheelers"),
    ("EICHERMOT", "Eicher Motors Ltd", "Automobile & Ancillaries", "Two Wheelers"),
    ("HEROMOTOCO", "Hero MotoCorp Ltd", "Automobile & Ancillaries", "Two Wheelers"),
    ("TVSMOTOR", "TVS Motor Company Ltd", "Automobile & Ancillaries", "Two Wheelers"),
    ("BOSCHLTD", "Bosch Ltd", "Automobile & Ancillaries", "Auto Components"),
    ("MOTHERSON", "Samvardhana Motherson International Ltd", "Automobile & Ancillaries", "Auto Components"),

    # ---- Capital Goods & Engineering --------------------------------------------
    ("LT", "Larsen & Toubro Ltd", "Capital Goods & Engineering", "Construction & Engineering"),
    ("SIEMENS", "Siemens Ltd", "Capital Goods & Engineering", "Electrical Equipment"),
    ("ABB", "ABB India Ltd", "Capital Goods & Engineering", "Electrical Equipment"),
    ("CUMMINSIND", "Cummins India Ltd", "Capital Goods & Engineering", "Industrial Machinery"),
    ("THERMAX", "Thermax Ltd", "Capital Goods & Engineering", "Industrial Machinery"),
    ("POLYCAB", "Polycab India Ltd", "Capital Goods & Engineering", "Cables & Wires"),

    # ---- Defence & Aerospace ------------------------------------------------------
    ("HAL", "Hindustan Aeronautics Ltd", "Defence & Aerospace", "Aerospace & Defence"),
    ("BEL", "Bharat Electronics Ltd", "Defence & Aerospace", "Defence Electronics"),
    ("BDL", "Bharat Dynamics Ltd", "Defence & Aerospace", "Missiles & Ammunition"),

    # ---- Metals & Mining ------------------------------------------------------------
    ("TATASTEEL", "Tata Steel Ltd", "Metals & Mining", "Iron & Steel"),
    ("JSWSTEEL", "JSW Steel Ltd", "Metals & Mining", "Iron & Steel"),
    ("HINDALCO", "Hindalco Industries Ltd", "Metals & Mining", "Aluminium"),
    ("VEDL", "Vedanta Ltd", "Metals & Mining", "Diversified Metals"),
    ("COALINDIA", "Coal India Ltd", "Metals & Mining", "Coal Mining"),
    ("JINDALSTEL", "Jindal Steel & Power Ltd", "Metals & Mining", "Iron & Steel"),
    ("NMDC", "NMDC Ltd", "Metals & Mining", "Iron Ore Mining"),

    # ---- Cement ------------------------------------------------------------------------
    ("ULTRACEMCO", "UltraTech Cement Ltd", "Cement", "Cement & Products"),
    ("SHREECEM", "Shree Cement Ltd", "Cement", "Cement & Products"),
    ("AMBUJACEM", "Ambuja Cements Ltd", "Cement", "Cement & Products"),
    ("ACC", "ACC Ltd", "Cement", "Cement & Products"),
    ("DALBHARAT", "Dalmia Bharat Ltd", "Cement", "Cement & Products"),

    # ---- Chemicals & Specialty --------------------------------------------------------------
    ("ASIANPAINT", "Asian Paints Ltd", "Chemicals & Specialty", "Paints"),
    ("BERGEPAINT", "Berger Paints India Ltd", "Chemicals & Specialty", "Paints"),
    ("PIDILITIND", "Pidilite Industries Ltd", "Chemicals & Specialty", "Adhesives"),
    ("SRF", "SRF Ltd", "Chemicals & Specialty", "Specialty Chemicals"),
    ("GRASIM", "Grasim Industries Ltd", "Chemicals & Specialty", "Diversified"),
    ("DEEPAKNTR", "Deepak Nitrite Ltd", "Chemicals & Specialty", "Specialty Chemicals"),
    ("AARTIIND", "Aarti Industries Ltd", "Chemicals & Specialty", "Specialty Chemicals"),

    # ---- Telecom -----------------------------------------------------------------------------
    ("BHARTIARTL", "Bharti Airtel Ltd", "Telecom", "Telecom Services"),
    ("IDEA", "Vodafone Idea Ltd", "Telecom", "Telecom Services"),
    ("INDUSTOWER", "Indus Towers Ltd", "Telecom", "Telecom Infrastructure"),

    # ---- Power & Utilities ---------------------------------------------------------------------
    ("NTPC", "NTPC Ltd", "Power & Utilities", "Power Generation"),
    ("POWERGRID", "Power Grid Corporation of India Ltd", "Power & Utilities", "Power Transmission"),
    ("TATAPOWER", "Tata Power Company Ltd", "Power & Utilities", "Integrated Power"),
    ("ADANIGREEN", "Adani Green Energy Ltd", "Power & Utilities", "Renewable Energy"),
    ("NHPC", "NHPC Ltd", "Power & Utilities", "Hydro Power"),

    # ---- Consumer Durables ----------------------------------------------------------------------
    ("TITAN", "Titan Company Ltd", "Consumer Durables", "Gems & Jewellery"),
    ("HAVELLS", "Havells India Ltd", "Consumer Durables", "Electrical Appliances"),
    ("VOLTAS", "Voltas Ltd", "Consumer Durables", "Air Conditioning"),
    ("CROMPTON", "Crompton Greaves Consumer Electricals Ltd", "Consumer Durables", "Electrical Appliances"),
    ("DIXON", "Dixon Technologies (India) Ltd", "Consumer Durables", "Electronics Manufacturing"),

    # ---- Retail & E-commerce ----------------------------------------------------------------------
    ("DMART", "Avenue Supermarts Ltd", "Retail & E-commerce", "Food Retail"),
    ("TRENT", "Trent Ltd", "Retail & E-commerce", "Apparel Retail"),
    ("NYKAA", "FSN E-Commerce Ventures Ltd", "Retail & E-commerce", "Online Retail"),
    ("ZOMATO", "Eternal Ltd", "Retail & E-commerce", "Food Delivery"),

    # ---- Infrastructure & Construction ---------------------------------------------------------------
    ("ADANIPORTS", "Adani Ports & SEZ Ltd", "Infrastructure & Construction", "Ports"),
    ("GMRAIRPORT", "GMR Airports Ltd", "Infrastructure & Construction", "Airports"),
    ("IRB", "IRB Infrastructure Developers Ltd", "Infrastructure & Construction", "Roads & Highways"),

    # ---- Logistics & Transport ---------------------------------------------------------------------
    ("CONCOR", "Container Corporation of India Ltd", "Logistics & Transport", "Rail Logistics"),
    ("INDIGO", "InterGlobe Aviation Ltd", "Logistics & Transport", "Airlines"),
    ("DELHIVERY", "Delhivery Ltd", "Logistics & Transport", "Express Logistics"),

    # ---- Hotels & Travel ----------------------------------------------------------------------------
    ("INDHOTEL", "Indian Hotels Company Ltd", "Hotels & Travel", "Hotels"),

    # ---- Real Estate --------------------------------------------------------------------------------
    ("DLF", "DLF Ltd", "Real Estate", "Residential & Commercial"),
    ("GODREJPROP", "Godrej Properties Ltd", "Real Estate", "Residential"),
    ("OBEROIRLTY", "Oberoi Realty Ltd", "Real Estate", "Residential & Commercial"),

    # ---- Media & Entertainment ------------------------------------------------------------------------
    ("PVRINOX", "PVR INOX Ltd", "Media & Entertainment", "Cinema Exhibition"),
    ("SUNTV", "Sun TV Network Ltd", "Media & Entertainment", "Broadcasting"),

    # ---- Textiles ---------------------------------------------------------------------------------------
    ("PAGEIND", "Page Industries Ltd", "Textiles", "Innerwear & Apparel"),
    ("TRIDENT", "Trident Ltd", "Textiles", "Home Textiles"),

    # ---- Agri & Fertilisers ---------------------------------------------------------------------------
    ("UPL", "UPL Ltd", "Agri & Fertilisers", "Agrochemicals"),
    ("PIIND", "PI Industries Ltd", "Agri & Fertilisers", "Agrochemicals"),
    ("COROMANDEL", "Coromandel International Ltd", "Agri & Fertilisers", "Fertilisers"),

    # ---- Paper & Packaging -----------------------------------------------------------------------------
    ("APLAPOLLO", "APL Apollo Tubes Ltd", "Paper & Packaging", "Steel Tubes"),

    # ---- Diversified / Conglomerate --------------------------------------------------------------------
    ("ADANIENT", "Adani Enterprises Ltd", "Diversified / Conglomerate", "Diversified"),
    ("SIEMENS-ENERGY", "Siemens Energy India Ltd", "Diversified / Conglomerate", "Energy Equipment"),
)


#: Business models the canonical 54-item schema does not represent faithfully.
#:
#: A bank has no revenue line in the industrial sense, no cost of goods, no
#: working capital cycle and no meaningful EV/EBITDA. Feeding one through a
#: DCF built for manufacturers produces a number, and the number is wrong.
#: Module 4 already refuses to publish a valuation it cannot stand behind;
#: this set is what tells the validation harness to *expect* that refusal
#: rather than to record it as a failure.
FINANCIAL_SECTORS: frozenset[str] = frozenset({
    "Banking - Private",
    "Banking - PSU",
    "NBFC & Financial Services",
    "Insurance",
})


def is_financial(sector: str | None) -> bool:
    return (sector or "") in FINANCIAL_SECTORS


def universe(limit: int | None = None) -> tuple[tuple[str, str, str, str], ...]:
    return NSE_UNIVERSE[:limit] if limit else NSE_UNIVERSE
