"""Consumer brands and the listed company that owns them, for names Yahoo's search misses.

Yahoo searches company names and tickers, so "zara" finds only currency pairs and a Canadian
certificate. This table sends a brand to its parent's primary listing (Zara -> Inditex, ITX.MC).
A few parent names are listed too, where Yahoo offers a secondary line first (Inditex -> IXD1.DE).
Every ticker was checked to return a covered equity from yfinance on 2026-10-21.

Words too generic to stand for one company (Office, Windows, Max, Prime) are left out on purpose.
"""

from __future__ import annotations

import unicodedata

# (primary listing, parent name, brands). A brand equal to the parent name is not repeated.
OWNERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # Fashion and luxury
    ("ITX.MC", "Inditex", (
        "Inditex", "Zara", "Zara Home", "Bershka", "Pull&Bear", "Pull and Bear",
        "Massimo Dutti", "Stradivarius", "Oysho", "Lefties",
    )),
    ("HM-B.ST", "H&M", ("H&M", "Hennes & Mauritz", "COS", "Arket", "Weekday", "Monki")),
    ("MC.PA", "LVMH", (
        "Louis Vuitton", "Dior", "Moët & Chandon", "Moët", "Hennessy", "Sephora", "Fendi",
        "Celine", "Givenchy", "Bulgari", "Bvlgari", "Tiffany", "TAG Heuer", "Hublot", "Loewe",
        "Kenzo",
        "Dom Pérignon", "Veuve Clicquot", "Guerlain", "Rimowa", "Berluti", "Loro Piana",
        "Marc Jacobs", "Ruinart", "Krug",
    )),
    ("KER.PA", "Kering", (
        "Gucci", "Saint Laurent", "Yves Saint Laurent", "YSL", "Balenciaga", "Bottega Veneta",
        "Boucheron", "Alexander McQueen", "Brioni", "Pomellato",
    )),
    ("CFR.SW", "Richemont", (
        "Cartier", "Van Cleef & Arpels", "IWC", "Jaeger-LeCoultre", "Montblanc", "Panerai",
        "Vacheron Constantin", "Chloé", "Piaget", "Buccellati",
    )),
    ("UHR.SW", "Swatch Group", (
        "Swatch", "Omega", "Longines", "Tissot", "Breguet", "Blancpain", "Rado",
    )),
    ("PUIG.MC", "Puig", (
        "Rabanne", "Paco Rabanne", "Carolina Herrera", "Jean Paul Gaultier",
        "Charlotte Tilbury", "Byredo", "Nina Ricci",
    )),
    ("CPRI", "Capri Holdings", ("Michael Kors", "Jimmy Choo")),
    ("TPR", "Tapestry", ("Coach", "Kate Spade")),
    ("PVH", "PVH", ("Calvin Klein", "Tommy Hilfiger")),
    ("GAP", "Gap Inc.", ("Gap", "Old Navy", "Banana Republic", "Athleta")),
    ("ANF", "Abercrombie & Fitch", ("Hollister",)),
    ("VFC", "VF Corporation", ("The North Face", "North Face", "Vans", "Timberland")),
    ("NKE", "Nike", ("Jordan", "Air Jordan", "Converse")),
    ("EL.PA", "EssilorLuxottica", (
        "Ray-Ban", "Oakley", "Persol", "Oliver Peoples", "Sunglass Hut", "Varilux",
        "Supreme", "Essilor", "Luxottica",
    )),
    # Beauty and personal care
    ("OR.PA", "L'Oréal", (
        "Lancôme", "Garnier", "Maybelline", "Kiehl's", "La Roche-Posay", "Vichy", "CeraVe",
        "NYX", "Kérastase", "Aesop", "Biotherm",
    )),
    ("EL", "Estée Lauder", (
        "Clinique", "MAC Cosmetics", "La Mer", "Jo Malone", "Bobbi Brown", "Aveda",
    )),
    ("BEI.DE", "Beiersdorf", ("Nivea", "Eucerin", "La Prairie", "Labello", "Hansaplast")),
    ("HEN3.DE", "Henkel", ("Schwarzkopf", "Loctite", "Persil", "Le Chat", "Pritt", "Diadermine")),
    ("ULVR.L", "Unilever", (
        "Dove", "Axe", "Lynx", "Rexona", "Knorr", "Hellmann's", "Vaseline", "Cif",
        "Domestos", "Sunsilk",
    )),
    ("PG", "Procter & Gamble", (
        "Pampers", "Gillette", "Ariel", "Tide", "Oral-B", "Head & Shoulders", "Pantene",
        "Braun", "Febreze", "Swiffer", "Old Spice", "Lenor", "Dash",
    )),
    ("CL", "Colgate-Palmolive", ("Colgate", "Palmolive", "Hill's")),
    ("HLN.L", "Haleon", (
        "Sensodyne", "Advil", "Centrum", "Voltaren", "Parodontax", "Aquafresh",
    )),
    ("RKT.L", "Reckitt", (
        "Durex", "Dettol", "Vanish", "Air Wick", "Strepsils", "Veet", "Harpic", "Calgon",
        "Gaviscon", "Nurofen",
    )),
    ("KVUE", "Kenvue", ("Tylenol", "Listerine", "Neutrogena", "Band-Aid", "Johnson's Baby")),
    # Food and drink
    ("NESN.SW", "Nestlé", (
        "Nespresso", "Nescafé", "KitKat", "Perrier", "Vittel", "San Pellegrino", "Purina",
        "Maggi", "Buitoni", "Nesquik", "Gerber", "Dolce Gusto", "Smarties",
    )),
    ("BN.PA", "Danone", (
        "Evian", "Activia", "Actimel", "Alpro", "Volvic", "Badoit", "Blédina", "Danette",
        "Aptamil",
    )),
    ("MDLZ", "Mondelez", (
        "Oreo", "Milka", "Cadbury", "Toblerone", "Côte d'Or", "Belvita", "Prince de LU",
        "Granola", "Mikado",
    )),
    ("KHC", "Kraft Heinz", ("Heinz", "Kraft", "Philadelphia", "Oscar Mayer")),
    ("KO", "Coca-Cola", ("Coca-Cola", "Coke", "Sprite", "Fanta", "Minute Maid", "Powerade",
                         "Costa Coffee", "Smartwater")),
    ("PEP", "PepsiCo", (
        "Pepsi", "Lay's", "Doritos", "Gatorade", "Quaker", "7Up", "Cheetos", "Mountain Dew",
        "Pepsi Max",
    )),
    ("GIS", "General Mills", ("Cheerios", "Häagen-Dazs", "Old El Paso")),
    ("LISN.SW", "Lindt & Sprüngli", ("Lindt", "Ghirardelli")),
    ("RI.PA", "Pernod Ricard", (
        "Ricard", "Absolut", "Jameson", "Chivas", "Chivas Regal", "Havana Club", "Malibu",
        "Martell", "Beefeater", "Lillet",
    )),
    ("DGE.L", "Diageo", (
        "Johnnie Walker", "Guinness", "Smirnoff", "Baileys", "Tanqueray", "Captain Morgan",
        "Don Julio", "J&B",
    )),
    ("RCO.PA", "Rémy Cointreau", ("Cointreau", "Rémy Martin", "Louis XIII")),
    ("CPR.MI", "Campari", ("Aperol", "Grand Marnier", "Skyy")),
    ("ABI.BR", "AB InBev", (
        "Budweiser", "Stella Artois", "Corona", "Leffe", "Hoegaarden", "Beck's", "Michelob",
    )),
    ("HEIA.AS", "Heineken", ("Desperados", "Amstel", "Affligem", "Pelforth", "Birra Moretti")),
    ("CARL-B.CO", "Carlsberg", ("Kronenbourg", "1664", "Grimbergen", "Tuborg")),
    # Restaurants, hotels, travel
    ("YUM", "Yum! Brands", ("KFC", "Pizza Hut", "Taco Bell")),
    ("QSR", "Restaurant Brands International", ("Burger King", "Tim Hortons", "Popeyes")),
    ("AC.PA", "Accor", (
        "Ibis", "Novotel", "Sofitel", "Mercure", "Fairmont", "Pullman", "Raffles",
    )),
    ("MAR", "Marriott", ("Sheraton", "Westin", "Ritz-Carlton", "W Hotels")),
    ("HLT", "Hilton", ("Hampton Inn", "Waldorf Astoria", "DoubleTree")),
    ("BKNG", "Booking Holdings", ("Booking.com", "Kayak", "Priceline", "Agoda", "OpenTable")),
    ("EXPE", "Expedia", ("Hotels.com", "Vrbo")),
    ("UBER", "Uber", ("Uber Eats",)),
    ("AF.PA", "Air France-KLM", ("Air France", "KLM", "Transavia")),
    ("IAG.L", "IAG", ("British Airways", "Iberia", "Vueling", "Aer Lingus")),
    ("LHA.DE", "Lufthansa", ("Eurowings", "Austrian Airlines", "Brussels Airlines")),
    # Cars
    ("STLAP.PA", "Stellantis", (
        "Peugeot", "Citroën", "Fiat", "Jeep", "Opel", "Alfa Romeo", "Maserati", "Chrysler",
        "Dodge", "Lancia", "DS Automobiles",
    )),
    ("VOW3.DE", "Volkswagen", (
        "VW", "Audi", "Skoda", "Seat", "Cupra", "Lamborghini", "Bentley", "Ducati",
    )),
    ("BMW.DE", "BMW", ("Mini",)),
    ("MBG.DE", "Mercedes-Benz", ("Mercedes", "Maybach", "AMG")),
    ("RNO.PA", "Renault", ("Dacia", "Alpine")),
    # Tech, media, internet
    ("GOOGL", "Alphabet", ("Google", "YouTube", "Android", "Waymo", "Gmail", "Fitbit")),
    ("META", "Meta Platforms", ("Facebook", "Instagram", "WhatsApp", "Oculus")),
    ("MSFT", "Microsoft", (
        "Xbox", "LinkedIn", "GitHub", "Azure", "Skype", "Bing", "Minecraft", "Activision",
        "Blizzard",
    )),
    ("AMZN", "Amazon", (
        "AWS", "Amazon Web Services", "Prime Video", "Twitch", "Whole Foods", "Audible",
        "Kindle", "Alexa",
    )),
    ("AAPL", "Apple", ("iPhone", "iPad", "MacBook", "AirPods", "iTunes")),
    ("SNAP", "Snap", ("Snapchat",)),
    ("MTCH", "Match Group", ("Tinder", "Hinge", "Meetic")),
    ("TTWO", "Take-Two Interactive", ("GTA", "Grand Theft Auto", "Rockstar Games")),
    ("EA", "Electronic Arts", ("EA Sports", "The Sims")),
    ("MAT", "Mattel", ("Barbie", "Hot Wheels", "Fisher-Price")),
    ("HAS", "Hasbro", ("Monopoly", "Play-Doh", "Nerf")),
    ("CRM", "Salesforce", ("Slack", "Tableau")),
    ("ADBE", "Adobe", ("Photoshop", "Acrobat", "Illustrator")),
    ("DSY.PA", "Dassault Systèmes", ("SolidWorks", "Catia")),
    ("AVGO", "Broadcom", ("VMware",)),
    ("BABA", "Alibaba", ("AliExpress", "Taobao", "Tmall")),
    ("PDD", "PDD Holdings", ("Temu", "Pinduoduo")),
    ("DIS", "Walt Disney", ("Disney", "Pixar", "Marvel", "ESPN", "Hulu", "Lucasfilm")),
    ("WBD", "Warner Bros. Discovery", ("HBO", "CNN", "Warner Bros")),
    ("CMCSA", "Comcast", ("NBC", "NBCUniversal", "Peacock", "Sky", "DreamWorks")),
    ("TMUS", "T-Mobile US", ("T-Mobile",)),
    ("UMG.AS", "Universal Music Group", ("Universal Music", "UMG")),
    # Retail and household
    ("WMT", "Walmart", ("Sam's Club", "Flipkart")),
    ("AD.AS", "Ahold Delhaize", ("Albert Heijn", "Delhaize", "Food Lion", "Stop & Shop")),
    ("KGF.L", "Kingfisher", ("Castorama", "B&Q", "Brico Dépôt", "Screwfix")),
    ("FNAC.PA", "Fnac Darty", ("Fnac", "Darty")),
    ("SK.PA", "SEB", ("Tefal", "Moulinex", "Rowenta", "Krups", "All-Clad")),
    ("PHIA.AS", "Philips", ("Sonicare", "Avent")),
    ("ELUX-B.ST", "Electrolux", ("AEG", "Frigidaire")),
    ("WHR", "Whirlpool", ("KitchenAid", "Maytag")),
    # Health
    ("NOVO-B.CO", "Novo Nordisk", ("Ozempic", "Wegovy")),
    ("LLY", "Eli Lilly", ("Mounjaro", "Zepbound")),
    ("PFE", "Pfizer", ("Viagra",)),
)

MIN_PREFIX = 3  # below this a brand only matches when typed in full ("KO", "VW")


def normalize(text: str) -> str:
    """Lower case, no accents, punctuation as spaces: "Moët & Chandon" -> "moet chandon"."""
    plain = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in plain if not unicodedata.combining(c)).lower()
    plain = plain.replace("'", "").replace("’", "")  # "L'Oréal" -> "loreal", "Kiehl's" -> "kiehls"
    return " ".join("".join(c if c.isalnum() else " " for c in plain).split())


def _index() -> dict[str, tuple[str, str, str]]:
    """Normalized brand -> (ticker, parent name, brand as written). Duplicates are an error."""
    index: dict[str, tuple[str, str, str]] = {}
    for ticker, parent, brands in OWNERS:
        for brand in brands:
            key = normalize(brand)
            if key in index and index[key][0] != ticker:
                raise ValueError(f"brand {brand!r} listed under {index[key][0]} and {ticker}")
            index.setdefault(key, (ticker, parent, brand))
    return index


BRAND_INDEX: dict[str, tuple[str, str, str]] = _index()


def brand_matches(
    query: str, index: dict[str, tuple[str, str, str]] = BRAND_INDEX
) -> list[tuple[str, str, str]]:
    """(ticker, parent, brand) for brands the query names or begins; one row per parent.

    An exact brand comes first, then brands the query is a prefix of, alphabetically.
    """
    needle = normalize(query)
    if not needle:
        return []
    exact = index.get(needle)
    prefixed = (
        [index[k] for k in sorted(index) if k != needle and k.startswith(needle)]
        if len(needle) >= MIN_PREFIX
        else []
    )
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for row in ([exact] if exact else []) + prefixed:
        if row[0] not in seen:
            seen.add(row[0])
            rows.append(row)
    return rows
